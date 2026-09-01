"""Sentinel-1 RTC via Microsoft Planetary Computer. Free, no account needed.

STAC: https://planetarycomputer.microsoft.com/api/stac/v1, collection
sentinel-1-rtc. RTC is radiometrically terrain corrected gamma0, a better
analysis product than raw GRD (terrain shadow and foreshortening corrected).
10 m, UTM, float32, nodata -32768.

Asset hrefs must be signed (planetary_computer.sign); anonymous signing works,
a free API key only raises rate limits.
"""

from datetime import datetime

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

_ENV = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_MULTIPLEX="YES",
            VSI_CACHE="TRUE")


def search(bbox, start, end, limit=None, **_):
    client = Client.open(STAC_URL, modifier=pc.sign_inplace)
    items = list(client.search(
        collections=[COLLECTION], bbox=list(bbox), datetime=f"{start}/{end}",
    ).items())
    items.sort(key=lambda i: i.properties["datetime"])
    return items[:limit] if limit else items


def ingest_scene(item, bbox, writer, bands=None, skip_keys=None, log=print,
                 keep_offzone=False, aoi=None) -> int:
    bands = bands or DEFAULT_BANDS
    skip_keys = skip_keys or set()
    date = datetime.fromisoformat(
        item.properties["datetime"].replace("Z", "+00:00")).strftime("%Y-%m-%d")
    epsg = item.properties.get("proj:epsg") or str(item.properties.get("proj:code", "")).replace("EPSG:", "")
    crs = f"EPSG:{epsg}"
    grid = Grid(crs, RES, TILE_PX)
    written = 0

    from ..aoi import tile_filter
    in_aoi = tile_filter(aoi)

    with rasterio.Env(**_ENV):
        for band in bands:
            key = BANDS[band]
            if key not in item.assets:
                continue
            nw, ns, ne, nn = common.bbox_to_crs(bbox, crs)
            todo = [(tx, ty) for tx, ty in grid.tiles_for_bounds(nw, ns, ne, nn)
                    if (band, date, tx, ty) not in skip_keys
                    and (keep_offzone or common.tile_is_canonical(grid, tx, ty, crs))
                    and in_aoi(common.wgs84_bounds(grid, tx, ty, crs))]
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
                                  NODATA, None, item.id, blob):
                        written += 1
            log(f"    {band}: {written} tiles (cumulative)")
    return written
