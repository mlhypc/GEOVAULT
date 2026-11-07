"""GEDTM30 v1.2 global digital terrain model (30 m, EPSG:4326) from OpenGeoHub.

Bare-earth terrain (DTM) predicted by an ensemble machine-learning model from several
global DEMs, trained on ICESat-2 and GEDI terrain heights; heights above the EGM2008
geoid, float32 metres. Coverage 65 S - 85 N. Licence CC BY 4.0 (commercial use allowed).

Each layer is ONE global COG (1,296,009 x 540,009 px), read by window. Bands:
  DTM   terrain height (m)
  STD   prediction standard deviation (m)
  MASK  land mask from the publisher (uint8)

Trap: the DTM and STD files carry a GDAL scale tag of 0.1, but the stored values are
already metres (checked against Copernicus GLO-30). Tiles are stored raw without that
tag; never apply it.
"""

from . import static_cog

STATIC = True
DEFAULT_BANDS = ["DTM", "STD", "MASK"]
RES_LIST = [1.0 / 3600.0]   # 1 arc second
TILE_PX = 240

_BASE = "https://s3.opengeohub.org/global/dtm/v1.2/"
_FILES = {
    "DTM": "gedtm_rf_m_30m_s_20060101_20151231_go_epsg.4326.3855_v1.2.tif",
    "STD": "gedtm_rf_std_30m_s_20060101_20151231_go_epsg.4326.3855_v1.2.tif",
    "MASK": "gedtm_mask_c_30m_s_20060101_20151231_go_epsg.4326.3855_v1.2.tif",
}


def ingest(bbox, writer, bands=None, skip_keys=None, aoi=None, log=print) -> int:
    total = 0
    for band in bands or DEFAULT_BANDS:
        if band not in _FILES:
            raise ValueError(f"gedtm30: unknown band {band!r}, choose from {sorted(_FILES)}")
        total += static_cog.ingest_assets(
            [_BASE + _FILES[band]], band, writer, bbox, TILE_PX,
            skip_keys=skip_keys, aoi=aoi, nodata=None,
            source_id="opengeohub-gedtm30-v1.2", log=log)
    return total
