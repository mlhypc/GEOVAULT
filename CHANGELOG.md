# Changelog

## 0.6.0 (2026-11-11)

- Two fire sources from Planetary Computer, no account: `modis_fire` (MOD14A1 + MYD14A1 daily
  thermal anomalies: fire mask, maximum fire radiative power, QA and sample, 1 km, Terra and
  Aqua as separate bands; the 8-day files are split into one tile per day, and a file that
  holds fewer days after a satellite outage is dated from its `Dates` tag) and `modis_burned`
  (MCD64A1 monthly burned area: day of burn, its uncertainty, QA and the detection window,
  500 m).
- `sources/modis_sinu.py`: where a MODIS scene sits on the sinusoidal grid, which store tiles
  it holds and each tile's pixel offset, so an adapter can plan a scene offline and read each
  asset once. Used by the two new sources; searches keep only the newest production of a
  reprocessed scene.
- CLI: the skip set of an ingest now covers every month between a scene's `start_datetime`
  and `end_datetime`, not only the month it starts in. A file holding several days that
  crosses a month end was otherwise fetched again on every re-run, and `--dry-run` reported
  its last days as missing. Sources whose scenes have no interval are unaffected; the others
  only load a larger skip set.
- `tests/test_fire_grids.py` covers the scene math, the block read, daily layers, band
  routing, the months of a scene and an ingest of one scene end to end.

## 0.5.1 (2026-11-07)

- Fix `3dep_1m`: a 10 km cell could also take the
  neighbouring cell's first tile, which a float offset of a fraction of a millimetre made look
  inside it; it stored a 6-column sliver from its buffer, and the neighbour's full tile was then
  dropped as a duplicate key. Stores built with 0.5.0 can lack 500 m strips along cell edges:
  delete the `3dep_1m` dataset and ingest again.
- Static engine: tiles touching an asset's extent only within floating-point noise are left to
  the neighbouring asset, and tiles carry their valid pixel count so the fuller one wins a key.

## 0.5.0 (2026-11-07)

- Five static DEM sources: `nasadem` and `aw3d30` (Planetary Computer), `gedtm30`
  (OpenGeoHub global terrain model, bands DTM / STD / MASK), `fabdem` (University of
  Bristol, CC BY-NC-SA 4.0, non-commercial; cells taken from the publisher's tile index) and
  `3dep_1m` (USGS 1 m lidar DEM, US only: per-tile choice between overlapping lidar projects,
  all three USGS file-name generations, tiles touching a UTM zone line kept in both zones so
  no strip is left uncovered, a cell with an unreachable project skipped for a re-run).
- Static engine: an asset can carry a footprint, the part of the file it owns, for products
  whose files overlap their neighbours; a `sign` hook applied just before each open; a
  `degree_cells` helper now used by every 1-degree source. Assets that fail to open for
  another reason than "not found" are reported with a hint to re-run, no longer logged as
  open water.
- Fix: pixel-centre registered 1-degree sources (`glo30`, `glo90`, and the new `nasadem`,
  `fabdem`) missed the half-pixel strip a file owns past its cell when a bbox edge fell in it.
- Fix: the tile key is now `(band, date, crs, x, y)`, in the store, the skip set, the catalog
  (new `crs` column) and `reader.read_tile`. UTM tile numbers restart in every zone, so
  without the CRS a tile from one zone could replace or suppress the tile with the same
  numbers in another zone, ground six degrees away, whenever an ingest spans several zones
  on the same date. Stores whose AOIs never put the same tile numbers in two zones are not
  affected; tiles lost in a multi-zone store are fetched again by re-running the ingest, since
  the skip set now includes the CRS. No migration needed; `geovault` rebuilds catalogs with
  the new column on the next ingest. Regression tests in `tests/test_store.py`.
- `__version__` was left at 0.2.0 in the last two releases; it now follows pyproject.
- `tests/test_dem_sources.py` covers footprints, lattices, file naming and the 3DEP tile choice.

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
