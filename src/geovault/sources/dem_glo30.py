"""Copernicus GLO-30 DEM (30 m, EPSG:4326) from the AWS open data bucket.

One COG per 1x1 degree cell, anonymous access. Static layer: fetched once,
stored under the static/ partition. Cells over open water do not exist as
files; the engine skips missing assets silently.
"""

from . import static_cog

STATIC = True
DEFAULT_BANDS = ["DEM"]
RES_LIST = [1.0 / 3600.0]   # 1 arc second
TILE_PX = 240        # divides the 3600 px of a 1-degree file: 15 x 15 tiles, none straddling files
NODATA = -9999.0
_PX = 1.0 / 3600.0   # pixel-centre registered: a file owns ground half a pixel past its cell

_URL = ("https://copernicus-dem-30m.s3.amazonaws.com/"
        "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM/"
        "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM.tif")


def _cells(bbox, aoi=None):
    for lat, lon in static_cog.degree_cells(bbox, aoi, step=1, pad=_PX / 2):
        yield _URL.format(ns="N" if lat >= 0 else "S", lat=abs(lat),
                          ew="E" if lon >= 0 else "W", lon=abs(lon))


def ingest(bbox, writer, bands=None, skip_keys=None, aoi=None, log=print) -> int:
    return static_cog.ingest_assets(
        list(_cells(bbox, aoi)), "DEM", writer, bbox, TILE_PX,
        skip_keys=skip_keys, aoi=aoi, nodata=NODATA,
        source_id="copernicus-glo30", log=log)
