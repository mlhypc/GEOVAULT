"""NASADEM (30 m, EPSG:4326) from Microsoft Planetary Computer.

NASA's reprocessing of the February 2000 SRTM radar mission (improved phase unwrapping,
ICESat control, ASTER/GDEM void fill). Surface model (C-band, partly penetrates canopy),
heights above the EGM96 geoid, int16 metres. Public domain (NASA, no restriction).

Files are 1x1 degree COGs of 3601 x 3601 px, pixel-is-point: neighbouring files share
their edge row and column. Each file is therefore assigned the cell whose pixel centres
satisfy lon <= x < lon + 1 and lat < y <= lat + 1, so every pixel is stored once.
Anonymous access with Planetary Computer SAS signing (no account).
"""

from . import static_cog

STATIC = True
DEFAULT_BANDS = ["DEM"]
RES_LIST = [1.0 / 3600.0]   # 1 arc second
TILE_PX = 240        # divides the 3600 px each file owns: 15 x 15 tiles
NODATA = -32768

_PX = 1.0 / 3600.0
_URL = ("https://nasademeuwest.blob.core.windows.net/nasadem-cog/v001/"
        "NASADEM_HGT_{ns}{lat:02d}{ew}{lon:03d}.tif")


def _sign(href):
    import planetary_computer
    return planetary_computer.sign(href)


def assets(bbox, aoi=None):
    """(URL, footprint) per 1-degree cell covering bbox / AOI."""
    for lat, lon in static_cog.degree_cells(bbox, aoi, pad=_PX / 2):
        href = _URL.format(ns="n" if lat >= 0 else "s", lat=abs(lat),
                           ew="e" if lon >= 0 else "w", lon=abs(lon))
        footprint = (lon - _PX / 2, lat + _PX / 2, lon + 1 - _PX / 2, lat + 1 + _PX / 2)
        yield href, footprint


def ingest(bbox, writer, bands=None, skip_keys=None, aoi=None, log=print) -> int:
    return static_cog.ingest_assets(
        list(assets(bbox, aoi)), "DEM", writer, bbox, TILE_PX,
        skip_keys=skip_keys, aoi=aoi, nodata=NODATA,
        source_id="nasadem-v001", sign=_sign, log=log)
