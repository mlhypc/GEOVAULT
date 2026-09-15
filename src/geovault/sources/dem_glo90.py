"""Copernicus GLO-90 DEM (90 m, EPSG:4326) from the AWS open data bucket.

Same product family and bucket layout as GLO-30 (see dem_glo30.py), coarser: one
1200x1200 px COG per 1x1 degree cell (3 arc second) instead of 3600x3600, about
1/8 the transfer. Anonymous access, static layer, cells over open water do not
exist as files (skipped silently). Use this instead of GLO-30 when a basemap-grade
elevation layer is wanted (hillshade, coarse relief) rather than per-parcel precision.
"""

import math

from . import static_cog

STATIC = True
DEFAULT_BANDS = ["DEM"]
RES_LIST = [1.0 / 1200.0]   # 3 arc seconds
TILE_PX = 240         # divides the 1200 px of a 1-degree file: 5 x 5 tiles, none straddling files
NODATA = -9999.0

_URL = ("https://copernicus-dem-90m.s3.amazonaws.com/"
        "Copernicus_DSM_COG_30_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM/"
        "Copernicus_DSM_COG_30_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM.tif")


def _cells(bbox, aoi=None):
    """1x1 degree COG cells covering bbox. With an AOI only the cells the polygon
    actually touches are yielded: a scattered set of parcels has a huge bbox but
    touches few cells, and every yielded cell costs a remote open."""
    w, s, e, n = bbox
    if aoi is not None:
        from shapely.geometry import box
        from shapely.prepared import prep
        hit = prep(aoi).intersects
    for lat in range(math.floor(s), math.ceil(n)):
        for lon in range(math.floor(w), math.ceil(e)):
            if aoi is not None and not hit(box(lon, lat, lon + 1, lat + 1)):
                continue
            yield _URL.format(ns="N" if lat >= 0 else "S", lat=abs(lat),
                              ew="E" if lon >= 0 else "W", lon=abs(lon))


def ingest(bbox, writer, bands=None, skip_keys=None, aoi=None, log=print) -> int:
    return static_cog.ingest_assets(
        list(_cells(bbox, aoi)), "DEM", writer, bbox, TILE_PX,
        skip_keys=skip_keys, aoi=aoi, nodata=NODATA,
        source_id="copernicus-glo90", log=log)
