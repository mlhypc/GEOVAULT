"""FABDEM V1-2 (30 m, EPSG:4326) from the University of Bristol data repository.

Copernicus GLO-30 with forests and buildings removed by machine learning: a bare-earth
terrain model (DTM), heights above the EGM2008 geoid, float32 metres, 60 S - 80 N.

Licence: CC BY-NC-SA 4.0, NON-COMMERCIAL use only (a commercial licence is sold by
Fathom). GEOVAULT fetches the data from the publisher on the user's machine and does not
redistribute it; respecting the licence is the user's responsibility.

Files are 1x1 degree GeoTIFFs of 3600 x 3600 px (pixel-is-point label, same lattice as
GLO-30), packed in 10x10 degree zip archives. Each file is read in place through the
archive with HTTP range requests, so only the needed bytes travel. Cells missing from the
publisher's tile index (open water) are not requested; any open failure is then a real
failure and is reported for a re-run.
"""

import re
from functools import lru_cache

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import static_cog

STATIC = True
DEFAULT_BANDS = ["DTM"]
RES_LIST = [1.0 / 3600.0]   # 1 arc second
TILE_PX = 240        # divides the 3600 px of a file: 15 x 15 tiles
NODATA = -9999.0
_PX = 1.0 / 3600.0   # pixel-centre registered: a file owns ground half a pixel past its cell

_BASE = "https://data.bris.ac.uk/datasets/s5hqmjcdj8yo2ibzi9b4ew3sn/"


def _lat(v):
    return f"{'N' if v >= 0 else 'S'}{abs(v):02d}"


def _lon(v):
    return f"{'E' if v >= 0 else 'W'}{abs(v):03d}"


def zip_name(lat, lon):
    """Archive holding the 1-degree cell with south-west corner (lat, lon)."""
    la = (lat // 10) * 10
    lo = (lon // 10) * 10
    # the publisher names the east edge of the 170-180 E column "W180", not "E180"
    east = "W180" if lo + 10 == 180 else _lon(lo + 10)
    return f"{_lat(la)}{_lon(lo)}-{_lat(la + 10)}{east}_FABDEM_V1-2.zip"


_INDEX = _BASE + "FABDEM_v1-2_tiles.geojson"
_TILE = re.compile(r"([NS])(\d+)([EW])(\d+)")


def parse_tile_name(name):
    """(lat, lon) of the south-west corner from an index tile name such as N079W106."""
    ns, lat, ew, lon = _TILE.match(name).groups()
    return (int(lat) if ns == "N" else -int(lat)), (int(lon) if ew == "E" else -int(lon))


@lru_cache(maxsize=1)
def existing_cells():
    """Cells the publisher actually has, from its tile index (about 6 MB, read once per run).
    Through /vsizip/ GDAL reports a dropped connection exactly like a missing file, so the
    index, not the open error, decides what is open water."""
    session = requests.Session()
    # the publisher's server drops connections under load; retry before giving up
    retry = Retry(total=5, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    r = session.get(_INDEX, timeout=300)
    r.raise_for_status()
    return frozenset(parse_tile_name(f["properties"]["tile_name"]) for f in r.json()["features"])


def assets(bbox, aoi=None, cells=None):
    cells = existing_cells() if cells is None else cells
    for lat, lon in static_cog.degree_cells(bbox, aoi, pad=_PX / 2):
        if (lat, lon) in cells:
            yield (f"/vsizip//vsicurl/{_BASE}{zip_name(lat, lon)}/"
                   f"{_lat(lat)}{_lon(lon)}_FABDEM_V1-2.tif")


def ingest(bbox, writer, bands=None, skip_keys=None, aoi=None, log=print) -> int:
    return static_cog.ingest_assets(
        list(assets(bbox, aoi)), "DTM", writer, bbox, TILE_PX,
        skip_keys=skip_keys, aoi=aoi, nodata=NODATA,
        source_id="bristol-fabdem-v1-2", log=log)
