"""ALOS World 3D 30 m (AW3D30, EPSG:4326) from Microsoft Planetary Computer.

JAXA surface model from ALOS PRISM optical stereo imagery (2006-2011), heights above the
EGM96 geoid, int16 metres. Planetary Computer hosts version 3.2; the newer 4.1 is only on
the JAXA portal (account needed) and is not covered here. JAXA terms allow commercial and
non-commercial use with attribution.

Files are 1x1 degree COGs of 3600 x 3600 px, pixel-is-area with edges on whole degrees,
so they tile without overlap. Anonymous access with Planetary Computer SAS signing.
Cells over open water do not exist as files and are skipped.
"""

from . import static_cog

STATIC = True
DEFAULT_BANDS = ["DSM"]
RES_LIST = [1.0 / 3600.0]   # 1 arc second
TILE_PX = 240        # divides the 3600 px of a file: 15 x 15 tiles
NODATA = -9999       # the files declare none; voids are filled in v3.2

_URL = ("https://ai4edataeuwest.blob.core.windows.net/alos-dem/AW3D30_global/"
        "ALPSMLC30_{ns}{lat:03d}{ew}{lon:03d}_DSM.tif")


def _sign(href):
    import planetary_computer
    return planetary_computer.sign(href)


def assets(bbox, aoi=None):
    for lat, lon in static_cog.degree_cells(bbox, aoi):
        yield _URL.format(ns="N" if lat >= 0 else "S", lat=abs(lat),
                          ew="E" if lon >= 0 else "W", lon=abs(lon))


def ingest(bbox, writer, bands=None, skip_keys=None, aoi=None, log=print) -> int:
    return static_cog.ingest_assets(
        list(assets(bbox, aoi)), "DSM", writer, bbox, TILE_PX,
        skip_keys=skip_keys, aoi=aoi, nodata=NODATA,
        source_id="jaxa-aw3d30-v3.2", sign=_sign, log=log)
