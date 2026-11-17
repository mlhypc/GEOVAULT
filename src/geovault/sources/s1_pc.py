"""Sentinel-1 RTC via Microsoft Planetary Computer. Free, no account needed.

STAC: https://planetarycomputer.microsoft.com/api/stac/v1, collection
sentinel-1-rtc. RTC is radiometrically terrain corrected gamma0, a better
analysis product than raw GRD (terrain shadow and foreshortening corrected).
10 m, UTM, float32, nodata -32768.

CRS: a scene comes in the UTM zone of the scene, not of the ground under each pixel,
so within half a scene's width of a zone line the same ground is imaged in one zone's CRS
on some passes and in the neighbour's on others. Every scene is stored in the CRS it
comes in; a rule by ground location would drop those passes. Two overlapping scenes of
one pass can fall in different zones; their overlap is then stored in both CRS.

Asset hrefs must be signed (planetary_computer.sign); anonymous signing works,
a free API key only raises rate limits.
"""


import planetary_computer as pc
import rasterio
from pystac_client import Client

from ..grid import Grid
from . import common

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "sentinel-1-rtc"

BANDS = {"VV": "vv", "VH": "vh"}   # band name -> asset key
DEFAULT_BANDS = list(BANDS)
NODATA = -32768.0
RES = 10
TILE_PX = 256

import os
_ENV = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_MULTIPLEX="YES",
            VSI_CACHE="TRUE")
for _k, _v in _ENV.items():
    os.environ.setdefault(_k, _v)   # visible to every thread (rasterio.Env is thread-local)



def search(bbox, start, end, limit=None, **_):
    client = Client.open(STAC_URL, modifier=pc.sign_inplace)
    items = list(client.search(
        collections=[COLLECTION], bbox=list(bbox), datetime=f"{start}/{end}",
    ).items())
    items.sort(key=lambda i: i.properties["datetime"])
    return items[:limit] if limit else items


ACQUISITIONS = True      # tiles carry `acq`; the CLI plans with Writer.existing_scenes


def acquisition(item) -> str:
    """The orbit a scene was taken on: one overpass of one satellite. The scenes of an
    overpass are consecutive slices of it; a morning and an evening pass of the same day
    are two acquisitions. (The orbit number changes at the equator, where the slices on
    either side come in different CRS and share no tile anyway.) The platform is
    lower-cased: the catalogue spells it 'SENTINEL-1A' on scenes processed before 2025
    and 'sentinel-1a' since, and both occur within one orbit."""
    return (f"{str(common.stac_field(item, 'platform')).lower()}_"
            f"{common.stac_field(item, 'sat:absolute_orbit')}")


def _todo(item, band, bbox, stored, in_aoi):
    """(grid, tiles of `band` this scene still has to be read for). No zone rule
    (keep_offzone=True): that is for MGRS-gridded products; a scene is stored in its own CRS."""
    date, crs = common.scene_date_crs(item)
    grid = Grid(crs, RES, TILE_PX)
    return grid, common.plan_scene_tiles(
        grid, bbox, crs, band, date, stored, acquisition(item), item.id, True, in_aoi,
        extent=common.scene_extent(item, BANDS[band]))


def plan_scene(item, bbox, bands=None, skip_keys=None, keep_offzone=False, aoi=None) -> dict:
    """band -> number of tiles this scene would still fetch (nothing is read).
    Tiles inside the scene's raster that hold no data are counted too, so a small
    residue on scene edges is normal after a full ingest."""
    from ..aoi import tile_filter
    bands = bands or DEFAULT_BANDS
    in_aoi = tile_filter(aoi)
    out = {}
    for band in bands:
        if BANDS[band] not in item.assets:
            continue
        n = len(_todo(item, band, bbox, skip_keys or {}, in_aoi)[1])
        if n:
            out[band] = n
    return out


def ingest_scene(item, bbox, writer, bands=None, skip_keys=None, log=print,
                 keep_offzone=False, aoi=None) -> int:
    bands = bands or DEFAULT_BANDS
    date, crs = common.scene_date_crs(item)
    acq = acquisition(item)
    written = 0

    from ..aoi import tile_filter
    in_aoi = tile_filter(aoi)

    with rasterio.Env(**_ENV):
        for band in bands:
            key = BANDS[band]
            if key not in item.assets:
                continue
            grid, todo = _todo(item, band, bbox, skip_keys or {}, in_aoi)
            if not todo:
                continue
            with rasterio.open(item.assets[key].href) as ds:
                for tx, ty in todo:
                    arr = common.read_tile_window(ds, grid, tx, ty, NODATA)
                    if arr is None:
                        continue
                    blob = common.encode_geotiff(arr, grid, tx, ty, crs, NODATA)
                    if writer.add(band, date, tx, ty, crs, RES, TILE_PX,
                                  common.wgs84_bounds(grid, tx, ty, crs),
                                  NODATA, None, item.id, blob,
                                  int((arr != NODATA).sum()), acq=acq):
                        written += 1
            log(f"    {band}: {written} tiles (cumulative)")
    return written
