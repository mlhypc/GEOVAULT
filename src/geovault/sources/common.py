"""Shared COG helpers for source adapters.

Acquisition philosophy: ZERO remote compute. STAC-search for scenes, then
range-read exactly the tile windows we need from public COGs. All derivation
(cloud share, indices) happens locally.
"""

import io
import math

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.transform import Affine
from rasterio.windows import from_bounds

# SCL classes counted as unusable when computing a tile's cloud_pct:
# 1 saturated/defective, 3 cloud shadow, 8 cloud medium, 9 cloud high, 10 thin cirrus.
CLOUD_SCL = (1, 3, 8, 9, 10)


def read_tile_window(ds, grid, tx, ty, nodata):
    """Read one grid tile from an open rasterio dataset. Returns the array, or
    None when the tile is entirely outside the raster or entirely nodata."""
    w, s, e, n = grid.tile_bounds(tx, ty)
    rb = ds.bounds
    if e <= rb.left or w >= rb.right or n <= rb.bottom or s >= rb.top:
        return None
    win = from_bounds(w, s, e, n, transform=ds.transform)
    arr = ds.read(1, window=win, boundless=True, fill_value=nodata)
    if arr.shape != (grid.tile_px, grid.tile_px):
        return None
    if np.all(arr == nodata):
        return None
    return arr


def encode_geotiff(arr, grid, tx, ty, crs, nodata, scale=None, offset=None) -> bytes:
    """Encode one tile array as a DEFLATE GeoTIFF blob (lossless, self-describing).

    scale/offset, when given, are written as the GeoTIFF band scale and offset: the
    stored digital number stays raw (lossless) but the blob documents how to turn it
    into a physical value (Landsat Collection 2: reflectance and kelvin). Readers get
    them from rasterio as ds.scales[0] / ds.offsets[0]."""
    transform = Affine(*grid.tile_transform(tx, ty))
    buf = io.BytesIO()
    with rasterio.MemoryFile() as mem:
        with mem.open(
            driver="GTiff", width=grid.tile_px, height=grid.tile_px, count=1,
            dtype=arr.dtype, crs=crs, transform=transform, nodata=nodata,
            compress="deflate", predictor=2,
        ) as dst:
            dst.write(arr, 1)
            if scale is not None:
                dst.scales = [float(scale)]
            if offset is not None:
                dst.offsets = [float(offset)]
        buf.write(mem.read())
    return buf.getvalue()


from functools import lru_cache


@lru_cache(maxsize=64)
def _transformer(src: str, dst: str) -> Transformer:
    """Transformer.from_crs costs milliseconds (CRS parsing, pipeline search); tile loops call it
    tens of thousands of times per band, so one instance per (src, dst) pair is kept. Transformers
    are thread-safe for transform() calls."""
    return Transformer.from_crs(src, dst, always_xy=True)


def wgs84_bounds(grid, tx, ty, crs) -> tuple:
    """Tile corners reprojected to WGS84 (w, s, e, n) for uniform bbox queries."""
    w, s, e, n = grid.tile_bounds(tx, ty)
    xs, ys = _transformer(str(crs), "EPSG:4326").transform([w, e, w, e], [s, s, n, n])
    return (min(xs), min(ys), max(xs), max(ys))


def bbox_to_crs(bbox, crs) -> tuple:
    """WGS84 (w, s, e, n) -> native CRS rectangle (envelope of reprojected corners)."""
    w, s, e, n = bbox
    xs, ys = _transformer("EPSG:4326", str(crs)).transform([w, e, w, e], [s, s, n, n])
    return (min(xs), min(ys), max(xs), max(ys))


def candidate_tiles(grid, bbox, crs, aoi=None):
    """Tile indices worth testing for an ingest. With an AOI the candidates come from the bbox of
    EACH polygon part, not from the AOI's overall bbox: parcels spread over several countries
    otherwise expand to a continent-sized rectangle (tens of thousands of tiles per band, all
    reprojected and tested just to be rejected). Without an AOI, plain bbox mode."""
    if aoi is None:
        return list(grid.tiles_for_bounds(*bbox_to_crs(bbox, crs)))
    parts = list(getattr(aoi, "geoms", [aoi]))
    out = set()
    for g in parts:
        w, s, e, n = bbox_to_crs(g.bounds, crs)
        if not all(math.isfinite(v) for v in (w, s, e, n)):
            continue
        out.update(grid.tiles_for_bounds(w, s, e, n))
    return sorted(out)


def canonical_epsg(lon: float, lat: float) -> str:
    """The UTM zone a point canonically belongs to (EPSG:326xx N / 327xx S).

    Ground near a zone boundary appears in scenes of BOTH zones (MGRS overlap
    across CRS). The dedup key includes crs, so without a rule the same ground
    would be stored twice. Rule: a tile is stored only by the zone its center
    belongs to."""
    zone = int((lon + 180) // 6) + 1
    return f"EPSG:{(32600 if lat >= 0 else 32700) + zone}"


def tile_is_canonical(grid, tx, ty, crs) -> bool:
    """True if this tile's center lies in the zone of its own CRS."""
    w, s, e, n = wgs84_bounds(grid, tx, ty, crs)
    return canonical_epsg((w + e) / 2, (s + n) / 2) == crs


def cloud_pct_from_scl(scl_arr):
    """Unusable-pixel share (0-100) from an SCL array; None if all no-data."""
    valid = scl_arr != 0
    if not valid.any():
        return None
    cloudy = np.isin(scl_arr, CLOUD_SCL) & valid
    return round(float(cloudy.sum()) / float(valid.sum()) * 100.0, 2)


def scene_date_crs(item) -> tuple:
    """(YYYY-MM-DD, 'EPSG:xxxxx') of a STAC item."""
    from datetime import datetime
    date = datetime.fromisoformat(
        item.properties["datetime"].replace("Z", "+00:00")).strftime("%Y-%m-%d")
    epsg = item.properties.get("proj:epsg") or str(item.properties.get("proj:code", "")).replace("EPSG:", "")
    return date, f"EPSG:{epsg}"


def plan_tiles(grid, bbox, crs, band, date, skip_keys, keep_offzone, in_aoi) -> list:
    """Tile indices of `band` still to fetch for this scene: inside the bbox and the
    AOI, canonical to the scene's UTM zone, and not already in the store.
    Shared by ingest (what to read) and --dry-run (what would be read)."""
    nw, ns, ne, nn = bbox_to_crs(bbox, crs)
    return [(tx, ty) for tx, ty in grid.tiles_for_bounds(nw, ns, ne, nn)
            if (band, date, tx, ty) not in skip_keys
            and (keep_offzone or tile_is_canonical(grid, tx, ty, crs))
            and in_aoi(wgs84_bounds(grid, tx, ty, crs))]
