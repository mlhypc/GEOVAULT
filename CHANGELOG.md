# Changelog

## 0.4.0 (2026-11-03)

- Three lighter NDVI sources for grid-scale vegetation-cycle questions (see docs in each module):
  `modis_vi_1km` (MOD13Q1 / MYD13Q1 read at the COG 4x overview, 926 m, plus a per-km `good`
  share from the 250 m reliability band; same tile numbering as `modis_vi`), `s3_syn_v10`
  (Sentinel-3 SYNERGY V10 10-day NDVI, 1 km, anonymous on Planetary Computer, gapless since
  2019-10; extra `[syn]` = h5py + fsspec + aiohttp) and `cgls_ndvi300` (Copernicus Global Land
  NDVI 300 m V3 from the CDSE object store; needs CDSE S3 keys in `api/cdse_s3`, template added).
- `credentials.cdse_s3()`; `tests/test_ndvi_grids.py` covers the three lattices and band routing.

## 0.3.0 (2026-11-03)

- New source `modis_vi`: MODIS vegetation indices (MOD13Q1 Terra + MYD13Q1 Aqua v061, 16-day
  composites, 250 m) from Microsoft Planetary Computer. Terra and Aqua are stored as separate
  bands (ndvi, evi, reliability, doy, quality), raw values with GeoTIFF scale, default bands
  ndvi + reliability. Planetary Computer has no scenes between 2025-07-12 and 2025-11-01.

## 0.2.0 (2026-10-29)

- Writer flushes its buffer to a part file on its own once it holds more than `--flush-mb`
  (default 256 MB) of tiles, so an interrupted static ingest keeps what it fetched instead
  of losing everything held in RAM. A better tile for an already flushed key is appended
  again and wins at compaction (newest `fetched_at`, now strictly increasing per process).
- Static ingests log progress: one line per asset with elapsed, rate and ETA, and one every
  5,000 tiles inside an asset.
- Writer gained `flush()`, `written` (all part files of the session) and a `log` callback;
  `close()` still flushes and auto-compacts.
- `tests/test_store.py` covers auto-flush, flushed-key replacement and month splitting.

## 0.1.0

- Initial release: STAC/COG sources, month-partitioned Parquet store, catalog, reader.
