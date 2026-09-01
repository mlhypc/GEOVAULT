"""Sentinel-2 L2A via AWS earth-search (element84). Anonymous, quota-free.

STAC: https://earth-search.aws.element84.com/v1, collection sentinel-2-l2a.
Pixels: per-band COGs on the sentinel-cogs bucket (AWS open data, no signing).

Bands stay on their native UTM grids (no resampling). SCL is fetched with every
scene; it feeds the per-tile cloud_pct, computed locally with numpy.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import rasterio
from pystac_client import Client

from ..grid import Grid
from . import common

STAC_URL = "https://earth-search.aws.element84.com/v1"
COLLECTION = "sentinel-2-l2a"

# STAC asset key -> (band name, resolution in meters)
ASSETS = {
    "coastal": ("B1", 60), "blue": ("B2", 10), "green": ("B3", 10), "red": ("B4", 10),
    "rededge1": ("B5", 20), "rededge2": ("B6", 20), "rededge3": ("B7", 20),
    "nir": ("B8", 10), "nir08": ("B8A", 20), "nir09": ("B9", 60),
    "swir16": ("B11", 20), "swir22": ("B12", 20), "scl": ("SCL", 20),
}
BAND_TO_ASSET = {v[0]: (k, v[1]) for k, v in ASSETS.items()}
DEFAULT_BANDS = list(BAND_TO_ASSET)
NODATA = 0
TILE_PX = 256
WORKERS = 6   # wave-2 band parallelism; each band opens its own COG handle

_ENV = dict(AWS_NO_SIGN_REQUEST="YES", GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
            GDAL_HTTP_MULTIPLEX="YES", VSI_CACHE="TRUE")


def search(bbox, start, end, cloud_max=None, limit=None):
    """STAC search -> list of scene items, oldest first."""
    client = Client.open(STAC_URL)
    q = {"eo:cloud_cover": {"lte": cloud_max}} if cloud_max is not None else None
    items = list(client.search(
        collections=[COLLECTION], bbox=list(bbox),
        datetime=f"{start}/{end}", query=q,
    ).items())
    items.sort(key=lambda i: i.properties["datetime"])
    return items[:limit] if limit else items


def ingest_scene(item, bbox, writer, bands=None, skip_keys=None, log=print,
                 keep_offzone=False, aoi=None) -> int:
    """Fetch the requested bands of one scene over bbox/aoi; returns tiles written.

    Two waves: SCL first (the cloud_pct source), then the remaining bands in
    parallel (one thread per band, each with its own COG handle). Tiles whose
    center falls outside the scene's own UTM zone are skipped (canonical-zone
    rule) unless keep_offzone is set.
    """
    bands = bands or DEFAULT_BANDS
    skip_keys = skip_keys or set()
    date = datetime.fromisoformat(
        item.properties["datetime"].replace("Z", "+00:00")).strftime("%Y-%m-%d")
    epsg = item.properties.get("proj:epsg") or item.properties.get("proj:code", "").replace("EPSG:", "")
    crs = f"EPSG:{epsg}"
    scene = item.id
    scl = {}
    scl_grid = Grid(crs, 20, TILE_PX)

    from ..aoi import tile_filter
    in_aoi = tile_filter(aoi)

    def band_todo(band, res):
        grid = Grid(crs, res, TILE_PX)
        nw, ns, ne, nn = common.bbox_to_crs(bbox, crs)
        return grid, [
            (tx, ty) for tx, ty in grid.tiles_for_bounds(nw, ns, ne, nn)
            if (band, date, tx, ty) not in skip_keys
            and (keep_offzone or common.tile_is_canonical(grid, tx, ty, crs))
            and in_aoi(common.wgs84_bounds(grid, tx, ty, crs))
        ]

    def fetch_band(band):
        """One band -> list of prepared rows (thread-safe: no writer access)."""
        asset_key, res = BAND_TO_ASSET[band]
        if asset_key not in item.assets:
            return []
        grid, todo = band_todo(band, res)
        rows = []
        if not todo:
            return rows
        with rasterio.open(item.assets[asset_key].href) as ds:
            for tx, ty in todo:
                arr = common.read_tile_window(ds, grid, tx, ty, NODATA)
                if arr is None:
                    continue
                if band == "SCL":
                    scl[(tx, ty)] = arr
                    cloud = common.cloud_pct_from_scl(arr)
                else:
                    cloud = _cloud_for_footprint(scl, scl_grid, grid, tx, ty)
                rows.append((band, date, tx, ty, crs, res, TILE_PX,
                             common.wgs84_bounds(grid, tx, ty, crs), NODATA,
                             cloud, scene,
                             common.encode_geotiff(arr, grid, tx, ty, crs, NODATA)))
        return rows

    written = 0
    with rasterio.Env(**_ENV):
        # Wave 1: SCL (the cloud source).
        if "SCL" in bands:
            for row in fetch_band("SCL"):
                written += 1 if writer.add(*row) else 0
        # If SCL was already cached (skipped), load it from the store so the
        # second wave still gets real cloud_pct values instead of NULLs.
        rest = [b for b in bands if b != "SCL"]
        if rest and not scl:
            _load_scl_from_store(writer.dataset, date, crs, scl_grid, scl)
        # Wave 2: remaining bands in parallel.
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for rows in ex.map(fetch_band, rest):
                for row in rows:
                    written += 1 if writer.add(*row) else 0
    log(f"    {written} tiles")
    return written


def _load_scl_from_store(dataset, date, crs, scl_grid, scl):
    """Fill the SCL tile dict from already-stored SCL blobs for this date/crs."""
    import io
    import duckdb
    import rasterio as rio
    from ..store import month_files
    files = month_files(dataset, 20, date)
    if not files:
        return
    lst = ", ".join(f"'{f.as_posix()}'" for f in files)
    con = duckdb.connect()
    rows = con.execute(
        f"SELECT x, y, data FROM read_parquet([{lst}], union_by_name=true) "
        f"WHERE band='SCL' AND date=? AND crs=?", [date, crs]).fetchall()
    con.close()
    for x, y, blob in rows:
        with rio.MemoryFile(io.BytesIO(bytes(blob))) as mem:
            with mem.open() as ds:
                scl[(x, y)] = ds.read(1)


def _cloud_for_footprint(scl, scl_grid, grid, tx, ty):
    """cloud_pct for a band tile, sliced from cached SCL tiles over the same ground."""
    import numpy as np
    w, s, e, n = grid.tile_bounds(tx, ty)
    parts = []
    for (sx, sy), arr in scl.items():
        sw, ss, se, sn = scl_grid.tile_bounds(sx, sy)
        iw, is_, ie, in_ = max(w, sw), max(s, ss), min(e, se), min(n, sn)
        if iw >= ie or is_ >= in_:
            continue
        c0 = int(round((iw - sw) / 20)); c1 = int(round((ie - sw) / 20))
        r0 = int(round((sn - in_) / 20)); r1 = int(round((sn - is_) / 20))
        parts.append(arr[r0:r1, c0:c1].ravel())
    if not parts:
        return None
    return common.cloud_pct_from_scl(np.concatenate(parts))
