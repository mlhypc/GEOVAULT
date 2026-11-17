# Changelog

## 0.10.3 (2026-11-17)

- A tile across the antimeridian could not be found where it lies. Its WGS84 bounds
  were the smallest and the largest longitude of its corners, about -180 and 180:
  everything on that latitude except the tile. A request on its own ground (a point
  at 179.99 E, a bbox across its west-longitude end) did not match it, so `series`,
  `mosaic` and `clip` returned nothing there, and every request elsewhere on that
  latitude got it as a candidate. The bounds of such a tile are now stored with
  `west > east`, as a bbox is in GeoJSON and STAC (`common.wgs84_bounds`), and the
  bbox test of the catalog (`catalog.bbox_sql`, used by `reader.tiles_for_bbox` and
  `catalog.coverage`), the AOI filter of an ingest, the MGRS zone rule and the 3DEP
  zone test read them that way. Concerned are the tiles of a projected CRS that lie
  across the antimeridian: in UTM zones 1 and 60, on polar grids, and the sinusoidal
  (MODIS) tiles at the two ends of the projection. A geographic grid has no such
  tile: its tiles keep the longitudes of the grid, which can run past 180, as before.
- Beside a pole the bounds of a tile left ground out. They were taken from its four
  corners, but the point of a tile nearest the pole lies on an edge or inside it. On
  a polar stereographic grid with tiles of 256 km the tile holding the pole stopped
  183 km short of it and the tile beside that one 54 km short of its own ground (1.8
  and 0.5 km with tiles of 2.56 km); the sinusoidal tiles under a pole spanned a part
  of the longitudes of their ground (58 of 180 degrees at 500 m). `common.wgs84_bounds`
  now follows the outline of a tile, 8 points per edge and its point nearest each
  pole, so the bounds hold the ground of the tile. A tile that holds a pole, or lies
  within a pixel of one, spans every longitude and reaches the pole; a tile around
  the world spans every longitude; a tile that reaches past the edge of its
  projection gets bounds wider than its ground (a sinusoidal tile at one end of the
  projection reaches a few degrees onto the other end). Measured against ground
  sampled on the edges and inside the tiles: nothing left out on polar and sinusoidal
  grids, at most 0.003 pixel on UTM tiles and 0.05 pixel on conic tiles of 256 km.
  The bounds of other tiles change only where an edge bulges past the corners, as on
  a UTM tile across the central meridian of its zone (0.004 pixel at 10 m); they are
  the same to the last digit on 1,644 tiles of UTM and sinusoidal grids at 34 to
  44 N. A tile with a corner off the projection still has no finite bounds and is
  not planned for an area. The bounds are computed once per tile in a process, since
  an ingest asks for them for every band and scene.
- `catalog.bbox_sql`, and so `reader.tiles_for_bbox`, `catalog.coverage` and the
  `coverage` command, refuses a bbox with west > east, south > north or a value that
  is not finite with a `ValueError`. (Such a bbox matched only tiles spanning the
  whole world, or failed inside DuckDB.) A request bbox across the antimeridian is
  not supported: ask for its two parts.
- Tiles stored before keep the bounds they were stored with, and an ingest skips a
  tile it already holds: a store with tiles across the antimeridian or beside a pole
  has to be fetched again for them to be found there.
- Tests: `tests/test_tile_bounds.py`, 24 tests (the ground of a tile, sampled on its
  edges and inside, lies in its bounds on UTM, polar, sinusoidal, Mercator and
  interrupted grids; a pole inside a tile, on an edge and on a corner; a tile wider
  than half the world and one around it), and `tests/test_antimeridian.py`, 15 tests
  (the bounds of a tile across the antimeridian and of its neighbours; the AOI
  filter, the zone rule and the 3DEP zone test; a bbox, a point and a mosaic on both
  sides; no match elsewhere; a bbox the wrong way round; a geographic grid).

## 0.10.2 (2026-11-17)

- An ingest left tiles out of its plan along one edge of the area. `common.bbox_to_crs`
  made the native rectangle of a bbox (of `--bbox`, or of the bounds of a `--geojson`
  area) from its four corners, but a parallel is a curve in UTM: where a bbox lies
  across the central meridian of the zone its south edge dips below its corners (the
  north edge in the southern hemisphere; in a sinusoidal grid the east and west edges
  of a bbox across the equator). The dip is 7 m for a bbox a quarter of a degree wide,
  116 m for one degree, 4.2 km for a whole zone. Measured on a 10 m grid with the zone
  rule: a bbox one zone wide lost 147 of 53,252 tiles, a bbox from 26 to 45 E lost
  1,843 tiles of zone 36 (22.7 km) and 1,207 of zone 37 (16.7 km); a bbox of one
  degree lost tiles in 32 of 600 positions tried, a parcel loses nothing (5 cm). The
  edges are now followed in steps of 0.02 degrees (`common.EDGE_STEP_DEG`), which
  leaves centimetres. Also fixed by this: a UTM bbox reaching a pole collapsed to a
  line. Sources stored in a geographic CRS were never affected.
  To fill what a past ingest left out, run it again: it fetches only the missing tiles.
- Tests: `tests/test_bbox_plan.py`, 23 tests (the tile under the bulging edge in six
  cases, every point of a bbox inside its native rectangle, a small bbox planned as
  before, a bbox given the other way round, an empty part of an AOI, a geographic CRS
  on another datum).

## 0.10.1 (2026-11-17)

Three ways the store could lose or split tiles; all of them older than 0.10.0.
`s1` tiles stored by 0.9.0 or 0.10.0 from scenes whose platform the catalogue spells
in upper case carry that spelling in `acq` (`WHERE acq <> lower(acq)` finds them):
delete those months and ingest them again.

- A compaction (and `reencode`) deleted the month's files one by one and only then
  renamed the new file into place. When a part could not be deleted, because another
  program held it open, the sealed file was already gone and its rows existed only in
  a `.parquet.tmp` that the next compaction overwrote (reproduced on Windows: a month
  of four tiles came back with one). The new file is now put in place first
  (`store._seal`) and a part that cannot be deleted stays an open part, resolved by
  the next compaction. Windows refuses to rename over a file a reader has open; when
  the reader shares the file for deletion, as DuckDB does, the old sealed file is
  moved aside as `.parquet.old` for that instant and moved back if the new one cannot
  take its place. A sealed file held without that sharing (a plain file handle,
  pyarrow) still makes the compaction raise, with the month as it was. A sealed file
  left aside (the run was killed between the two renames, or the way back was refused
  too) makes its month an open one: `geovault compact` puts it back, once the lock
  file of a killed run is deleted.
- `Writer.flush` emptied its buffer before it wrote. A part that could not be written
  (disk full) raised, but its tiles were gone, and the Writer went on believing they
  were stored, so the same tile added again in that run was dropped. Now the other
  months of the flush are still written, the rows that are not on disk go back into
  the buffer for the next flush, and the first error is raised. While the cause
  lasts every `add` that triggers a flush raises again and the buffer keeps growing:
  in `geovault ingest` the scenes that add a tile meanwhile are listed as FAILED
  (exit code 2; their tiles are stored once a flush succeeds), and an error at the
  close of a chunk ends the run.
- `s1` and `landsat` name the platform of an acquisition in lower case. Planetary
  Computer spells the same satellite `SENTINEL-1A` on scenes processed before 2025 and
  `sentinel-1a` since, also within one orbit (6 of the 1773 overpasses of one store):
  such an overpass was stored as two acquisitions and its tiles were never merged.
- Copies of a tile with the same `fetched_at` are resolved by rule at compaction,
  where the choice was left to chance: the fuller one, then the smaller scene id (as
  `Writer.add` decides), then the smaller blob (a tile re-encoded by `reencode` next
  to its old encoding in a part that could not be deleted). `geovault reencode` now
  says when it left months unsealed, and that message names the likely cause.
- A failed compaction, `reencode` or part write removes its `.parquet.tmp`, unless
  that file itself is held open.
- Not covered: files are not synced to disk before they are renamed, so a power cut
  (not a killed process) right after a compaction can still cost the month.
- Tests: `tests/test_store_failures.py`, 19 tests (real handles on a part, on the
  sealed file with and without sharing; the new file refused its place; a sealed file
  left aside; copies of one age; flushes failing on a month, before they start and at
  the rename; a tile added again; a fuller tile arriving meanwhile), and in
  `tests/test_acquisitions.py` a merged tile left beside one of its scenes and the two
  spellings of a platform.

## 0.10.0 (2026-11-17)

Breaking: one store format. A store written before 0.9.0 can no longer be read or
added to (its files lack the `acq` and `valid_px` columns): delete `data/store` and
`data/catalog` (under `GEOVAULT_DATA` when that is set) and ingest again. Files
written by 0.9.0 are read as before and keep the row groups they have until their
month is written again: by `geovault reencode <dataset>`, or by the compaction that
follows new tiles (`geovault compact` leaves a sealed month alone). Dependencies:
`pyarrow>=16` is new (it writes the store files) and DuckDB is now `>=1.5`; run
`pip install -e .` again after updating.

- Row groups are sized by the tiles they hold: a group ends at 4 MB of tiles or 2048
  tiles (`store.GROUP_BYTES`, `store.GROUP_ROWS`). The files asked DuckDB for groups of
  256 rows, but its writer makes none smaller than 2048, so most month files were one
  or two groups and a reader that wanted one tile fetched the month (a 1.4 GB static
  layer was two groups). Measured on one month file per dataset, one tile: `s1`
  112 -> 5 ms, `s2` 88 -> 6 ms, `agera5` 27 -> 5 ms. Through `reader.read_tile`, 40
  tiles picked at random from a whole dataset: `s1` 4.1 -> 0.7 s, `s2` 2.6 -> 1.1 s, a 921 MB static layer 7.3 -> 1.0 s, a
  1.4 GB one 19.9 -> 1.3 s. Datasets of small tiles (`pardig_nrt`, `chirps`), where
  2048 tiles are under 4 MB, keep the layout they had. File size differs by less than
  1%.
- The price is paid where every key of a dataset is listed, since a file has more
  groups: the catalog query over 171 `s2` month files takes 0.9 s, was 0.2 s, and
  the skip-set queries of an ingest over all of them 0.7 s, was 0.2 s.
- Part files, compaction and `reencode` write through one writer (`store._write_sorted`,
  pyarrow). Against the `COPY` it replaces it peaks lower in memory (a 921 MB file:
  3.0 GB, was 4.3 GB) at the same speed for large tiles; a file of 288,000 small tiles
  takes 5 s, was 2 s. Min/max statistics are written for the key columns, `scene_id`
  and the bounds only (`store.STAT_COLS`). A part file is written under a temporary
  name and renamed, so a failed or interrupted flush leaves no half-written part (an
  interrupted one leaves a `.parquet.tmp` file that nothing reads).
- Removed: everything that read files and catalogs of earlier versions. `store.scan`
  is a plain list of files (`store.ADDED_COLS`, `store.has_rows_without_acq` and
  `catalog.lacks_acquisitions` are gone), `geovault ingest` no longer checks for a
  store written before 0.9.0 (exit code 3 is gone), and the reader names the CRS and
  the acquisition in its queries instead of accepting catalog rows without them.
- Tests: 11 new in `tests/test_row_groups.py` (group sizes on the three write paths,
  the row limit, a tile larger than a group, groups across read batches, the schema
  types, statistics, a NaN nodata value found by a filter, a failed part write) and
  one for `read_tile` on a row without a scene id; the 6 tests of files and catalogs
  of earlier versions are removed with the code.

## 0.9.0 (2026-11-17)

Breaking for `s2`, `s1` and `landsat` stores. A store written before this version can
still be read, but `geovault ingest` (also with `--dry-run`) stops with exit code 3
instead of mixing old and new tiles: delete `data/store/dataset=<name>/` and
`data/catalog/<name>.parquet` for these three and ingest again. Every other dataset is
unchanged and needs nothing.

- The acquisition is part of the tile key: `(band, date, acq, crs, x, y)`. A date is a
  calendar day, and a satellite can image the same ground twice in one: until now the two
  images shared a key and one was dropped (Sentinel-1 morning and evening pass: on one
  tile in western Turkey, 528 of the 936 days with data; Sentinel-2B and 2A ten minutes
  apart on 2025-04-14 and 2025-11-09). `s2`, `s1` and `landsat` tiles name their
  acquisition in the new store and catalog column `acq` (datatake id without the
  processing baseline; platform and
  absolute orbit; platform and WRS path); it is NULL for all other sources.
- Scenes of one acquisition that each hold part of a tile are merged into one tile. The
  tile with more valid pixels used to replace the other, so the rest of the ground was
  lost wherever a tile lies across the cut between two scenes (measured: 28% of the 60 m
  Sentinel-2 tiles, whose 15 km exceed the 10 km granule overlap, and 3.6% of the
  Sentinel-1 tiles of a year were partly empty and could be completed). The merge runs
  when a month is compacted (`common.merge_tiles`): fullest scene first, the others fill
  its empty pixels; `scene_id` names every scene, joined by `|`.
- A later run completes a partly empty tile. The skip rule was per tile, so a scene found
  by a resumed or wider ingest never added its part. A tile without an empty pixel is
  never fetched again; a partly empty one is skipped only for the scenes it holds
  (`Writer.existing_scenes`, `common.plan_scene_tiles`).
- A scene is planned only over the tiles inside its raster (STAC `proj:transform` and
  `proj:shape`). Every scene used to plan every tile of the bbox, so `--dry-run`
  reported tiles far outside a scene as missing; the residue on scene edges that remains
  is smaller (measured on a Sentinel-1 footprint corner: 88 -> 40 tiles).
- New store and catalog column `valid_px`, the number of pixels holding an observation.
  Files written before 0.9.0 have neither column and read as NULL (`store.scan`);
  compaction, the catalog and the reader accept old and new files together.
- `reader.mosaic` and `reader.clip` take `acq=`. When an exact date holds several
  acquisitions observed on the request and none is named, they return the one observed
  on most of it and warn; two overpasses are never blended. Until now the tiles of a
  date filled each other's gaps, so ground only the other overpass saw now comes back
  as nodata unless that overpass is named. `reader.acquisitions` lists them.
  `reader.series` returns one record per acquisition, with `acq` in every record.
  `reader.read_tile` matches the acquisition of its row and raises when a catalog row
  matches no stored tile or several (a catalog out of date, or one built before 0.5.0
  over a file holding the same tile numbers in two UTM zones: rebuild it), where it
  returned the first match or failed with a `TypeError`.
- `Writer.add` takes `acq=`; `Writer.close()` compacts a month that holds a tile of an
  acquisition in several rows (`close(merge=False)` leaves that to a later compaction).
  `reencode` resolves shared keys the same way as compaction. `store.compact` returns
  only the months it sealed and `store.compact_month` returns False when the month is
  locked. `geovault ingest` and `geovault compact` name the months they could not seal
  and the lock files in the way (another process, or left by a crash) instead of
  counting those months as sealed.
- `s1` tiles now store their valid pixel count; the SCL and `qa_pixel` tiles used for
  `cloud_pct` on a re-run are those that hold the scene itself.
- Tests: `tests/test_acquisitions.py` (merge and its order independence, two acquisitions
  on a day, parts from separate runs, tiles that cannot be merged, files and catalogs
  written before the new columns, the plan per scene, the cloud mask of a scene, the
  reader's choice of acquisition, scenes not merged yet).

## 0.8.0 (2026-11-17)

- Fix the zone rule of `s2`. A tile was stored only when its centre lay in the regular UTM
  zone and hemisphere of its CRS, which lost ground in three ways: strips along every zone
  line that neither zone stored (the two lattices are rotated against each other;
  measured along 222 km of the 36 E line: 77 km2 at 10 m, 132 km2 at 20 m, different per
  band resolution), a 640 m strip along the whole equator, and everything where MGRS does
  not use the regular zones (south-west Norway between 3 and 6 E, parts of Svalbard): there
  an ingest found the scenes and stored nothing or only part (nothing at Bergen), and
  `--dry-run` reported nothing missing.
  A tile is now stored when it touches the ground MGRS assigns to its CRS
  (`common.tile_is_canonical`, `common.mgrs_zone_areas`). Each zone keeps tiles up to one
  tile past the line, so a strip about one tile wide on average, at most two, is stored in
  both CRS; `reader.series` at a point in it returns two records per date, one per CRS.
- Fix `landsat` and `s1`: no zone rule. Their scenes come in the zone of the scene, not of
  the ground, so the rule did not remove duplicates, it dropped scenes: every Landsat scene
  of the southern hemisphere, which is delivered in the northern CRS (all 25 scenes of a
  test area at Canberra), and near zone lines the scenes of the neighbouring zone (measured
  at 37 N: 6 of 16 Landsat scenes 27 km from the 36 E line, 14 of 20 Sentinel-1 scenes
  13 km from it). Every scene is stored in its own CRS. `--keep-offzone` now only concerns
  `s2`.
- Stores built before this change keep what they have. Re-running an ingest fetches the
  tiles the old rule left out; `--dry-run` reports them as missing, so it exits 1 on a
  store it used to call complete. An `s2` store in south-west Norway or on Svalbard keeps
  the regular-zone tiles it has next to the MGRS-zone tiles it gains.
- `reader.mosaic` and `reader.clip` return one CRS, as before, but prefer the zone MGRS
  assigns to the bbox centre (it was the regular zone, which holds only a strip of tiles
  where MGRS departs from it). They warn when tiles of another CRS hold observations on
  ground of the request where the result has none. Observed pixels are compared, on a
  lattice of points over the request, so a parcel-sized request is judged too; `clip`
  judges the polygon, part by part, not its bbox. Code that turns warnings into errors
  has to expect it for a request across a zone line.
- Fix `reader.clip`: pixels outside the polygon are set to the band's nodata value, as
  documented. They were set to 0 for unsigned bands and to -32768 for all others, whatever
  the band's nodata was. On unsigned bands whose nodata is not 0 the fill was a value of
  its own (`reliability` of `modis_vi`, where 0 is good data, `good` of `modis_vi_1km`,
  `MASK` of `gedtm30`, `qa_pixel` of `landsat`); on signed and float bands with another
  nodata (-9999 of the DEM, reanalysis and precipitation sources, -3000 of MODIS NDVI, ...)
  it was a second marker next to the band's own. Code that tested clipped arrays against
  -32768 must test against the band's nodata, or use `masked=True`. Bands whose nodata
  already was 0 or -32768 (`s2`, `s1`, `landsat` reflectance, `worldcover`, `soilgrids`,
  `nasadem`, `modis_lst`) are unchanged.
- Fix `reader.mosaic` and `reader.clip` on bands without a nodata value (`max_frp`, `qa`,
  `sample` of `modis_fire`; `qa`, `uncertainty` of `modis_burned`): every call failed with a
  `TypeError`. A mosaic of a band whose nodata is NaN no longer comes back empty, and
  `reader.series` skips its NaN pixels.
- `reader.mosaic` and `reader.clip` take `masked=True` and then return a numpy masked array:
  masked where no stored tile covers a pixel, where the tile holds nodata, and for `clip`
  outside the polygon. A band without a nodata value can only be read this way; without
  `masked=True` it raises a `ValueError` that says so.
- `reader.series` records carry the `crs` of the tile they come from. Fix: a point less
  than a pixel west or north of a stored tile was given that tile's edge pixel, as a
  second record for the date or as a value for ground no tile holds.
- Tests: `tests/test_zone_rule.py` (every point lies in a kept tile of its own MGRS zone:
  across zone lines in both hemispheres, the antimeridian, the equator, and through the
  whole width of the Norway and Svalbard zones, at 10, 20 and 60 m; Landsat and Sentinel-1
  plans ignore the zone) and `tests/test_reader_mosaic.py` (fill values of mosaic and clip,
  bands without nodata, NaN nodata, the masked return, cleanest tile first, the CRS a
  mosaic takes, the series fixes, and the warning: parcel-sized requests, small and thin
  parts of a polygon, nodata in kept and in left-out tiles, the edge of a swath stored in
  both CRS).

## 0.7.0 (2026-11-16)

- Two precipitation sources from Google's public WeatherNext bucket, no account (extra
  `pardig`: zarr, gcsfs, numcodecs): `pardig_nrt` (PARDIG-NRT, 2016 to a few hours ago) and
  `pardig_lr` (PARDIG-LR, 1999-2023), both a 15-minute instantaneous rate on a 0.1 deg grid,
  stored raw in m/h, one tile per step with `date` = `YYYY-MM-DDTHH:MM`. Archives are read
  from the publisher's time-series chunking through a disk cache, so each object is
  downloaded once and reused by every year scene and later run; the NRT live stream is read
  step by step, finished steps only. Segment names are discovered from the bucket listing,
  and the Zarr encoding, grid and time axis are checked before anything is decoded.
- `encode_geotiff` / `encode_blob` take an optional `predictor` that pins the TIFF predictor
  and skips the trial encoding; existing sources keep the automatic choice.
- `reader.series` fetches the blobs with one query per month file instead of one per date,
  file by file, so a long series no longer pays a connection per date (measured on a
  15-minute layer: about 0.7 ms per date instead of 11 ms; 3-14 times faster on whole
  histories of the other datasets, same values). On catalogs built before 0.5.0 (no crs
  column) a file holding the same tile numbers in two UTM zones now yields each zone once,
  where it used to return the first zone twice. A catalog row its file no longer holds
  still raises, now with a message that says to rebuild the catalog. `date` accepts any
  prefix of the stored date strings.
- Tests: `tests/test_pardig.py` (lattice, north-up orientation, chunk assembly across time
  chunks and the north pole, time axes, search across archive and live segments, the cache,
  an ingest end to end against a fake bucket, the pinned predictor) and
  `tests/test_reader_series.py` (two zones, catalogs without crs, stale catalogs).

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
