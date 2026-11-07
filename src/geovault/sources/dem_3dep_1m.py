"""USGS 3DEP 1 m DEM (bare earth from airborne lidar), United States only.

Official USGS product: 10 x 10 km tiles in the UTM zone of the lidar project (NAD83
family), float32 metres, COG on the public prd-tnm bucket, no account. Public domain.
Tiles are found through The National Map (TNM) Access API. Heights are NAVD88 in the
conterminous US; projects in Hawaii, Alaska and the territories use other vertical
datums, so check the CRS of the source files before mixing regions.

File names come in three generations (USGS_1M_<zone>_x<x>y<y>_..., USGS_one_meter_x<x>y<y>_...,
USGS_1m_x<x>y<y>_...); x * 10 km is the west edge of the cell and y * 10 km its north edge.
When the name carries no zone, it is read from the file's CRS. Each file carries a 6 m
buffer over its neighbours; every tile is cut to its nominal cell, so no pixel is stored
twice. Older files declare a different nodata value; voids are rewritten to NODATA.

Several lidar projects can cover the same cell, often each only in part. Every tile is
taken from the most recently published project (TNM publication date, not the lidar
acquisition year) whose tile is complete; when none is complete, from the one with the
most valid pixels. The project name is recorded as the tile's scene id, and the tile is
georeferenced with the newest file's CRS (projects in one zone may use different NAD83
realisations; the difference is well under a metre).

Projects near a UTM zone line are often delivered in the neighbouring zone. A tile is
stored when it touches the zone of its own CRS: ground within one tile of the line can
then be stored in both zones (the reader picks one), but no strip between the zones is
left uncovered. Tiles lying wholly in another zone are left to that zone's files.

A cell is skipped when any of its project files fails to open, so an older project never
stands in for a newer one that was only temporarily unreachable; the run then ends with
a hint to re-run.
"""

import re
from collections import defaultdict

import numpy as np
import rasterio
import requests
from pyproj import CRS as ProjCRS

from ..aoi import tile_filter
from ..grid import Grid
from ..log import Progress
from . import common, static_cog

STATIC = True
DEFAULT_BANDS = ["DEM"]
RES_LIST = [1.0]
TILE_PX = 500        # divides the 10,000 px of a cell: 20 x 20 tiles
NODATA = -999999.0

_TNM = "https://tnmaccess.nationalmap.gov/api/v1/products"
_DATASET = "Digital Elevation Model (DEM) 1 meter"
_NAME = re.compile(r"USGS_(?:1M_(\d+)_|one_meter_|1m_)x(\d+)y(\d+)_(.+)\.tif$", re.IGNORECASE)
_CELL = 10000.0


def parse_name(url):
    """(utm_zone or None, x, y, project) from a USGS 1 m file name, or None."""
    m = _NAME.search(url.rsplit("/", 1)[-1])
    if not m:
        return None
    zone = int(m.group(1)) if m.group(1) else None
    return zone, int(m.group(2)), int(m.group(3)), m.group(4)


def footprint(x, y):
    """Nominal cell of tile x/y in its UTM CRS: x * 10 km is the west edge, y * 10 km the north."""
    return (x * _CELL, y * _CELL - _CELL, x * _CELL + _CELL, y * _CELL)


def utm_zone(crs):
    """UTM zone number of a rasterio CRS, or None when it is not a UTM projection."""
    zone = ProjCRS.from_wkt(crs.to_wkt()).utm_zone     # e.g. "11N"
    return int(zone[:-1]) if zone else None


def _products(bbox):
    w, s, e, n = bbox
    offset, out = 0, []
    while True:
        r = requests.get(_TNM, params={"datasets": _DATASET, "bbox": f"{w},{s},{e},{n}",
                                       "max": 500, "offset": offset, "prodFormats": "GeoTIFF"},
                         timeout=120)
        r.raise_for_status()
        j = r.json()
        items = j.get("items", [])
        out += items
        offset += len(items)
        if not items or offset >= j.get("total", 0):
            return out


def _touches_aoi(item, aoi):
    """Cheap AOI test on the WGS84 bounding box TNM gives for each file (no remote open)."""
    if aoi is None:
        return True
    b = item.get("boundingBox") or {}
    if not all(k in b for k in ("minX", "minY", "maxX", "maxY")):
        return True
    from shapely.geometry import box
    return aoi.intersects(box(b["minX"], b["minY"], b["maxX"], b["maxY"]))


def cells(bbox, aoi=None, log=print):
    """({(zone, x, y): [(publication date, url, project), ...] newest first}, failed count).
    Files whose TNM bounding box misses the AOI are dropped before anything is opened."""
    out = defaultdict(set)
    failed = 0
    for it in _products(bbox):
        url = it.get("downloadURL", "")
        p = parse_name(url)
        if not p or not _touches_aoi(it, aoi):
            continue
        zone, x, y, project = p
        if zone is None:                      # older names: the zone is only in the file's CRS
            try:
                with rasterio.Env(**static_cog.GDAL_ENV), rasterio.open(url) as ds:
                    zone = utm_zone(ds.crs)
            except rasterio.errors.RasterioIOError as exc:
                failed += 1
                log(f"    {url.rsplit('/', 1)[-1]}: FAILED to open ({exc}), skipped")
                continue
            if zone is None:
                continue
        out[(zone, x, y)].add((it.get("publicationDate") or "", url, project))
    return {k: sorted(v, reverse=True) for k, v in sorted(out.items())}, failed


def _read(ds, grid, tx, ty):
    """Tile from one project file with its voids rewritten to NODATA, or None if empty."""
    arr = common.read_tile_window(ds, grid, tx, ty, NODATA)
    if arr is None:
        return None
    if ds.nodata is not None and ds.nodata != NODATA:
        arr[arr == ds.nodata] = NODATA
    arr[~np.isfinite(arr)] = NODATA
    return None if np.all(arr == NODATA) else arr


def pick(read_fns, nodata):
    """(index, array) to keep, reading lazily newest first: the first complete tile wins;
    when none is complete, the one with the most valid pixels. (None, None) if all empty."""
    best, best_arr, best_valid = None, None, 0
    for i, read in enumerate(read_fns):
        arr = read()
        if arr is None:
            continue
        valid = int(np.count_nonzero(arr != nodata))
        if valid == arr.size:
            return i, arr
        if valid > best_valid:
            best, best_arr, best_valid = i, arr, valid
    return best, best_arr


def touches_zone(wgs84_bounds, zone):
    """True when the tile overlaps the longitude band of UTM zone `zone`."""
    w, s, e, n = wgs84_bounds
    zone_west = -180.0 + (zone - 1) * 6.0
    return w < zone_west + 6.0 and e > zone_west


def _open_all(files, log):
    """Open every project file of a cell. If one fails, close the others and return None:
    choosing among the remaining projects could store an older tile for good."""
    dss = []
    for _, url, project in files:
        try:
            dss.append((rasterio.open(url), project))
        except rasterio.errors.RasterioIOError as exc:
            log(f"    {url.rsplit('/', 1)[-1]}: FAILED to open ({exc}), cell skipped")
            for ds, _ in dss:
                ds.close()
            return None
    return dss


def _cell_tiles(dss, zone, fp, bbox, aoi, in_aoi, skip_keys, writer) -> int:
    """Store the tiles of one 10 km cell that fall inside bbox / AOI."""
    ds0 = dss[0][0]
    crs = f"EPSG:{ds0.crs.to_epsg()}" if ds0.crs.to_epsg() else str(ds0.crs)
    own_tf, _, _ = static_cog.owned_window(ds0.transform, ds0.width, ds0.height, fp)
    grid = Grid.from_transform(crs, own_tf, TILE_PX)
    nw, ns, ne, nn = common.bbox_to_crs(bbox, crs)
    cw, cs, ce, cn = fp
    w, s, e, n = max(cw, nw), max(cs, ns), min(ce, ne), min(cn, nn)
    if w >= e or s >= n:
        return 0
    stored = 0
    for tx, ty in grid.tiles_for_bounds(w, s, e, n):
        if ("DEM", "", crs, tx, ty) in skip_keys:
            continue
        wb = common.wgs84_bounds(grid, tx, ty, crs)
        if not in_aoi(wb) or not touches_zone(wb, zone):
            continue
        reads = [lambda ds=ds: _read(ds, grid, tx, ty) for ds, _ in dss]
        i, arr = pick(reads, NODATA)
        if i is None:
            continue
        blob = common.encode_geotiff(arr, grid, tx, ty, crs, NODATA)
        if writer.add("DEM", "", tx, ty, crs, grid.res, TILE_PX, wb, NODATA, None,
                      f"usgs-3dep-1m:{dss[i][1]}", blob):
            stored += 1
    return stored


def ingest(bbox, writer, bands=None, skip_keys=None, aoi=None, log=print) -> int:
    skip_keys = skip_keys or set()
    in_aoi = tile_filter(aoi)
    todo, failed = cells(bbox, aoi, log)
    log(f"  {len(todo)} cells, {sum(len(v) for v in todo.values())} project files")
    prog = Progress(len(todo))
    written = 0
    for k, ((zone, x, y), files) in enumerate(todo.items(), 1):
        stored = 0
        with rasterio.Env(**static_cog.GDAL_ENV):
            dss = _open_all(files, log)
            if dss is None:
                failed += 1
            else:
                try:
                    stored = _cell_tiles(dss, zone, footprint(x, y), bbox, aoi, in_aoi,
                                         skip_keys, writer)
                finally:
                    for ds, _ in dss:
                        ds.close()
        written += stored
        log(f"    [{k}/{len(todo)}] cell {zone}/x{x}y{y} ({len(files)} projects): "
            f"{stored} tiles  | {prog.tick()}")
    log(f"    DEM: {written} tiles")
    if failed:
        log(f"    DEM: {failed} files or cells failed to open; run the same command again")
    return written
