# Sources in detail

Notes on the datasets that need more than their row in the
[source table](../README.md#data-sources): what the bands hold, how the source lays
its files out, and what to know before reading the values.

## Landsat

Landsat sits next to Sentinel-2 for two things S2 cannot give: a thermal band
(surface temperature) and an archive back to the 1980s. It is not an S2
replacement (30 m, 16-day cycle per satellite, 8 days with L8+L9 together).

```bash
geovault ingest --dataset landsat --geojson parcel.geojson --start 2013-01-01 --end 2026-09-01 --cloud-max 80
```

Bands are the STAC common names, so one adapter reads every generation:
`coastal` (L8/9 only), `blue`, `green`, `red`, `nir08`, `swir16`, `swir22`
(surface reflectance), `lwir` (surface temperature; asset `lwir11` on L8/9,
`lwir` on L4-7) and `qa_pixel` (CFMask bits). Only Tier 1 scenes are taken.
Pixels are stored raw (uint16, lossless); the physical value is
`reflectance = DN * 2.75e-5 - 0.2` and `kelvin = DN * 0.00341802 + 149`, and
both factors are written into every blob as GeoTIFF band scale/offset
(`ds.scales[0]`, `ds.offsets[0]` in rasterio). `cloud_pct` per tile comes from
`qa_pixel` bits 1-4 (dilated cloud, cirrus, cloud, shadow), computed locally
like SCL for S2. Landsat 7 SLC-off stripes (2003 onwards) arrive as nodata and
count against the tile's valid pixels.

Grid note: Landsat Collection 2 pixel edges sit 15 m off the UTM origin (scene
origins end in ...85/...15), the same for every scene and generation, so the
`landsat` lattice is anchored at (15, 15) and its tiles are 7.68 km (256 px).
A per-scene anchor would have broken the dedup key across scenes.

Latency: Planetary Computer publishes a Landsat scene about a week after
acquisition (measured: acquired 2026-08-24, catalogued 2026-08-31). Sentinel-2
on earth-search is same-day (median 5 h), Sentinel-1 RTC under a day.

## Fire layers (MODIS active fire, burned area)

Two products answer different questions and are stored as two datasets: `modis_fire` says
where a fire was seen burning on a given day, `modis_burned` says which ground burned and
on which day. Neither needs an account.

```bash
geovault ingest --dataset modis_fire   --bbox 25.6 35.8 44.9 42.2 --start 2026-07-01 --end 2026-08-31
geovault ingest --dataset modis_burned --bbox 25.6 35.8 44.9 42.2 --start 2026-01-01 --end 2026-08-31
```

**`modis_fire`** (MOD14A1 + MYD14A1): the product is daily but published in 8-day files, each
asset a raster with one band per day (fewer than 8 after a satellite outage; the file's
`Dates` tag names them). Every day becomes its own tile, dated with that day, so `date`
works as in any daily dataset. A file is always stored whole: a date
range that cuts an 8-day file stores all of that file's days. The last file of a year runs
into January, where the next year's first file starts again on 1 January with the same
pixels; one tile per day is kept. Terra (~10:30 / 22:30 local) and Aqua (~13:30 / 01:30)
are separate bands:

```
terra_fire_mask  terra_max_frp  terra_qa  terra_sample
aqua_fire_mask   aqua_max_frp   aqua_qa   aqua_sample
```

`fire_mask` is the uint8 class of the product: 0-2 not processed, 3 water, 4 cloud, 5 land
without fire, 6 unknown, 7 / 8 / 9 fire of low / nominal / high confidence. `max_frp` is
int32 with `MW = DN * 0.1` written as GeoTIFF scale and 0 where there is no fire; the 0 is
stored as a value, not as nodata, so a series shows "no fire" and not a gap. A tile-day
is stored when its fire mask holds at least one processed pixel, and then with every band,
so an all-zero `max_frp` tile means "observed, no fire"; a tile-day that was not processed
at all is not stored and `--dry-run` keeps reporting it. `cloud_pct` of a `fire_mask` tile
is its share of pixels without a fire observation (cloud, unknown or not processed). The
grid is the one of `modis_lst` (240 px tiles of 222 km, same numbering), so the two stack
pixel for pixel. The daily mask is a 1 km summary of the day's overpasses: small, short or
cloud-covered fires are missed.

**`modis_burned`** (MCD64A1): one file per month, dated with the first day of the month.
Bands `burn_date` (int16 day of the year, 0 unburned, -1 unmapped, -2 water),
`uncertainty` (days), `qa`, `first_day` and `last_day` (the window in which a burn could be
detected). A tile is stored unless its `burn_date` is entirely unmapped; `cloud_pct` of a
`burn_date` tile is the share of its pixels left unmapped. 463 m pixels, 240 px tiles
of 111 km. The product maps the mark a fire leaves in surface reflectance, so it finds
fires that burned an area, not every ignition, and under-detects small and patchy burns
such as many cropland fires. Months appear two to three months late. The producer can
revise a month afterwards; a month already in the store is skipped on re-runs, so a
revision is not picked up.

The Planetary Computer catalogue of both products has gaps (checked 2026-11-11): no
`modis_fire` file between 4 July and 24 November 2025 and a few single 8-day files absent
in most years; `modis_burned` lacks September 2023, July 2024, May 2025 and August to
October 2025. A search over a gap returns no scenes, so neither the ingest nor `--dry-run`
can report it; compare `geovault coverage` with the dates you expect.

```python
series("modis_fire", lon, lat, "aqua_fire_mask", date="2026-07")   # class per day
series("modis_burned", lon, lat, "burn_date", date="2026")         # day of year, 0 = unburned
```

Both adapters read each asset of a scene once and cut the tiles locally. One example run
over a 20 x 6 degree area (six MODIS scenes) took about two minutes and 16 MB per month
for `modis_fire`, and about seven seconds and 5 MB per month for `modis_burned`.

## Meteorology layers (CHIRPS, MODIS LST, ERA5 family, PARDIG)

The vault's rules (no account, no remote compute, native grid, lossless) fit the
daily satellite products directly (CHIRPS, MODIS LST). The others come with stated
deviations: ERA5 and PARDIG are Zarr stores, not COGs; the ERA5-Land family is a
request API behind a free account. IMERG stays out behind Earthdata login. The two
plain daily products first:

```bash
geovault ingest --dataset chirps    --bbox 25.6 35.8 44.9 42.2 --start 2026-01-01 --end 2026-09-14
geovault ingest --dataset modis_lst --bbox 25.6 35.8 44.9 42.2 --start 2026-01-01 --end 2026-09-14
```

**`chirps`**: one global file per day, two streams on the UCSB server: `final`
(station data merged, about three weeks after month end) and `prelim` (~2 days
behind real time). A day is taken from final when it exists, else prelim, and
a stored prelim tile is replaced automatically the day final appears
(`scene_id` is `chirps-final` or `chirps-prelim`; re-run the same ingest to
upgrade). The files are gzip-compressed plain GeoTIFFs, so they cannot be
range-read: the whole 2-11 MB file is downloaded and tiled locally, one
download per day whatever the AOI. Band `precip`, float32 mm/day, nodata -9999
(also over sea), 100 px tiles of 5 degrees anchored at (0, 0); Turkey is 8
tiles. `cloud_pct` is None.

**`modis_lst`**: Terra and Aqua are separate bands, not merged: Terra passes
~10:30/22:30 local, Aqua ~13:30/01:30 (near the daily maximum), and the two
are different measurements of the same day.

```
terra_lst_day  terra_lst_night  terra_qc_day  terra_qc_night     (default)
aqua_lst_day   aqua_lst_night   aqua_qc_day   aqua_qc_night      (default)
terra_view_time_day ... aqua_view_time_night                     (--bands)
```

LST is stored raw uint16 with `kelvin = DN * 0.02` written as GeoTIFF scale
(`ds.scales[0]`), DN 0 = no retrieval. `cloud_pct` of an LST tile is its share
of DN 0 pixels, computed locally. A tile that is entirely DN 0 (fully cloudy)
is not stored, exactly like an S2 tile beyond the edge of its swath; `--dry-run` then
keeps reporting it and a re-run looks at it again. CRS is the MODIS sinusoidal
(`+proj=sinu +R=6371007.181`, no EPSG code), 240 px tiles of 222 km anchored
at (0, 0) (the global origin is an exact multiple of the 1200 px scene, so
every scene is 5 x 5 whole tiles). Turkey is MODIS tiles h20v04, h20v05,
h21v04, h21v05: 8 scenes per day. Planetary Computer publishes MODIS about
2-3 weeks after acquisition.

Stored (2026-09-18, Turkey bbox 25.6 35.8 44.9 42.2): `chirps` 2010-01-01 to
2026-09-10, 6097 days x 8 tiles, 322 MB, no missing day (Sep 2026 still prelim);
`modis_lst` 2026-01-01 to 2026-08-28, 72k tiles, 1.1 GB. MODIS has 25 days with
no scene at all and Aqua misses 60 days: Planetary Computer itself returns zero
items for those dates (checked), most of them Saturdays, so this is an upstream
catalog gap, not a fetch failure; `--dry-run` cannot see it either (nothing to
plan). Mean `cloud_pct` of LST tiles runs ~78% in January and ~40% in August.

**`era5`** (optional extra: `pip install -e .[era5]`): ERA5 hourly single levels from
Google's ARCO-ERA5 bucket, the only anonymous and current source of gridded 2 m air
temperature, dewpoint, wind and radiation we found (ERA5-Land 0.1 deg sits behind
Copernicus / DestinE accounts). 0.25 deg cells (~28 x 22 km over Turkey): a regional
context layer, the coarsest in the vault. Stated deviations from the rules above:
the source is Zarr, not COG, and it is chunked one hour x whole globe, so reading
Turkey downloads the global chunk anyway, about 3 MB per hour per variable, ~350 MB
per day for the six default bands, while the stored tiles are a few hundred KB.
Bandwidth, not disk.

```bash
geovault ingest --dataset era5 --bbox 25.6 35.8 44.9 42.2 --start 2026-06-01 --end 2026-06-30
geovault ingest --dataset era5 --bbox ... --start ... --end ... --bands t2m d2m tp u10 v10 ssrd sp swvl1
```

One tile per HOUR, `date` = `YYYY-MM-DDTHH`; a `date` prefix of `YYYY-MM-DD` in the
reader returns the 24 hours, daily statistics are computed at read time and never
stored. Values are RAW ERA5 units (K, m of water, J m-2 per hour, m s-1, Pa,
m3 m-3), nodata -9999. Band codes follow ECMWF short names (`t2m d2m tp u10 v10
ssrd` default; `sp msl tcc skt e pev sd i10fg swvl1-4 stl1 mx2t mn2t` on request).
Lattice: cell centres sit on the 0.25 deg lines, so the anchor is (-0.125, -0.125);
100 px tiles of 25 deg, Turkey is one tile. The final ERA5 ends about 3 months back,
the preliminary ERA5T about a week back (both read from the store's attributes at
search time); ERA5T hours are stored with `scene_id` `era5t` and replaced by the
final `era5` rows on the next run once available, like CHIRPS prelim/final.

**`era5land`, `era5land_daily` and `agera5`** (Copernicus CDS, free account, extra `pip install -e .[cds]`,
credentials in `api/`): the 0.1 deg family (~11 x 9 km over Turkey). ERA5-Land is the
hourly raw reanalysis, land only; AgERA5 is its daily agronomic reduction (v2.0), a
derived product stored as published like CHIRPS. Second stated deviation from the
rules: CDS is a request API (it assembles a NetCDF server-side and returns a file), not
byte-range reads. `era5land`: one request per day with all bands and 24 hours;
`agera5`: one request per calendar month and variable, run concurrently; area = the whole
10 deg tiles the AOI touches, so stored tiles are always complete. Measured: a day of
Turkey answered in tens of seconds; the service is queued and may take minutes under
load. Re-running an ingest retries only what is missing, as everywhere.

CDS can also reject a scene outright with `400 ... Number queued requests for this
dataset is temporarily limited`, when too many jobs are queued for the dataset at once
(default `--workers 3` scenes in parallel, each already running up to 4 concurrent
variable requests inside `agera5`, so 12 simultaneous submissions). The limit varies
with the load on CDS: the same range can ingest cleanly at the default. If you see this
error, re-run the same command with `--workers 1` to drop the cross-scene concurrency.
If that is still rejected, run one variable per command (`--workers 1 --bands <one>`)
in a loop; a month over a 20 x 6 degree area then takes about a minute per variable.

```bash
geovault ingest --dataset era5land --bbox 25.6 35.8 44.9 42.2 --start 2026-06-01 --end 2026-06-30
geovault ingest --dataset agera5   --bbox 25.6 35.8 44.9 42.2 --start 2026-01-01 --end 2026-09-01
geovault ingest --dataset agera5   --bbox ... --start ... --end ... --bands t2m_mean t2m_max t2m_min rh_12 tp ssr
```

`era5land` rows are per HOUR (`date` = `YYYY-MM-DDTHH`), raw ERA5-Land units, nodata -9999
(sea too). Accumulated fields (`tp`, `ssrd`, `strd`, `e`, `pev`, `ro`) hold the running total
since 00 UTC of the same day: hour 00 carries the previous day's 24 h total, hourly amounts
are differences of consecutive hours, derived by the reader. Preliminary ERA5T hours are
stored with `scene_id` `era5t` (from the response's `expver`), and a re-run re-fetches
ERA5T days older than 60 days so the final `era5land` rows replace them. Bands: `t2m d2m
tp u10 v10 ssrd swvl1 swvl2` default; `strd sp skt e pev ro sd sf swvl3 swvl4 stl1 stl2
lai_hv lai_lv` on request.

`era5land_daily` rows are per DAY in the LOCAL calendar day (`TIME_ZONE` utc+03:00 for
Turkey): the CDS daily-statistics service reduces the hourly fields server-side, one request per
band and day. Bands are `<variable>_<mean|min|max>` of the ERA5-Land instantaneous fields
(`t2m d2m u10 v10 sp skt swvl1-4 stl1 stl2 lai_hv lai_lv`); the service refuses the ACCUMULATED
fields (precipitation, radiation, evaporation, runoff, snowfall), so daily rain and radiation
come from `agera5` or by differencing the hourly `era5land` store. Default bands: `t2m_mean
t2m_min t2m_max d2m_mean u10_mean v10_mean swvl1_mean swvl2_mean skt_max skt_min`.

```bash
geovault ingest --dataset era5land_daily --bbox 25.6 35.8 44.9 42.2 --start 2026-01-01 --end 2026-09-08
```

`agera5` rows are per DAY. Bands are `<variable>_<statistic>`: `t2m_mean t2m_max t2m_min
t2m_dmax t2m_dmean t2m_nmin t2m_nmean` (K), `rh_06 rh_09 rh_12 rh_15 rh_18` (% at local
hours), `d2m_mean` (K), `vp_mean` (hPa), `ws10_mean` (m/s), `tcc_mean`, `tp` (mm/day),
`ssr` (J m-2 day-1), `sd`. Grid note: the CDS area subsetter drops the northern and western
edge rows for this dataset, so the request is padded one cell on those sides.

**`pardig_nrt` and `pardig_lr`** (optional extra: `pip install -e .[pardig]`): PARDIG, the
precipitation product of Google's WeatherNext, a machine-learning estimate of the
instantaneous surface precipitation rate trained on the GPM combined radar-radiometer product
(CORRA), every 15 minutes on a 0.1 deg grid. Two variants, kept as two datasets because their
inputs differ: `pardig_nrt` (ECMWF HRES analysis + SatWeave geostationary mosaic, 2016 to a
few hours ago; the publisher adds steps in 6-hourly batches, 2-8 hours behind real time) and
`pardig_lr` (ERA5, GridSat-B1 and passive microwave sounders, 1999-2023, a static archive).
The publisher recommends NRT for anything from 2016 on; if you splice LR onto NRT, compare
them over 2016-2023 first, since the change of inputs can shift the record. Both are marked
experimental: rates are capped at 75 mm/h, estimates poleward of about 67 deg rely on
unconstrained extrapolation, and light snowfall, drizzle and winter orographic precipitation
are underestimated. The method paper (arXiv:2611.13941) adds that LR ends with its microwave
sounder record (appendix A.1) and that the sparser satellite record before 2008 shifts the
LR distribution.

```bash
geovault ingest --dataset pardig_nrt --bbox 25.6 35.8 44.9 42.2 --start 2025-01-01 --end 2025-12-31
geovault ingest --dataset pardig_lr  --bbox 25.6 35.8 44.9 42.2 --start 2023-01-01 --end 2023-12-31
```

Stated deviation, as for `era5`: the source is Zarr, not COG, and whole objects are
downloaded rather than byte ranges. The bucket holds each archive twice, chunked as maps (4
steps x the globe) and as time series (every step x 5 x 5 pixels). The adapter reads the
time-series copy: a 5 deg tile is 100 chunk objects (10 x 10) for the whole record instead of
a global chunk per hour. Because each object holds every step of the archive, even a one-day
archive request downloads the full-record objects of every tile it touches (Turkey, 8 tiles:
1.3 GB for NRT 2016-2025, measured; about 3 GB for LR 1999-2023, estimated). They go to a disk cache,
`<GEOVAULT_DATA>/cache/<dataset>/<archive folder>/` (e.g. `cache/pardig_nrt/2016_to_2025/`),
and every later year scene and run reads them from there. The cache is emptied when the
archive's `zarr.json` changes, is not removed after a run (it costs about as much disk as
the stored tiles) and can be deleted whenever no ingest is running.

The live stream after the NRT archive is one folder per step holding one global chunk (~1.1
MB). A step is read only once its folder holds a `success` object (seen in every finished
step folder; not documented by the publisher); unfinished steps are picked up by the next
run, and `--dry-run` reports them as missing until then. Segment names (`2016_to_2025`,
`2026_to_present`, ...) are discovered by listing the bucket at search time; the folder
layout, band and grid are fixed and checked before anything is decoded. A scene is one
calendar year of an archive or one day of the live stream. Do not run two ingests of the
same dataset at once (each process would download the same objects); one command can cover
the whole range, and its year scenes share the cache.

One tile per 15-minute step, `date` = `YYYY-MM-DDTHH:MM` (valid time, UTC). `series` accepts
any prefix (`YYYY-MM-DD` returns 96 steps, `YYYY-MM-DDTHH` four); `mosaic` and `clip` need
the full `YYYY-MM-DDTHH:MM`. Band `precipitation`, RAW m h-1 as published (x 1000 = mm/h),
float32, nodata -9999. Amounts are a read-time derivation, never stored: the hourly amount is
approximately the mean of its four instantaneous rates x 1 h. Lattice: cell centres on the 0.1
deg lines (the store's latitude runs south to north and is flipped to north-up), anchor
(-0.05, -0.05), 50 px tiles of 5 deg; Turkey is 8 tiles. `scene_id` names the segment
(`NRT/2016_to_2025`, `NRT/2026_to_present`, `LR/1999_to_2023`).

Measured on a 24-core desktop, Turkey (bbox 25.6 35.8 44.9 42.2,
8 tiles), NRT 2016-2025, one ingest command per year in sequence: 2.8 M rows (350,657 steps x
8 tiles), 1.6 GB on disk in the store plus 1.3 GB of cache (800 objects); about 7 minutes per
year once the cache is filled, 10 minutes for the first year including the download, 70
minutes in all; `--dry-run` over the whole range reported nothing missing. Stored values were
checked once (2026-11-16) tile for tile against the publisher's map-chunked copy: all 8
Turkey tiles at four steps between 2016 and 2025, one tile over two full days each of NRT and
LR, and one tile over a day of live folders, all identical. There is no automated test
against the bucket.

```python
series("chirps", lon, lat, "precip", date="2026")            # mm/day per day
series("modis_lst", lon, lat, "aqua_lst_day", date="2026-08") # raw DN, * 0.02 = K
series("era5", lon, lat, "t2m", date="2026-06-01")              # 24 hourly values, K
series("era5land", lon, lat, "t2m", date="2026-06-01")          # same at 0.1 deg
series("agera5", lon, lat, "t2m_max", date="2026-06")           # daily maxima, K
series("era5land_daily", lon, lat, "swvl1_mean", date="2026-06") # daily mean topsoil moisture, m3/m3
series("pardig_nrt", lon, lat, "precipitation", date="2026-06-01") # 96 rates, m/h
```

Static datasets need no date range:

```bash
geovault ingest --dataset glo30 --geojson parcel.geojson
geovault ingest --dataset worldcover --geojson parcel.geojson
geovault ingest --dataset soilgrids --geojson parcel.geojson            # topsoil defaults
geovault ingest --dataset soilgrids --geojson parcel.geojson --bands clay_0-5cm clay_5-15cm
```

Copernicus GLO-30 is not published for every country: the public bucket has no
cells over e.g. Azerbaijan and Armenia (HTTP 404), so parcels there get no DEM
tiles and the ingest reports them as missing assets, not as an error. GLO-90
(`glo90`) is the same product family, coarser (3 arc second vs 1), from a
separate AWS bucket; same missing-cell behaviour.

The other global DEMs follow the same 1-degree pattern; `3dep_1m` is UTM-tiled (see
below). Each DEM is stored exactly as published (native grid, vertical datum and
units), so comparing them needs a datum conversion on the reader's side (EGM96 for
`nasadem` / `aw3d30`, EGM2008 for `glo30` / `gedtm30` / `fabdem`, NAVD88 for
`3dep_1m` in the conterminous US, other datums in Hawaii, Alaska and the territories):

- Some products ship files that overlap their neighbours (`nasadem`: 3601 px files
  sharing an edge row; `3dep_1m`: a 6 m buffer). Their adapters pass each file's
  nominal cell as a footprint and the engine stores only the pixels that file owns.
- `gedtm30` DTM and STD files carry a GDAL scale tag of 0.1 although the values are
  already metres; tiles are stored without it.
- `fabdem` is licensed CC BY-NC-SA 4.0 (non-commercial use only); GEOVAULT fetches it
  from the publisher on your machine and does not redistribute it. Do not share a
  store, tiles or derived products containing `fabdem` except under the same licence.
- Pixel-centre registered files (`glo30`, `glo90`, `nasadem`, `fabdem`) own ground half
  a pixel past their 1-degree cell, so a bbox just past a degree line opens both files.
- `3dep_1m` may find several lidar projects over one 10 km cell. Each tile comes from
  the project with the latest TNM publication date (not the acquisition year) whose
  tile is complete, else from the one with the most valid pixels; the project is the
  tile's `scene_id`. Old file names without a UTM zone take the zone from the file.
  Near a zone line, ground within one tile of the line is stored in both zones, so no
  strip is left uncovered; the reader picks one. `fabdem` cells are taken from the
  publisher's tile index, so an open failure is always reported for a re-run.

```bash
geovault ingest --dataset gedtm30 --bbox -119.50 36.55 -119.30 36.70
geovault ingest --dataset 3dep_1m --bbox -119.50 36.55 -119.30 36.70
```

SoilGrids is stored in its native Goode Homolosine projection (no resampling);
the reader's clip reprojects the polygon, not the pixels. Its bands are
independent VRTs, so `--workers N` fetches N bands at a time (each ISRIC VRT
costs several seconds just to open; 6 workers measured ~3x faster than
sequential). With a `--geojson` AOI, static ingests take their candidate tiles
from the bbox of each polygon part, not from the AOI's overall bbox: parcels
spread over several countries would otherwise expand to a continent-sized
rectangle (37k tiles per band tested and rejected, versus 135 real ones).

SoilGrids is fetched band by band and the Python threads of `--workers` share one
GIL with GDAL: 6 threads measured 1.2 cores. For a large AOI run several
`geovault ingest` processes with disjoint `--bands` instead (Turkey, 61 bands:
6 processes finished in 40 min where one 6-thread process was heading for 5.5 h).
Processes write their own part files; compaction takes a per-month lock file so
concurrent closes do not merge the same parts twice. `ocs` exists only as
`ocs_0-30cm` on ISRIC; other `ocs_<depth>` bands yield 0 tiles.

Static layers have an empty date and one band each, except SoilGrids where the
band is the property and depth:

```python
clip("glo30", "parcel.geojson", "DEM", "")
clip("worldcover", "parcel.geojson", "MAP", "")
clip("soilgrids", "parcel.geojson", "clay_0-5cm", "")
```

Adding a dataset is one module under `src/geovault/sources/` (a `search` and an
`ingest_scene` function) plus one line in the registry. The engine (grid, store,
catalog, reader) rarely needs to change.

## GEM-15 (surface and terrain elevation, 15 m)

`gem15` is Google's GEM-15 version 1, which its publisher calls experimental: a
surface model and a terrain model at 15 m from stereo satellite imagery, float32
metres, NaN where there is no value. The heights are above the EGM96 geoid; that is
in the product's documentation, not in the files, and most other elevation models in
this table use EGM2008.

| Band | Holds |
|------|-------|
| `dsm` | surface elevation |
| `dsm_percentile_5`, `dsm_percentile_50`, `dsm_percentile_95` | percentiles of the finer surface heights inside the 15 m pixel |
| `dsm_std` | their standard deviation |
| `dtm` | terrain elevation |

The product comes as one pair of files per 1 x 1 degree cell, each in the UTM zone of
the cell. A file holds values about 1 km past its cell, and there neighbouring cells
do not agree, since each was made on its own: on the terrain model across 35 E at
38.5 N two cells differ by 0.7 m in the median over the ground both hold and by 9.5 m
at most, at the corner of four cells at 35 E, 39 N by 0.1 to 0.3 m in the median. A
tile on a cell line is therefore put together from the cells that meet in it, every
pixel from the cell its centre lies in, and what a cell holds past its own ground is
not stored. The step between two cells stays where the product has it, on the degree
line: expect it in a slope or a hillshade made across one.

A tile holds the cells of its own UTM zone; ground of a cell in the neighbouring zone
is stored in that zone's tiles, so nothing is stored twice and a read across a zone
line behaves as described under
[UTM zones](storage.md#utm-zones-which-crs-a-tile-is-stored-in). A tile is stored only
once every cell it takes pixels from has answered: when a cell cannot be opened the
run leaves the tile out and says so, and the same command fetches it later.

```bash
geovault ingest --dataset gem15 --geojson parcel.geojson
```

About 1.2 GB per cell at the source for all six bands; a tile of 256 x 256 pixels
covers 3.84 km.
