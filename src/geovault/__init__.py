"""GEOVAULT: research-grade satellite tile vault.

Acquire from the best free source per dataset (STAC search + COG range reads,
zero remote compute), store as month-granular Parquet files holding lossless
GeoTIFF tiles, answer existence queries from a small catalog, and read from any
local project through geovault.reader or plain DuckDB.
"""

__version__ = "0.1.0"
