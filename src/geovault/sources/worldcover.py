"""ESA WorldCover 2021 v200 (10 m land cover, EPSG:4326) from AWS open data.

One COG per 3x3 degree macrotile, anonymous access. Values are class codes
(10 tree cover, 20 shrubland, 30 grassland, 40 cropland, 50 built-up,
60 bare, 70 snow/ice, 80 water, 90 wetland, 95 mangroves, 100 moss/lichen).
"""

import math

from . import static_cog

STATIC = True
DEFAULT_BANDS = ["MAP"]
RES_LIST = [1.0 / 12000.0]
TILE_PX = 240        # divides the 36000 px of a 3-degree file: 150 x 150 tiles per file
NODATA = 0

_URL = ("https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
        "ESA_WorldCover_10m_2021_v200_{ns}{lat:02d}{ew}{lon:03d}_Map.tif")


def _cells(bbox, aoi=None):
    """3x3 degree COG cells covering bbox; with an AOI only the cells the polygon touches
    (a scattered set of parcels has a huge bbox, every yielded cell costs a remote open)."""
    w, s, e, n = bbox
    if aoi is not None:
        from shapely.geometry import box
        from shapely.prepared import prep
        hit = prep(aoi).intersects
    lat0 = math.floor(s / 3) * 3
    lon0 = math.floor(w / 3) * 3
    for lat in range(lat0, math.ceil(n), 3):
        for lon in range(lon0, math.ceil(e), 3):
            if aoi is not None and not hit(box(lon, lat, lon + 3, lat + 3)):
                continue
            yield _URL.format(ns="N" if lat >= 0 else "S", lat=abs(lat),
                              ew="E" if lon >= 0 else "W", lon=abs(lon))


def ingest(bbox, writer, bands=None, skip_keys=None, aoi=None, log=print) -> int:
    return static_cog.ingest_assets(
        list(_cells(bbox, aoi)), "MAP", writer, bbox, TILE_PX,
        skip_keys=skip_keys, aoi=aoi, nodata=NODATA,
        source_id="esa-worldcover-2021-v200", log=log)
