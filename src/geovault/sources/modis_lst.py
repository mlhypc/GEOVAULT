"""MODIS land surface temperature, daily, 1 km (MOD11A1 + MYD11A1 v061) via
Microsoft Planetary Computer. Free, no account.

STAC collection modis-11A1-061 holds BOTH satellites: Terra (MOD11A1, ~10:30 /
22:30 local overpass) and Aqua (MYD11A1, ~13:30 / 01:30). Same day, same tile,
different physics (Aqua's early-afternoon pass is near the daily maximum), so
they are kept as separate bands rather than merged:

    terra_lst_day  terra_lst_night  terra_qc_day  terra_qc_night
    aqua_lst_day   aqua_lst_night   aqua_qc_day   aqua_qc_night
    (+ *_view_time_day / *_view_time_night on request)

LST is stored raw (uint16, lossless): kelvin = DN * 0.02, written into every
blob as GeoTIFF scale so readers get it from ds.scales[0]. DN 0 = no retrieval
(cloud, or no overpass). cloud_pct per LST tile is the share of DN 0 pixels,
computed locally, the role SCL/qa_pixel play for the optical datasets. QC bands
carry the LP DAAC quality bits and no cloud value. A tile that is 100% DN 0
(fully cloudy) is not stored, like an S2 tile outside its granule; re-runs will
look at it again, --dry-run reports it as a residue.

Grid: MODIS sinusoidal (custom sphere R=6371007.181), pixel 926.625433 m, the
global origin at -20015109.354 m is an exact multiple of the 1200 px scene size,
which is 5 x 240 px tiles, so a (0, 0) anchor is deterministic across all
scenes and both satellites. Tiles are 222 km. Turkey is covered by MODIS tiles
h20v04, h20v05, h21v04, h21v05: 8 scenes per day (4 tiles x 2 satellites).

Latency: Planetary Computer is 2-3 weeks behind (measured 2026-09-14: latest
Terra scene 2026-08-28).
"""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import planetary_computer as pc
import rasterio
from pystac_client import Client

from ..grid import Grid
from . import common

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "modis-11A1-061"

# MODIS sinusoidal as a proj string: the products carry a custom WKT with no EPSG code.
CRS = "+proj=sinu +lon_0=0 +x_0=0 +y_0=0 +R=6371007.181 +units=m +no_defs"
RES = 926.625433055833
TILE_PX = 240                 # 1200 px scene = 5 x 5 tiles, none straddling scenes
LST_SCALE = 0.02
NODATA = 0
WORKERS = 4

SATS = {"MOD": "terra", "MYD": "aqua"}
# band suffix -> STAC asset key
ASSETS = {
    "lst_day": "LST_Day_1km",
    "lst_night": "LST_Night_1km",
    "qc_day": "QC_Day",
    "qc_night": "QC_Night",
    "view_time_day": "Day_view_time",
    "view_time_night": "Night_view_time",
}
ALL_BANDS = [f"{s}_{k}" for s in SATS.values() for k in ASSETS]
DEFAULT_BANDS = [f"{s}_{k}" for s in SATS.values()
                 for k in ("lst_day", "lst_night", "qc_day", "qc_night")]

_ENV = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_MULTIPLEX="YES",
            VSI_CACHE="TRUE")
for _k, _v in _ENV.items():
    os.environ.setdefault(_k, _v)


def grid_for() -> Grid:
    return Grid(CRS, RES, TILE_PX, 0.0, 0.0)


def _sat(item) -> str:
    return SATS.get(item.id[:3], item.id[:3].lower())


def _date(item) -> str:
    return item.properties["datetime"][:10]


def _bands_of(item, bands):
    """(band, asset key) pairs of this scene's satellite among the requested bands."""
    sat = _sat(item)
    out = []
    for b in bands:
        s, _, k = b.partition("_")
        if s == sat and k in ASSETS and ASSETS[k] in item.assets:
            out.append((b, ASSETS[k]))
    return out


def cloud_pct_from_lst(arr):
    """Share (0-100) of no-retrieval pixels; None if the tile is entirely DN 0."""
    n = arr.size
    zeros = int((arr == NODATA).sum())
    if zeros == n:
        return None
    return round(zeros / n * 100.0, 2)


def search(bbox, start, end, cloud_max=None, limit=None):
    """STAC search -> Terra and Aqua daily scenes, oldest first. Items carry
    start_datetime only; a plain 'datetime' is filled in for the CLI."""
    client = Client.open(STAC_URL, modifier=pc.sign_inplace)
    items = list(client.search(
        collections=[COLLECTION], bbox=list(bbox), datetime=f"{start}/{end}",
    ).items())
    for i in items:
        if not i.properties.get("datetime"):
            i.properties["datetime"] = i.properties["start_datetime"]
    items.sort(key=lambda i: (i.properties["datetime"], i.id))
    return items[:limit] if limit else items


def plan_scene(item, bbox, bands=None, skip_keys=None, keep_offzone=False, aoi=None) -> dict:
    from ..aoi import tile_filter
    bands = bands or DEFAULT_BANDS
    skip_keys = skip_keys or set()
    grid, date = grid_for(), _date(item)
    in_aoi = tile_filter(aoi)
    out = {}
    for band, _ in _bands_of(item, bands):
        n = len(common.plan_tiles(grid, bbox, CRS, band, date, skip_keys, True, in_aoi))
        if n:
            out[band] = n
    return out


def ingest_scene(item, bbox, writer, bands=None, skip_keys=None, log=print,
                 keep_offzone=False, aoi=None) -> int:
    """Fetch the requested bands of one scene over bbox/aoi (bands in parallel)."""
    from ..aoi import tile_filter
    bands = bands or DEFAULT_BANDS
    skip_keys = skip_keys or set()
    grid, date = grid_for(), _date(item)
    in_aoi = tile_filter(aoi)
    pairs = _bands_of(item, bands)
    if not pairs:
        return 0

    def fetch_band(pair):
        band, key = pair
        # keep_offzone=True: one global CRS, the UTM zone rule does not apply
        todo = common.plan_tiles(grid, bbox, CRS, band, date, skip_keys, True, in_aoi)
        if not todo:
            return []
        is_lst = "lst_" in band
        rows = []
        with rasterio.open(item.assets[key].href) as ds:
            for tx, ty in todo:
                arr = common.read_tile_window(ds, grid, tx, ty, NODATA)
                if arr is None:
                    continue
                cloud = cloud_pct_from_lst(arr) if is_lst else None
                blob = common.encode_geotiff(arr, grid, tx, ty, CRS, NODATA,
                                             LST_SCALE if is_lst else None, None)
                rows.append((band, date, tx, ty, CRS, RES, TILE_PX,
                             common.wgs84_bounds(grid, tx, ty, CRS), NODATA, cloud, item.id,
                             blob, int((arr != NODATA).sum())))
        return rows

    written = 0
    with rasterio.Env(**_ENV), ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for rows in ex.map(fetch_band, pairs):
            for row in rows:
                written += 1 if writer.add(*row) else 0
    log(f"    {written} tiles")
    return written
