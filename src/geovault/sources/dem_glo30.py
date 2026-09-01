"""Copernicus GLO-30 DEM (30 m, EPSG:4326) from the AWS open data bucket.

One COG per 1x1 degree cell, anonymous access. Static layer: fetched once,
stored under the static/ partition. Cells over open water do not exist as
files; the engine skips missing assets silently.
"""

import math

from . import static_cog

STATIC = True
DEFAULT_BANDS = ["DEM"]
RES_LIST = [1.0 / 3600.0]   # 1 arc second
TILE_PX = 256
NODATA = -9999.0

_URL = ("https://copernicus-dem-30m.s3.amazonaws.com/"
        "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM/"
        "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM.tif")


def _cells(bbox):
    w, s, e, n = bbox
    for lat in range(math.floor(s), math.ceil(n)):
        for lon in range(math.floor(w), math.ceil(e)):
            yield _URL.format(ns="N" if lat >= 0 else "S", lat=abs(lat),
                              ew="E" if lon >= 0 else "W", lon=abs(lon))


def ingest(bbox, writer, bands=None, skip_keys=None, aoi=None, log=print) -> int:
    return static_cog.ingest_assets(
        list(_cells(bbox)), "DEM", writer, bbox, TILE_PX,
        skip_keys=skip_keys, aoi=aoi, nodata=NODATA,
        source_id="copernicus-glo30", log=log)
