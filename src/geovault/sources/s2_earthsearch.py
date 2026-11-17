"""Sentinel-2 L2A via AWS earth-search (element84). Anonymous, quota-free.

STAC: https://earth-search.aws.element84.com/v1, collection sentinel-2-l2a.
Pixels: per-band COGs on the sentinel-cogs bucket (AWS open data, no signing).

Bands stay on their native UTM grids (no resampling). SCL is fetched with every
scene; it feeds the per-tile cloud_pct, computed locally with numpy.
"""

from concurrent.futures import ThreadPoolExecutor
import re

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

import os
_ENV = dict(AWS_NO_SIGN_REQUEST="YES", GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
            GDAL_HTTP_MULTIPLEX="YES", VSI_CACHE="TRUE")
for _k, _v in _ENV.items():
    os.environ.setdefault(_k, _v)   # visible to every thread (rasterio.Env is thread-local)



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


ACQUISITIONS = True      # tiles carry `acq`; the CLI plans with Writer.existing_scenes


def acquisition(item) -> str:
    """The datatake a granule belongs to: one overpass of one satellite. The processing
    baseline at the end of the datatake id is dropped, so a reprocessed granule stays in
    the acquisition of the granules stored before."""
    return re.sub(r"_N\d+\.\d+$", "", common.stac_field(item, "s2:datatake_id"))


def _todo(item, band, bbox, stored, keep_offzone, in_aoi):
    """(grid, tiles of `band` this scene still has to be read for)."""
    asset_key, res = BAND_TO_ASSET[band]
    date, crs = common.scene_date_crs(item)
    grid = Grid(crs, res, TILE_PX)
    return grid, common.plan_scene_tiles(
        grid, bbox, crs, band, date, stored, acquisition(item), item.id, keep_offzone, in_aoi,
        extent=common.scene_extent(item, asset_key))


def ingest_scene(item, bbox, writer, bands=None, skip_keys=None, log=print,
                 keep_offzone=False, aoi=None) -> int:
    """Fetch the requested bands of one scene over bbox/aoi; returns tiles written.
    skip_keys is Writer.existing_scenes: what the store already holds.

    Two waves: SCL first (the cloud_pct source), then the remaining bands in
    parallel (one thread per band, each with its own COG handle). Granules of
    neighbouring MGRS zones overlap: a tile that does not touch the zone of the
    scene's CRS is left to the other zone's granule (common.tile_is_canonical)
    unless keep_offzone is set.
    """
    bands = bands or DEFAULT_BANDS
    skip_keys = skip_keys or {}
    date, crs = common.scene_date_crs(item)
    scene, acq = item.id, acquisition(item)
    scl = {}
    scl_grid = Grid(crs, 20, TILE_PX)

    from ..aoi import tile_filter
    in_aoi = tile_filter(aoi)

    def fetch_band(band):
        """One band -> list of prepared rows (thread-safe: no writer access)."""
        asset_key, res = BAND_TO_ASSET[band]
        if asset_key not in item.assets:
            return []
        grid, todo = _todo(item, band, bbox, skip_keys, keep_offzone, in_aoi)
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
                             common.encode_geotiff(arr, grid, tx, ty, crs, NODATA),
                             int((arr != NODATA).sum())))   # valid_px
        return rows

    written = 0
    with rasterio.Env(**_ENV):
        # Wave 1: SCL (the cloud source).
        if "SCL" in bands:
            for row in fetch_band("SCL"):
                written += 1 if writer.add(*row, acq=acq) else 0
        # If any SCL tile of this acquisition was already cached (skipped), load the
        # stored SCL so the second wave still gets real cloud_pct values instead
        # of NULLs. Checking `not scl` alone is wrong: a partially overlapping
        # AOI fetches some SCL tiles and skips others, and the 10 m tiles under
        # the skipped ones would get None.
        rest = [b for b in bands if b != "SCL"]
        scl_skipped = any(k[0] == "SCL" and k[1] == date and k[2] == acq for k in skip_keys)
        if rest and (not scl or scl_skipped):
            _load_scl_from_store(writer.dataset, date, crs, acq, scene, scl)
        # Wave 2: remaining bands in parallel.
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for rows in ex.map(fetch_band, rest):
                for row in rows:
                    written += 1 if writer.add(*row, acq=acq) else 0
    log(f"    {written} tiles")
    return written


def plan_scene(item, bbox, bands=None, skip_keys=None, keep_offzone=False, aoi=None) -> dict:
    """band -> number of tiles this scene would still fetch (nothing is read).
    Tiles inside the granule's raster that hold no data (beyond the edge of the swath)
    are counted too: they can never be stored, so a small residue on granule edges is
    normal after a full ingest."""
    from ..aoi import tile_filter
    bands = bands or DEFAULT_BANDS
    skip_keys = skip_keys or {}
    in_aoi = tile_filter(aoi)
    out = {}
    for band in bands:
        if BAND_TO_ASSET[band][0] not in item.assets:
            continue
        n = len(_todo(item, band, bbox, skip_keys, keep_offzone, in_aoi)[1])
        if n:
            out[band] = n
    return out


def _load_scl_from_store(dataset, date, crs, acq, scene, scl):
    """Fill the SCL tile dict from the stored SCL tiles that hold this scene."""
    fresh = set(scl)        # fetched this run: wins over the stored copy
    for (x, y), arr in common.stored_tiles(dataset, 20, "SCL", date, crs, acq, scene).items():
        if (x, y) not in fresh:
            scl[(x, y)] = arr


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
