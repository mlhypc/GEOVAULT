"""Shared COG helpers for source adapters.

Acquisition philosophy: ZERO remote compute. STAC-search for scenes, then
range-read exactly the tile windows we need from public COGs. All derivation
(cloud share, indices) happens locally.
"""

import math

import numpy as np
import rasterio
from pyproj import CRS, Transformer
from rasterio.transform import Affine
from rasterio.windows import from_bounds

# SCL classes counted as unusable when computing a tile's cloud_pct:
# 1 saturated/defective, 3 cloud shadow, 8 cloud medium, 9 cloud high, 10 thin cirrus.
CLOUD_SCL = (1, 3, 8, 9, 10)


def read_tile_window(ds, grid, tx, ty, nodata, band=1):
    """Read one grid tile of one band from an open rasterio dataset. Returns the array,
    or None when the tile is entirely outside the raster or holds no observation
    (`observed`: a nodata value of NaN marks a pixel as any other does)."""
    w, s, e, n = grid.tile_bounds(tx, ty)
    rb = ds.bounds
    if e <= rb.left or w >= rb.right or n <= rb.bottom or s >= rb.top:
        return None
    win = from_bounds(w, s, e, n, transform=ds.transform)
    arr = ds.read(band, window=win, boundless=True, fill_value=nodata)
    if arr.shape != (grid.tile_px, grid.tile_px):
        return None
    if not observed(arr, nodata).any():
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


def observed(arr, nodata):
    """True where a pixel holds an observation. A band without a nodata value has one
    everywhere."""
    if nodata is None:
        return np.ones(arr.shape, bool)
    if nodata != nodata:                    # NaN
        return ~np.isnan(arr)
    return arr != nodata


def merge_tiles(parts):
    """Merge tiles of one acquisition that each hold part of the same ground: adjacent
    scenes (granules, slices) cut one overpass, and a tile across the cut is partly
    empty in each of them.

    parts: (scene_id, blob, cloud_pct) per tile; a scene_id may already name several
    scenes ('a|b') when the tile is itself the result of a merge. The tile with the most
    observed pixels comes first (ties: the smaller scene id) and each following one only
    fills pixels that are still empty, so the result does not depend on the order of
    `parts`. Where scenes overlap their pixels can differ slightly; the first one's are
    kept; a tile is never replaced by a newer copy of a scene it already holds.

    Returns (blob, valid_px, scene_id, cloud_pct): scene_id names every scene, joined by
    '|'; cloud_pct is the mean of the tiles' values weighted by the pixels each one
    contributes, an estimate, or None when none has one. Raises ValueError when the
    tiles differ in size or data type."""
    tiles = []
    for scene, blob, cloud in parts:
        with rasterio.MemoryFile(bytes(blob)) as mem:
            with mem.open() as ds:
                arr = ds.read(1)
                scale = ds.scales[0] if ds.scales and ds.scales[0] != 1.0 else None
                offset = ds.offsets[0] if ds.offsets and ds.offsets[0] != 0.0 else None
                meta = (ds.crs, ds.transform, ds.nodata, scale, offset)
        tiles.append((scene or "", arr, observed(arr, meta[2]), cloud, blob, meta))
    if len({(t[1].shape, t[1].dtype) for t in tiles}) > 1:
        raise ValueError("tiles of one key differ in size or data type: "
                         + ", ".join(f"{t[0]} {t[1].shape} {t[1].dtype}" for t in tiles))
    tiles.sort(key=lambda t: (-int(t[2].sum()), t[0]))
    _, out, seen, cloud, first_blob, (crs, transform, nodata, scale, offset) = tiles[0]
    out, seen = out.copy(), seen.copy()
    shares = [(cloud, int(seen.sum()))]
    for _, arr, obs, cloud, _, _ in tiles[1:]:
        take = obs & ~seen
        out[take] = arr[take]
        seen |= take
        shares.append((cloud, int(take.sum())))
    scenes = "|".join(sorted({s for t in tiles for s in t[0].split("|") if s})) or None
    known = [(c, n) for c, n in shares if c is not None and n]
    cloud = round(sum(c * n for c, n in known) / sum(n for _, n in known), 2) if known else None
    filled = any(n for _, n in shares[1:])
    blob = encode_blob(out, crs, transform, nodata, scale, offset) if filled else bytes(first_blob)
    return blob, int(seen.sum()), scenes, cloud


from functools import lru_cache


@lru_cache(maxsize=64)
def _transformer(src: str, dst: str) -> Transformer:
    """Transformer.from_crs costs milliseconds (CRS parsing, pipeline search); tile loops call it
    tens of thousands of times per band, so one instance per (src, dst) pair is kept. Transformers
    are thread-safe for transform() calls."""
    return Transformer.from_crs(src, dst, always_xy=True)


@lru_cache(maxsize=64)
def _is_geographic(crs: str) -> bool:
    return CRS.from_user_input(crs).is_geographic


def lon_overlap(w: float, e: float, aw: float, ae: float) -> bool:
    """Do the longitude spans w..e and aw..ae share ground? w > e is a span across the
    antimeridian (w up to 180, then -180 up to e); aw..ae does not cross it."""
    if w <= e:
        return w < ae and e > aw
    return w < ae or e > aw


# The outline of a tile is followed in this many steps per edge for its WGS84 bounds.
OUTLINE_STEPS = 8
_EDGE = np.arange(OUTLINE_STEPS) / OUTLINE_STEPS
# The outline of the unit square, anticlockwise from its south-west corner, and two spare
# points: the point of the tile nearest each pole.
_OUTLINE_X = np.concatenate([_EDGE, np.ones(OUTLINE_STEPS), 1 - _EDGE, np.zeros(OUTLINE_STEPS + 2)])
_OUTLINE_Y = np.concatenate([np.zeros(OUTLINE_STEPS), _EDGE, np.ones(OUTLINE_STEPS), 1 - _EDGE,
                             np.zeros(2)])
_CORNERS = [0, OUTLINE_STEPS, 3 * OUTLINE_STEPS, 2 * OUTLINE_STEPS]      # sw, se, nw, ne


@lru_cache(maxsize=64)
def _poles(crs: str) -> tuple:
    """(x, y, latitude) of each pole that has a place in the CRS."""
    found = []
    for lat in (90.0, -90.0):
        x, y = _transformer("EPSG:4326", crs).transform(0.0, lat)
        if math.isfinite(x) and math.isfinite(y):
            found.append((x, y, lat))
    return tuple(found)


def wgs84_bounds(grid, tx, ty, crs) -> tuple:
    """WGS84 bounds (w, s, e, n) of a tile, for uniform bbox queries: they hold the
    ground of the tile, and sometimes more.

    In a projected CRS they are taken along the outline of the tile, OUTLINE_STEPS points
    per edge, and at its point nearest each pole. An edge is a curve in longitude and
    latitude: the four corners alone leave out what bulges past them, tens of kilometres
    of latitude beside a pole. (Between two outline points an edge still bulges past
    the bounds: under a twentieth of a pixel on tiles 256 km wide of UTM, conic, polar
    and sinusoidal grids, less on smaller tiles, more on a tile far outside the area
    its projection is made for.)

    - A tile across the antimeridian has west > east, as a bbox has in GeoJSON and STAC:
      its ground runs from west up to 180 and from -180 up to east. (The smallest and
      the largest longitude of such a tile span everything but the tile.) The tile lies
      on one side of the widest gap between the longitudes of its outline; where that
      gap is no wider than a step along the outline, the tile spans every longitude.
    - A tile that holds a pole, or lies within a pixel of one, spans every longitude and
      reaches the pole.
    - A tile with a corner off the projection has no finite bounds, and one that reaches
      past the edge of the projection gets bounds wider than its ground.
    - A tile of a geographic CRS has the bounds of its corners, in the longitudes of its
      grid, which may run past 180."""
    return _wgs84_bounds(grid, tx, ty, str(crs))


@lru_cache(maxsize=1 << 16)
def _wgs84_bounds(grid, tx: int, ty: int, crs: str) -> tuple:
    """One ingest asks for the bounds of a tile once per band and scene."""
    w, s, e, n = grid.tile_bounds(tx, ty)
    to_wgs84 = _transformer(crs, "EPSG:4326")
    if _is_geographic(crs):
        xs, ys = to_wgs84.transform([w, e, w, e], [s, s, n, n])
        return (min(xs), min(ys), max(xs), max(ys))
    x, y = w + (e - w) * _OUTLINE_X, s + (n - s) * _OUTLINE_Y
    held = []                                       # the latitude of a pole the tile holds
    for spare, (px, py, pole) in zip((-1, -2), _poles(crs)):
        nx, ny = min(max(px, w), e), min(max(py, s), n)     # the point of the tile nearest the pole
        if math.hypot(nx - px, ny - py) <= grid.res:
            held.append(pole)
        else:
            x[spare], y[spare] = nx, ny
    lon, lat = to_wgs84.transform(x, y)
    cx, cy = lon[_CORNERS], lat[_CORNERS]
    if not (np.isfinite(cx).all() and np.isfinite(cy).all()):
        return (float(cx.min()), float(cy.min()), float(cx.max()), float(cy.max()))
    lat = np.append(lat[np.isfinite(lat)], held)
    south, north = float(lat.min()), float(lat.max())
    if held:
        return (-180.0, south, 180.0, north)
    lon = lon[:-2]
    west, east = float(lon.min()), float(lon.max())
    if -180 <= west and east <= 180 and east - west < 180:      # the usual tile
        return (west, south, east, north)
    lon = lon[np.isfinite(lon)]
    lon = np.where(np.abs(lon) > 180, (lon + 180) % 360 - 180, lon)      # a CRS with +over
    order = np.sort(lon)
    gaps = np.diff(order, append=order[0] + 360)
    widest = int(gaps.argmax())
    step = np.abs((np.diff(lon, append=lon[0]) + 180) % 360 - 180).max()
    if gaps[widest] <= step + 1e-9:
        return (-180.0, south, 180.0, north)
    return (float(order[(widest + 1) % order.size]), south, float(order[widest]), north)


# The edges of a bbox are followed in steps of this many degrees when it is reprojected.
EDGE_STEP_DEG = 0.02


def bbox_to_crs(bbox, crs) -> tuple:
    """WGS84 (w, s, e, n) -> the native CRS rectangle that holds it. The edges are
    followed, not only the corners: a parallel is a curve in UTM, so an edge of a bbox
    that lies across the central meridian bulges beyond its corners (116 m for a bbox
    one degree wide, 4 km for one a whole zone wide, 23 km for one three zones wide),
    and a rectangle made from the corners left the tiles of that strip out of every
    plan. Points along the edges are reprojected one by one; a bbox no larger than one
    step has only its corners. Points the projection cannot place are left out."""
    w, s, e, n = bbox
    tr = _transformer("EPSG:4326", str(crs))
    span = max(abs(e - w), abs(n - s))
    steps = math.ceil(span / EDGE_STEP_DEG) if math.isfinite(span) else 0
    if steps <= 1:
        xs, ys = tr.transform([w, e, w, e], [s, s, n, n])
        return (min(xs), min(ys), max(xs), max(ys))
    t = np.linspace(0.0, 1.0, steps + 1)
    lon, lat = w + (e - w) * t, s + (n - s) * t
    xs, ys = tr.transform(np.concatenate([lon, lon, np.full_like(t, w), np.full_like(t, e)]),
                          np.concatenate([np.full_like(t, s), np.full_like(t, n), lat, lat]))
    ok = np.isfinite(xs) & np.isfinite(ys)
    if not ok.any():
        return (math.inf,) * 4
    return (float(xs[ok].min()), float(ys[ok].min()), float(xs[ok].max()), float(ys[ok].max()))


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

    The tile's WGS84 extent is wgs84_bounds, which is never smaller than the tile. A CRS
    that is not a UTM zone has no zone rule."""
    zone = crs_utm_zone(crs)
    if zone is None:
        return True
    w, s, e, n = wgs84_bounds(grid, tx, ty, crs)
    return any(lon_overlap(w, e, aw, ae) and s < an and n > as_
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


def stored_tiles(dataset, res, band, date, crs, acq, scene) -> dict:
    """(x, y) -> array of the tiles of one band already stored for an acquisition that
    hold the given scene: the mask a scene's other bands are judged by (SCL, qa_pixel)
    has to be its own, not that of a neighbouring scene of the overpass. A tile still
    held in several rows (scenes not merged yet) comes back merged as compaction would
    merge it, the fullest row first."""
    import duckdb
    from ..store import month_files, scan
    files = month_files(dataset, res, date)
    if not files:
        return {}
    con = duckdb.connect()
    rows = con.execute(
        f"SELECT x, y, scene_id, data FROM {scan(files)} WHERE band = ? AND date = ? "
        f"AND crs = ? AND acq IS NOT DISTINCT FROM ?", [band, date, crs, acq]).fetchall()
    con.close()
    by_tile = {}
    for x, y, scenes, blob in rows:
        with rasterio.MemoryFile(bytes(blob)) as mem:
            with mem.open() as ds:
                arr, nodata = ds.read(1), ds.nodata
        by_tile.setdefault((x, y), []).append((scenes or "", arr, observed(arr, nodata)))
    out = {}
    for key, parts in by_tile.items():
        if not any(scene in scenes.split("|") for scenes, _, _ in parts):
            continue
        parts.sort(key=lambda p: (-int(p[2].sum()), p[0]))
        arr, seen = parts[0][1], parts[0][2].copy()
        for _, other, obs in parts[1:]:
            take = obs & ~seen
            arr[take] = other[take]
            seen |= take
        out[key] = arr
    return out


def stac_field(item, name):
    """A STAC property an adapter cannot do without, with an error that names the scene."""
    try:
        return item.properties[name]
    except KeyError:
        raise ValueError(f"{getattr(item, 'id', 'STAC item')} has no {name!r}: "
                         f"its acquisition cannot be named") from None


def scene_extent(item, asset_key=None):
    """Native (west, south, east, north) of a scene's raster from the STAC projection
    fields (proj:transform and proj:shape, on the asset or on the item), or None when the
    item does not carry them."""
    asset = getattr(item, "assets", {}).get(asset_key)
    for fields in (getattr(asset, "extra_fields", None), item.properties):
        if fields and "proj:transform" in fields and "proj:shape" in fields:
            t, (rows, cols) = fields["proj:transform"], fields["proj:shape"]
            if t[1] or t[3]:            # a rotated raster: its extent is not this rectangle
                return None
            xs, ys = (t[2], t[2] + t[0] * cols), (t[5], t[5] + t[4] * rows)
            return (min(xs), min(ys), max(xs), max(ys))
    return None


def plan_scene_tiles(grid, bbox, crs, band, date, stored, acq, scene, keep_offzone, in_aoi,
                     extent=None) -> list:
    """plan_tiles for a source with acquisitions: the tiles of `band` this scene still has
    to be read for. `stored` is Writer.existing_scenes: a tile without an empty pixel is
    complete; a partly empty one is skipped only for the scenes it already holds, since
    another scene of the same acquisition can hold the missing part. A tile of another
    acquisition on the same date is a different tile. `extent`, the scene's raster in
    native coordinates (scene_extent), keeps the plan to the tiles the scene can hold;
    without it every tile of the bbox is planned for every scene."""
    nw, ns, ne, nn = bbox_to_crs(bbox, crs)
    if extent is not None:
        nw, ns, ne, nn = max(nw, extent[0]), max(ns, extent[1]), min(ne, extent[2]), min(nn, extent[3])
        if nw >= ne or ns >= nn:
            return []
    out = []
    for tx, ty in grid.tiles_for_bounds(nw, ns, ne, nn):
        held = stored.get((band, date, acq, crs, tx, ty), ())
        if held is None or scene in held:
            continue
        if extent is not None:
            w, s, e, n = grid.tile_bounds(tx, ty)
            if e <= extent[0] or w >= extent[2] or n <= extent[1] or s >= extent[3]:
                continue
        if (keep_offzone or tile_is_canonical(grid, tx, ty, crs)) \
                and in_aoi(wgs84_bounds(grid, tx, ty, crs)):
            out.append((tx, ty))
    return out


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
