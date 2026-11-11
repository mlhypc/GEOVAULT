"""MODIS thermal anomalies and fire, daily, 1 km (MOD14A1 Terra + MYD14A1 Aqua v061) via
Microsoft Planetary Computer. Free, no account.

STAC collection modis-14A1-061 holds both satellites, told apart by the product prefix of the
item id (MOD = Terra, MYD = Aqua; older items also carry a `platform` property). The product
is daily but published in 8-day files: every asset is a raster with one band per day. A
file normally holds 8 days; one written during a satellite outage holds fewer, and its
`Dates` tag names the days its bands stand for. Each day is stored as its own tile, dated
with that day; a day without a band is not stored.
Terra and Aqua are separate bands:

    terra_fire_mask  terra_max_frp  terra_qa  terra_sample
    aqua_fire_mask   aqua_max_frp   aqua_qa   aqua_sample

Values are stored raw (lossless):

    fire_mask  uint8 class: 0-2 not processed, 3 water, 4 cloud, 5 land without fire,
               6 unknown, 7 / 8 / 9 fire of low / nominal / high confidence; nodata 0
    max_frp    int32, maximum fire radiative power, MW = DN * 0.1 (written into each blob as
               GeoTIFF scale); 0 where there is no fire. The files tag 0 as nodata; here it
               is stored as a value, so a reader sees "no fire" and not a gap
    qa         uint8 bit field (land / water state, day / night)
    sample     uint16 position of the fire pixel within its scan line; 0 where there is no fire

A tile-day is stored when its fire mask holds at least one processed pixel, and then with all
requested bands, so a max_frp tile that is entirely 0 means "observed, no fire". A tile-day
whose fire mask is entirely "not processed" (no acquisition, missing input) is not stored;
re-runs look at it again and --dry-run reports it as a residue. cloud_pct of a fire mask tile
is the share of its pixels without a fire observation: cloud, unknown or not processed.

A file is always stored whole: an ingest whose date range cuts an 8-day file stores all of
that file's days. The last file of a year runs into January and the first file of the next
year starts on 1 January, so those first days come from two files with the same pixels; one
tile per day is kept.

Grid: MODIS sinusoidal, pixel 926.625433 m, 240 px tiles (222 km), (0, 0) anchor; a 1200 px
scene is 5 x 5 whole tiles. The tile footprint and numbering are those of `modis_lst`.

The daily fire mask keeps the most confident class of the day's overpasses and is a 1 km
summary: small, short or cloud-covered fires are missed. Planetary Computer publishes a
file one to two weeks after its 8 days end (measured 2026-11-11: newest file 24-31 October).
Its catalogue has gaps: a few single 8-day files are absent in most years, and there is
no file at all between 4 July and 24 November 2025 (checked 2026-11-11). A search over a gap
returns no scenes, so neither the ingest nor --dry-run can report it.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import numpy as np
import planetary_computer as pc
import rasterio
from pystac_client import Client

from ..grid import Grid
from . import common, modis_sinu

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "modis-14A1-061"

CRS = modis_sinu.CRS
RES = 926.625433055833
TILE_PX = 240                 # 1200 px scene = 5 x 5 tiles, none straddling scenes
FRP_SCALE = 0.1
WORKERS = 4

SATS = ("terra", "aqua")
# band suffix -> (STAC asset key, nodata, GeoTIFF scale or None)
ASSETS = {
    "fire_mask": ("FireMask", 0, None),
    "max_frp": ("MaxFRP", None, FRP_SCALE),
    "qa": ("QA", None, None),
    "sample": ("sample", None, None),
}
MASK_KEY = ASSETS["fire_mask"][0]
NOT_PROCESSED = (0, 1, 2)                 # fire mask classes of a pixel that was not processed
NOT_OBSERVED = NOT_PROCESSED + (4, 6)     # ... plus cloud and unknown: no fire observation
ALL_BANDS = [f"{sat}_{suffix}" for sat in SATS for suffix in ASSETS]
DEFAULT_BANDS = ALL_BANDS

_ENV = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_MULTIPLEX="YES",
            VSI_CACHE="TRUE")
for _k, _v in _ENV.items():
    os.environ.setdefault(_k, _v)


def grid_for() -> Grid:
    return Grid(CRS, RES, TILE_PX, 0.0, 0.0)


def _sat(item) -> str:
    platform = str(item.properties.get("platform", "")).lower()
    if platform in SATS:
        return platform
    return "terra" if item.id.startswith("MOD") else "aqua"


def layer_dates(item) -> list:
    """Dates (YYYY-MM-DD) of the days a file covers. Used to plan a scene without opening
    it; the days its bands really hold come from the file itself (band_dates)."""
    first = date.fromisoformat(item.properties["start_datetime"][:10])
    last = date.fromisoformat(item.properties["end_datetime"][:10])
    return [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]


def band_dates(ds, planned) -> list:
    """Date of each band of an open asset, from its `Dates` tag. A file without the tag
    must hold exactly the planned days, in order."""
    tagged = ds.tags().get("Dates", "").split()
    if tagged:
        if len(tagged) != ds.count:
            raise ValueError(f"{ds.count} bands but {len(tagged)} dates in the Dates tag")
        return tagged
    if ds.count != len(planned):
        raise ValueError(f"{ds.count} bands for {len(planned)} days and no Dates tag")
    return list(planned)


def _suffixes_of(item, bands) -> list:
    """Band suffixes of this scene's satellite among the requested bands."""
    sat = _sat(item)
    out = []
    for band in bands:
        band_sat, _, suffix = band.partition("_")
        if band_sat == sat and suffix in ASSETS and ASSETS[suffix][0] in item.assets:
            out.append(suffix)
    return out


def is_processed(mask) -> bool:
    """True when a fire mask tile holds at least one processed pixel."""
    return not np.isin(mask, NOT_PROCESSED).all()


def unobserved_pct(mask) -> float:
    """Share (0-100) of fire mask pixels without a fire observation."""
    return round(float(np.isin(mask, NOT_OBSERVED).sum()) / mask.size * 100.0, 2)


def search(bbox, start, end, cloud_max=None, limit=None):
    """STAC search -> Terra and Aqua 8-day files, oldest first, newest production of each.
    Items carry start_datetime only; a plain 'datetime' is filled in for the CLI."""
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
    """{band: {date: [(tx, ty), ...]}} still to fetch from this scene: tiles of the scene
    inside the bbox and the AOI, for every daily layer, minus what the store holds."""
    from ..aoi import tile_filter
    grid = grid_for()
    in_scene = set(modis_sinu.scene_tiles(grid, *modis_sinu.scene_of(item)))
    in_aoi = tile_filter(aoi)
    sat = _sat(item)
    out = {}
    for suffix in _suffixes_of(item, bands):
        band = f"{sat}_{suffix}"
        per_date = {}
        for day in layer_dates(item):
            # keep_offzone=True: one global CRS, the UTM zone rule does not apply
            tiles = common.plan_tiles(grid, bbox, CRS, band, day, skip_keys, True, in_aoi)
            tiles = [t for t in tiles if t in in_scene]
            if tiles:
                per_date[day] = tiles
        if per_date:
            out[band] = per_date
    return out


def plan_scene(item, bbox, bands=None, skip_keys=None, keep_offzone=False, aoi=None) -> dict:
    todo = _todo(item, bbox, bands or DEFAULT_BANDS, skip_keys or set(), aoi)
    return {band: sum(len(tiles) for tiles in per_date.values())
            for band, per_date in todo.items()}


def ingest_scene(item, bbox, writer, bands=None, skip_keys=None, log=print,
                 keep_offzone=False, aoi=None) -> int:
    """Fetch the requested bands of one 8-day file over bbox/aoi: one read per asset, then
    one tile per day cut locally."""
    grid = grid_for()
    h, v = modis_sinu.scene_of(item)
    days = layer_dates(item)
    todo = _todo(item, bbox, bands or DEFAULT_BANDS, skip_keys or set(), aoi)

    tiles = set()
    for per_date in todo.values():
        for day_tiles in per_date.values():
            tiles.update(day_tiles)
    if not tiles:
        return 0
    tiles = sorted(tiles)

    # The fire mask decides which tile-days were observed, so it is read even when it is
    # not among the bands still to fetch.
    keys = {MASK_KEY}
    for band in todo:
        keys.add(ASSETS[band.partition("_")[2]][0])

    def read(key):
        with rasterio.open(item.assets[key].href) as ds:
            return key, band_dates(ds, days), modis_sinu.read_block(ds, grid, h, v, tiles)

    blocks = {}
    file_days = None
    with rasterio.Env(**_ENV), ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for key, key_days, block in ex.map(read, sorted(keys)):
            if file_days is not None and key_days != file_days:
                raise ValueError(f"{key} holds other days than {MASK_KEY}")
            file_days = key_days
            blocks[key] = block

    written = 0
    for band, per_date in todo.items():
        suffix = band.partition("_")[2]
        key, nodata, scale = ASSETS[suffix]
        for layer, day in enumerate(file_days):
            for tx, ty in per_date.get(day, ()):
                mask = modis_sinu.cut_tile(blocks[MASK_KEY], grid, h, v, tx, ty, layer)
                if not is_processed(mask):
                    continue          # nothing was observed that day: nothing to store
                if key == MASK_KEY:
                    arr = mask
                else:
                    arr = modis_sinu.cut_tile(blocks[key], grid, h, v, tx, ty, layer)
                cloud = unobserved_pct(mask) if suffix == "fire_mask" else None
                valid = arr.size if nodata is None else int((arr != nodata).sum())
                blob = common.encode_geotiff(arr, grid, tx, ty, CRS, nodata, scale, None)
                added = writer.add(band, day, tx, ty, CRS, RES, TILE_PX,
                                   common.wgs84_bounds(grid, tx, ty, CRS), nodata, cloud,
                                   item.id, blob, valid)
                written += 1 if added else 0
    log(f"    {written} tiles")
    return written
