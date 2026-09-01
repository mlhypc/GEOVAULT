"""SoilGrids 250 m soil properties from ISRIC (files.isric.org), anonymous.

Served as VRT mosaics in the Interrupted Goode Homolosine projection; tiles are
stored in that native CRS (no resampling), reader-side clipping reprojects the
polygon instead. Band naming: <property>_<depth>, e.g. clay_0-5cm.

Properties: bdod cec cfvo clay nitrogen phh2o sand silt soc ocd ocs
Depths:     0-5cm 5-15cm 15-30cm 30-60cm 60-100cm 100-200cm
"""

from . import static_cog

STATIC = True
TILE_PX = 256
RES_LIST = [250.0]

PROPS = ["bdod", "cec", "cfvo", "clay", "nitrogen", "phh2o", "sand", "silt",
         "soc", "ocd", "ocs"]
DEPTHS = ["0-5cm", "5-15cm", "15-30cm", "30-60cm", "60-100cm", "100-200cm"]

# Default: the topsoil layer of the agronomically common properties.
# Ask for more with --bands (any <prop>_<depth> combination).
DEFAULT_BANDS = [f"{p}_0-5cm" for p in
                 ["clay", "sand", "silt", "phh2o", "soc", "nitrogen", "cec"]]

_URL = "/vsicurl/https://files.isric.org/soilgrids/latest/data/{prop}/{prop}_{depth}_mean.vrt"


def ingest(bbox, writer, bands=None, skip_keys=None, aoi=None, log=print) -> int:
    bands = bands or DEFAULT_BANDS
    total = 0
    for band in bands:
        prop, depth = band.rsplit("_", 1)
        href = _URL.format(prop=prop, depth=depth)
        total += static_cog.ingest_assets(
            [href], band, writer, bbox, TILE_PX,
            skip_keys=skip_keys, aoi=aoi, nodata=None,
            source_id=f"soilgrids-{band}", log=log)
    return total
