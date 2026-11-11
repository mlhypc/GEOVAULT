"""MODIS burned area, monthly, 500 m (MCD64A1 v061, Terra + Aqua combined) via Microsoft
Planetary Computer. Free, no account.

STAC collection modis-64A1-061: one file per month and scene, dated with the first day of
the month. Bands, stored raw (lossless):

    burn_date    int16 day of the year the pixel burned (1-366); 0 unburned, -1 unmapped
                 (too few valid observations), -2 water
    uncertainty  uint8 uncertainty of the burn date in days; 0 where unburned
    qa           uint8 bit field (valid data, shortened mapping period, relabelled grid cell)
    first_day    int16 first day of the year of reliable change detection; -1 unmapped, -2 water
    last_day     int16 last day of the year of reliable change detection; -1 unmapped, -2 water

A tile is stored when its burn_date holds at least one pixel other than "unmapped", and then
with all requested bands; a tile that is entirely unmapped is not stored, re-runs look at it
again and --dry-run reports it as a residue. cloud_pct of a burn_date tile is the share of
its pixels left unmapped.

Grid: MODIS sinusoidal, pixel 463.312717 m, 240 px tiles (111 km), (0, 0) anchor; a 2400 px
scene is 10 x 10 whole tiles.

The product maps burns by the change they leave in daily surface reflectance, so it sees
fires that burned an area, not every ignition: small and patchy burns, such as many
cropland fires, are under-detected. A month is published two to three months after it ends
(measured 2026-11-11: newest month August 2026). The producer can revise a month later; a
month already in the store is skipped on re-runs, so a revision is not picked up.
The Planetary Computer catalogue has gaps (checked 2026-11-11 for 2020 onward: September
2023, July 2024, May 2025 and August to October 2025 are absent). A search over a gap
returns no scenes, so neither the ingest nor --dry-run can report it.
"""

import os
from concurrent.futures import ThreadPoolExecutor

import planetary_computer as pc
import rasterio
from pystac_client import Client

from ..grid import Grid
from . import common, modis_sinu

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "modis-64A1-061"

CRS = modis_sinu.CRS
RES = 463.3127165279165
TILE_PX = 240                 # 2400 px scene = 10 x 10 tiles, none straddling scenes
UNMAPPED = -1
WORKERS = 4

# band -> (STAC asset key, nodata)
ASSETS = {
    "burn_date": ("Burn_Date", UNMAPPED),
    "uncertainty": ("Burn_Date_Uncertainty", None),
    "qa": ("QA", None),
    "first_day": ("First_Day", UNMAPPED),
    "last_day": ("Last_Day", UNMAPPED),
}
DATE_KEY = ASSETS["burn_date"][0]
ALL_BANDS = list(ASSETS)
DEFAULT_BANDS = ALL_BANDS

_ENV = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_MULTIPLEX="YES",
            VSI_CACHE="TRUE")
for _k, _v in _ENV.items():
    os.environ.setdefault(_k, _v)


def grid_for() -> Grid:
    return Grid(CRS, RES, TILE_PX, 0.0, 0.0)


def _date(item) -> str:
    return item.properties["start_datetime"][:10]


def _bands_of(item, bands) -> list:
    """Requested bands this scene carries."""
    return [band for band in bands if band in ASSETS and ASSETS[band][0] in item.assets]


def unmapped_pct(burn_date) -> float:
    """Share (0-100) of a burn_date tile's pixels left unmapped."""
    return round(float((burn_date == UNMAPPED).sum()) / burn_date.size * 100.0, 2)


def search(bbox, start, end, cloud_max=None, limit=None):
    """STAC search -> monthly files, oldest first, newest production of each. Items carry
    start_datetime only; a plain 'datetime' is filled in for the CLI."""
    client = Client.open(STAC_URL, modifier=pc.sign_inplace)
    items = list(client.search(
        collections=[COLLECTION], bbox=list(bbox), datetime=f"{start}/{end}",
    ).items())
    items = modis_sinu.newest_productions(items)
    for item in items:
        if not item.properties.get("datetime"):
            item.properties["datetime"] = item.properties["start_datetime"]
    items.sort(key=lambda i: (i.properties["datetime"], i.id))
    return items[:limit] if limit else items


def _todo(item, bbox, bands, skip_keys, aoi) -> dict:
    """{band: [(tx, ty), ...]} still to fetch from this scene: tiles of the scene inside
    the bbox and the AOI, minus what the store holds."""
    from ..aoi import tile_filter
    grid, month = grid_for(), _date(item)
    in_scene = set(modis_sinu.scene_tiles(grid, *modis_sinu.scene_of(item)))
    in_aoi = tile_filter(aoi)
    out = {}
    for band in _bands_of(item, bands):
        # keep_offzone=True: one global CRS, the UTM zone rule does not apply
        tiles = common.plan_tiles(grid, bbox, CRS, band, month, skip_keys, True, in_aoi)
        tiles = [t for t in tiles if t in in_scene]
        if tiles:
            out[band] = tiles
    return out


def plan_scene(item, bbox, bands=None, skip_keys=None, keep_offzone=False, aoi=None) -> dict:
    todo = _todo(item, bbox, bands or DEFAULT_BANDS, skip_keys or set(), aoi)
    return {band: len(tiles) for band, tiles in todo.items()}


def ingest_scene(item, bbox, writer, bands=None, skip_keys=None, log=print,
                 keep_offzone=False, aoi=None) -> int:
    """Fetch the requested bands of one monthly file over bbox/aoi: one read per asset,
    tiles cut locally."""
    grid, month = grid_for(), _date(item)
    h, v = modis_sinu.scene_of(item)
    todo = _todo(item, bbox, bands or DEFAULT_BANDS, skip_keys or set(), aoi)
    tiles = set()
    for band_tiles in todo.values():
        tiles.update(band_tiles)
    if not tiles:
        return 0
    tiles = sorted(tiles)

    # burn_date decides which tiles were mapped, so it is read even when it is not among
    # the bands still to fetch.
    keys = {DATE_KEY}
    for band in todo:
        keys.add(ASSETS[band][0])

    def read(key):
        with rasterio.open(item.assets[key].href) as ds:
            return key, modis_sinu.read_block(ds, grid, h, v, tiles)

    with rasterio.Env(**_ENV), ThreadPoolExecutor(max_workers=WORKERS) as ex:
        blocks = dict(ex.map(read, sorted(keys)))

    written = 0
    for band, band_tiles in todo.items():
        key, nodata = ASSETS[band]
        for tx, ty in band_tiles:
            burn_date = modis_sinu.cut_tile(blocks[DATE_KEY], grid, h, v, tx, ty)
            if (burn_date == UNMAPPED).all():
                continue              # nothing mapped this month: nothing to store
            if key == DATE_KEY:
                arr = burn_date
            else:
                arr = modis_sinu.cut_tile(blocks[key], grid, h, v, tx, ty)
            cloud = unmapped_pct(burn_date) if band == "burn_date" else None
            valid = arr.size if nodata is None else int((arr != nodata).sum())
            blob = common.encode_geotiff(arr, grid, tx, ty, CRS, nodata, None, None)
            added = writer.add(band, month, tx, ty, CRS, RES, TILE_PX,
                               common.wgs84_bounds(grid, tx, ty, CRS), nodata, cloud,
                               item.id, blob, valid)
            written += 1 if added else 0
    log(f"    {written} tiles")
    return written
