"""MODIS vegetation indices at 1 km, read from the overview pyramids of the 250 m MOD13Q1 /
MYD13Q1 COGs on Microsoft Planetary Computer. Free, no account. Same scenes, same search and
same tile numbering as `modis_vi`, 16x fewer pixels and about 9x less download: the budget
option when a question only needs "does this km green up and get cut", not field detail.

The PC COGs carry internal overviews at 2x, 4x, 8x and 16x. Level 1 (4x, 926.6 m, 1200 px
scenes) is read here. GDAL builds those overviews as nodata-aware averages: the fill value
(-3000) is excluded, so a 1 km pixel is the mean of its valid 250 m pixels and stays within
about 0.008 NDVI of a true 4 x 4 mean (checked 2026-11-03). The reliability band is a category
code whose overview average is meaningless; its full-resolution window (0.5 MB per scene) is
read instead and summarised per 1 km pixel as `good`: the share (0-100) of the 16 underlying
250 m pixels rated good or marginal (pixel_reliability 0 or 1), 255 when all 16 are fill.

Bands (per satellite, `terra_` / `aqua_` prefix):
    ndvi  int16, scale 0.0001, fill -3000      evi  same
    good  uint8 0-100, fill 255
Default: ndvi + good for both satellites. The scene date is the start of the 16-day period.

Grid: MODIS sinusoidal, pixel 926.6254330558334 m (4 x 231.656...), tiles of 60 px (55.6 km):
the SAME tile footprint and (x, y) numbering as `modis_vi`, so a tile of either dataset covers
the same ground. Known gaps and the Terra/Aqua end of life are those of `modis_vi`.
"""

import os
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import rasterio

from ..grid import Grid
from . import common
from . import modis_vi as base

STAC_URL = base.STAC_URL
COLLECTION = base.COLLECTION
CRS = base.CRS
FACTOR = 4                               # overview level 1 = 4x; level 0 would be 2x (463 m)
OVERVIEW_LEVEL = 1
RES = base.RES * FACTOR                  # 926.6254330558334 m
TILE_PX = base.TILE_PX // FACTOR         # 60 px, same 55.6 km footprint as modis_vi tiles
VI_SCALE = base.VI_SCALE
GOOD_NODATA = 255
WORKERS = 4

SATS = base.SATS
# band suffix -> (STAC asset key, nodata, GeoTIFF scale or None)
ASSETS = {
    "ndvi": base.ASSETS["ndvi"],
    "evi": base.ASSETS["evi"],
    "good": (base.ASSETS["reliability"][0], GOOD_NODATA, None),
}
ALL_BANDS = [f"{s}_{k}" for s in SATS for k in ASSETS]
DEFAULT_BANDS = [f"{s}_{k}" for s in SATS for k in ("ndvi", "good")]

_ENV = base._ENV
for _k, _v in _ENV.items():
    os.environ.setdefault(_k, _v)

search = base.search
fill_pct = base.fill_pct


def grid_for() -> Grid:
    return Grid(CRS, RES, TILE_PX, 0.0, 0.0)


def _bands_of(item, bands):
    """(band, asset key, nodata, scale) of this scene's satellite among the requested bands."""
    sat = base._sat(item)
    out = []
    for b in bands:
        s, _, k = b.partition("_")
        if s == sat and k in ASSETS and ASSETS[k][0] in item.assets:
            out.append((b, *ASSETS[k]))
    return out


def good_share(rel, rel_nodata) -> np.ndarray:
    """Per 1 km pixel, the share (0-100) of its FACTOR x FACTOR reliability pixels rated 0 or 1;
    GOOD_NODATA where every one of them is fill."""
    h, w = rel.shape
    blocks = rel.reshape(h // FACTOR, FACTOR, w // FACTOR, FACTOR)
    valid = blocks != rel_nodata
    good = valid & (blocks <= 1)
    n_valid = valid.sum(axis=(1, 3))
    out = np.full((h // FACTOR, w // FACTOR), GOOD_NODATA, dtype=np.uint8)
    has = n_valid > 0
    out[has] = np.rint(good.sum(axis=(1, 3))[has] / n_valid[has] * 100.0).astype(np.uint8)
    return out


def plan_scene(item, bbox, bands=None, skip_keys=None, keep_offzone=False, aoi=None) -> dict:
    from ..aoi import tile_filter
    bands = bands or DEFAULT_BANDS
    skip_keys = skip_keys or set()
    grid, date = grid_for(), base._date(item)
    in_aoi = tile_filter(aoi)
    out = {}
    for band, *_ in _bands_of(item, bands):
        n = len(common.plan_tiles(grid, bbox, CRS, band, date, skip_keys, True, in_aoi))
        if n:
            out[band] = n
    return out


def ingest_scene(item, bbox, writer, bands=None, skip_keys=None, log=print,
                 keep_offzone=False, aoi=None) -> int:
    """Fetch the requested bands of one composite over bbox/aoi (bands in parallel)."""
    from ..aoi import tile_filter
    bands = bands or DEFAULT_BANDS
    skip_keys = skip_keys or set()
    grid, full, date = grid_for(), base.grid_for(), base._date(item)
    in_aoi = tile_filter(aoi)
    specs = _bands_of(item, bands)
    if not specs:
        return 0

    def fetch_band(spec):
        band, key, nodata, scale = spec
        todo = common.plan_tiles(grid, bbox, CRS, band, date, skip_keys, True, in_aoi)
        if not todo:
            return []
        rows = []
        href = item.assets[key].href
        if band.endswith("_good"):
            # full-resolution reliability, summarised per 1 km pixel
            with rasterio.open(href) as ds:
                rel_nodata = np.dtype(ds.dtypes[0]).type(ds.nodata) if ds.nodata is not None \
                    else np.uint8(255)
                for tx, ty in todo:
                    rel = common.read_tile_window(ds, full, tx, ty, rel_nodata)
                    if rel is None:
                        continue
                    arr = good_share(rel, rel_nodata)
                    if np.all(arr == GOOD_NODATA):
                        continue
                    blob = common.encode_geotiff(arr, grid, tx, ty, CRS, GOOD_NODATA, None, None)
                    rows.append((band, date, tx, ty, CRS, RES, TILE_PX,
                                 common.wgs84_bounds(grid, tx, ty, CRS), GOOD_NODATA, None,
                                 item.id, blob, int((arr != GOOD_NODATA).sum())))
            return rows
        with rasterio.open(href, overview_level=OVERVIEW_LEVEL) as ds:
            if ds.nodata is not None:
                nodata = type(nodata)(ds.nodata)
            for tx, ty in todo:
                arr = common.read_tile_window(ds, grid, tx, ty, nodata)
                if arr is None:
                    continue
                cloud = fill_pct(arr, nodata)
                blob = common.encode_geotiff(arr, grid, tx, ty, CRS, nodata, scale, None)
                rows.append((band, date, tx, ty, CRS, RES, TILE_PX,
                             common.wgs84_bounds(grid, tx, ty, CRS), nodata, cloud, item.id,
                             blob, int((arr != nodata).sum())))
        return rows

    written = 0
    with rasterio.Env(**_ENV), ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for rows in ex.map(fetch_band, specs):
            for row in rows:
                written += 1 if writer.add(*row) else 0
    log(f"    {written} tiles")
    return written
