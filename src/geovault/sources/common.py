"""Shared COG helpers for source adapters.

Acquisition philosophy: ZERO remote compute. STAC-search for scenes, then
range-read exactly the tile windows we need from public COGs. All derivation
(cloud share, indices) happens locally.
"""

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


def _encode(arr, crs, transform, nodata, scale=None, offset=None, predictor=2) -> bytes:
    with rasterio.MemoryFile() as mem:
        with mem.open(
            driver="GTiff", width=arr.shape[1], height=arr.shape[0], count=1,
            dtype=arr.dtype, crs=crs, transform=transform, nodata=nodata,
            compress="deflate", predictor=predictor,
        ) as dst:
            dst.write(arr, 1)
            if scale is not None:
                dst.scales = [float(scale)]
            if offset is not None:
                dst.offsets = [float(offset)]
        return mem.read()


def encode_blob(arr, crs, transform, nodata, scale=None, offset=None, predictor=None) -> bytes:
    """DEFLATE GeoTIFF blob of one tile (lossless), choosing the TIFF predictor per tile.

    Integer tiles use the horizontal differencing predictor (2). Float tiles are encoded
    with both predictor 2 and the floating-point predictor (3) and the smaller blob is kept:
    3 wins on smooth continuous fields (reanalysis temperature -17%, S1 gamma0 -7%) and
    loses on sparse quantised ones (precipitation +30%), so neither is right for all bands.
    The choice is recorded in the TIFF itself; any reader decodes both.

    `predictor` pins the choice (2 or 3; 3 only for float tiles) and skips the trial
    encoding. Meant for sources with very many small tiles whose winner is known: it halves
    the encoding time. Pixels are identical either way; only the blob size can differ.
    `geovault reencode` does not know a source's pin and runs the trial again."""
    if predictor is not None:
        return _encode(arr, crs, transform, nodata, scale, offset, predictor)
    if np.issubdtype(arr.dtype, np.floating):
        b2 = _encode(arr, crs, transform, nodata, scale, offset, 2)
        b3 = _encode(arr, crs, transform, nodata, scale, offset, 3)
        return b3 if len(b3) < len(b2) else b2
    return _encode(arr, crs, transform, nodata, scale, offset, 2)


def encode_geotiff(arr, grid, tx, ty, crs, nodata, scale=None, offset=None, predictor=None) -> bytes:
    """Encode one grid tile array as a DEFLATE GeoTIFF blob (lossless, self-describing).

    scale/offset, when given, are written as the GeoTIFF band scale and offset: the
    stored digital number stays raw (lossless) but the blob documents how to turn it
    into a physical value (Landsat Collection 2: reflectance and kelvin). Readers get
    them from rasterio as ds.scales[0] / ds.offsets[0]. `predictor`: see encode_blob."""
    return encode_blob(arr, crs, Affine(*grid.tile_transform(tx, ty)), nodata, scale, offset,
                       predictor)


def reencode_blob(blob: bytes) -> bytes:
    """Re-encode a stored blob with the current encoder, keeping pixels, georeferencing,
    nodata and scale/offset bit-for-bit. Used by `geovault reencode` to bring old rows to
    the current encoding choice; returns the original when it is already the smaller."""
    with rasterio.MemoryFile(blob) as mem:
        with mem.open() as ds:
            arr = ds.read(1)
            crs, transform, nodata = ds.crs, ds.transform, ds.nodata
            scale = ds.scales[0] if ds.scales and ds.scales[0] != 1.0 else None
            offset = ds.offsets[0] if ds.offsets and ds.offsets[0] != 0.0 else None
    out = encode_blob(arr, crs, transform, nodata, scale, offset)
    return out if len(out) < len(blob) else blob


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
    """The regular UTM zone of a point (EPSG:326xx N / 327xx S): 6 degree zones, no MGRS
    exceptions. Used to pick a metric CRS for a point or a bbox, not to decide what is
    stored (see tile_is_canonical)."""
    zone = int((lon + 180) // 6) + 1
    return f"EPSG:{(32600 if lat >= 0 else 32700) + zone}"


def crs_utm_zone(crs):
    """(zone number, is southern) of a WGS84 UTM CRS given as 'EPSG:326xx' / 'EPSG:327xx',
    or None for any other CRS."""
    code = str(crs).upper().replace("EPSG:", "")
    if len(code) == 5 and code.isdigit() and code[:3] in ("326", "327") and 1 <= int(code[3:]) <= 60:
        return int(code[3:]), code[:3] == "327"
    return None


# Where the MGRS grid zones depart from the regular 6 degree zones, per latitude band
# (south, north): zone -> (west, east), None for a zone that does not exist in the band.
_MGRS_EXCEPTIONS = (
    ((56.0, 64.0), {31: (0.0, 3.0), 32: (3.0, 12.0)}),
    ((72.0, 84.0), {31: (0.0, 9.0), 32: None, 33: (9.0, 21.0), 34: None,
                    35: (21.0, 33.0), 36: None, 37: (33.0, 42.0)}),
)


def mgrs_zone_areas(zone: int, south: bool) -> list:
    """WGS84 rectangles (west, south, east, north) of the ground the MGRS grid assigns to
    a UTM zone and hemisphere: the 6 degree zone on one side of the equator, with zone 32
    widened to 3..12 E between 56 and 64 N (zone 31 narrowed to 0..3 E) and the four wide
    zones 31, 33, 35 and 37 between 72 and 84 N (32, 34 and 36 do not exist there)."""
    w = -180.0 + (zone - 1) * 6.0
    e = w + 6.0
    if south:
        return [(w, -90.0, e, 0.0)]
    out = [(w, 0.0, e, 56.0), (w, 64.0, e, 72.0), (w, 84.0, e, 90.0)]
    for (lat0, lat1), spans in _MGRS_EXCEPTIONS:
        span = spans.get(zone, (w, e))
        if span is not None:
            out.append((span[0], lat0, span[1], lat1))
    return out


def mgrs_epsg(lon: float, lat: float) -> str:
    """The UTM CRS the MGRS grid assigns to a point: canonical_epsg with the MGRS
    exceptions of mgrs_zone_areas, so the CRS a Sentinel-2 tile of that point is stored in."""
    zone = int(((lon + 180) % 360) // 6) + 1
    for (lat0, lat1), spans in _MGRS_EXCEPTIONS:
        if lat0 <= lat < lat1:
            for z, span in spans.items():
                if span is not None and span[0] <= lon < span[1]:
                    zone = z
    return f"EPSG:{(32600 if lat >= 0 else 32700) + zone}"


def tile_is_canonical(grid, tx, ty, crs) -> bool:
    """True when a tile of an MGRS-gridded product (Sentinel-2) is stored from scenes of
    this CRS: the tile touches the ground MGRS assigns to the CRS's zone and hemisphere.

    Granules of neighbouring zones, and of the two hemispheres, overlap, so the same
    ground arrives on the same day in two CRS; the key cannot tell they are the same
    ground. Every point has a granule in its own MGRS zone, and the tile of that CRS
    holding the point touches the zone, so no ground is lost. Each CRS keeps tiles reaching
    up to one tile past the line, so a strip about one tile wide on average, at most two,
    is stored in both CRS. (A rule keeping only tiles whose centre lies in the zone would
    leave ground on both sides of a line in no kept tile: the two lattices are rotated
    against each other, and shifted at the equator.)

    The tile's WGS84 extent is the envelope of its corners, which is exact in longitude
    and never too small where it decides. A CRS that is not a UTM zone has no zone rule."""
    zone = crs_utm_zone(crs)
    if zone is None:
        return True
    w, s, e, n = wgs84_bounds(grid, tx, ty, crs)
    return any(w < ae and e > aw and s < an and n > as_
               for aw, as_, ae, an in mgrs_zone_areas(*zone))


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
    AOI, not already in the store, and, unless keep_offzone is set, kept by the MGRS
    zone rule (tile_is_canonical). Only sources whose scenes follow the MGRS grid pass
    keep_offzone=False; every other source stores a scene in the CRS it comes in.
    Shared by ingest (what to read) and --dry-run (what would be read)."""
    nw, ns, ne, nn = bbox_to_crs(bbox, crs)
    return [(tx, ty) for tx, ty in grid.tiles_for_bounds(nw, ns, ne, nn)
            if (band, date, crs, tx, ty) not in skip_keys
            and (keep_offzone or tile_is_canonical(grid, tx, ty, crs))
            and in_aoi(wgs84_bounds(grid, tx, ty, crs))]
