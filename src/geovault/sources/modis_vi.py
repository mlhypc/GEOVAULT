"""MODIS vegetation indices, 16-day composites, 250 m (MOD13Q1 Terra + MYD13Q1 Aqua v061) via
Microsoft Planetary Computer. Free, no account (SAS signing, handled by planetary_computer).

STAC collection modis-13Q1-061 holds both satellites, told apart by the `platform` property. Their
composite periods are offset by 8 days (Terra starts on day 1, 17, 33 ...; Aqua on 9, 25, 41 ...),
so together they give an 8-day NDVI series. They are stored as separate bands:

    terra_ndvi  terra_evi  terra_reliability  terra_doy  terra_quality
    aqua_ndvi   aqua_evi   aqua_reliability   aqua_doy   aqua_quality

Values are stored raw (lossless): NDVI and EVI int16 with scale 0.0001 (written into each blob
as GeoTIFF scale, fill -3000), pixel_reliability uint8 (0 good, 1 marginal, 2 snow/ice, 3 cloud,
fill 255 on the PC COGs), composite_day_of_the_year int16 (1-366, fill -1), VI_Quality uint16 bit field (fill
65535). The scene date is the start of the 16-day period. cloud_pct of an NDVI/EVI tile is the
share of fill pixels; a tile that is entirely fill is not stored.

Grid: MODIS sinusoidal (custom sphere R=6371007.181), pixel 231.65635826395834 m, 4800 px scenes
= 20 x 20 tiles of 240 px (55.6 km), (0, 0) anchor at the global origin, deterministic across
scenes and satellites. Turkey: h19-h21 x v04-v05 (h19 only for the far west).

Known gaps on Planetary Computer (2026-10): no items anywhere between 2025-07-12 and 2025-11-01;
duplicate productions of some 2024 Terra scenes (the newest wins at compaction). Latency about
two weeks after the composite period ends. Terra science data is expected to end around 2027;
VIIRS VNP13A1 (Earthdata login) is the continuation.
"""

import os
from concurrent.futures import ThreadPoolExecutor

import planetary_computer as pc
import rasterio
from pystac_client import Client

from ..grid import Grid
from . import common

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "modis-13Q1-061"

CRS = "+proj=sinu +lon_0=0 +x_0=0 +y_0=0 +R=6371007.181 +units=m +no_defs"
RES = 231.65635826395834
TILE_PX = 240                 # 4800 px scene = 20 x 20 tiles, none straddling scenes
VI_SCALE = 0.0001
WORKERS = 4

SATS = {"terra": "terra", "aqua": "aqua"}
# band suffix -> (STAC asset key, nodata, GeoTIFF scale or None)
ASSETS = {
    "ndvi": ("250m_16_days_NDVI", -3000, VI_SCALE),
    "evi": ("250m_16_days_EVI", -3000, VI_SCALE),
    "reliability": ("250m_16_days_pixel_reliability", 255, None),
    "doy": ("250m_16_days_composite_day_of_the_year", -1, None),
    "quality": ("250m_16_days_VI_Quality", 65535, None),
}
ALL_BANDS = [f"{s}_{k}" for s in SATS for k in ASSETS]
DEFAULT_BANDS = [f"{s}_{k}" for s in SATS for k in ("ndvi", "reliability")]

_ENV = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_MULTIPLEX="YES",
            VSI_CACHE="TRUE")
for _k, _v in _ENV.items():
    os.environ.setdefault(_k, _v)


def grid_for() -> Grid:
    return Grid(CRS, RES, TILE_PX, 0.0, 0.0)


def _sat(item) -> str:
    p = str(item.properties.get("platform", "")).lower()
    return p if p in SATS else ("terra" if item.id.startswith("MOD") else "aqua")


def _date(item) -> str:
    return item.properties["datetime"][:10]


def _bands_of(item, bands):
    """(band, asset key, nodata, scale) of this scene's satellite among the requested bands."""
    sat = _sat(item)
    out = []
    for b in bands:
        s, _, k = b.partition("_")
        if s == sat and k in ASSETS and ASSETS[k][0] in item.assets:
            out.append((b, *ASSETS[k]))
    return out


def fill_pct(arr, nodata):
    """Share (0-100) of fill pixels; None if the tile is entirely fill."""
    n = arr.size
    fill = int((arr == nodata).sum())
    if fill == n:
        return None
    return round(fill / n * 100.0, 2)


def search(bbox, start, end, cloud_max=None, limit=None):
    """STAC search -> Terra and Aqua composites, oldest first. Items carry start_datetime
    only; a plain 'datetime' (the period start) is filled in for the CLI."""
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
    grid, date = grid_for(), _date(item)
    in_aoi = tile_filter(aoi)
    specs = _bands_of(item, bands)
    if not specs:
        return 0

    def fetch_band(spec):
        band, key, nodata, scale = spec
        # keep_offzone=True: one global CRS, the UTM zone rule does not apply
        todo = common.plan_tiles(grid, bbox, CRS, band, date, skip_keys, True, in_aoi)
        if not todo:
            return []
        rows = []
        with rasterio.open(item.assets[key].href) as ds:
            if ds.nodata is not None:          # the COG's own fill value wins over the table
                nodata = type(nodata)(ds.nodata)
            for tx, ty in todo:
                arr = common.read_tile_window(ds, grid, tx, ty, nodata)
                if arr is None:
                    continue
                cloud = fill_pct(arr, nodata) if scale is not None else None
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
