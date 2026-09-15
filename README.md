# GEOVAULT

Research-grade satellite tile vault: acquire from the best free public source per
dataset, store lossless in a handful of files, query from any local project.

> Built for personal research and education. It automates access to public data
> services but grants no rights to that data; see
> [Data licensing, attribution and responsible use](#data-licensing-attribution-and-responsible-use)
> before using it for anything beyond learning.

Give it a polygon and a date range; it fetches exactly the tiles that cover the
polygon, skips everything already stored, and lets you read the result back as
numpy arrays clipped to the polygon.

```bash
geovault ingest --dataset s2 --geojson parcel.geojson --start 2024-09-01 --end 2026-09-01
geovault ingest --dataset s1 --geojson parcel.geojson --start 2024-09-01 --end 2026-09-01
```

```python
from geovault.reader import clip
arr, transform, crs = clip("s2", "parcel.geojson", "B4", "2025-07-14")
```

## Design principles

1. **Catalog and store are separate.** "What exists?" is answered by one small
   Parquet per dataset; readers never scan pixel files. (Borrowed from Google
   Earth's BulkMetadata design, which we reverse-engineered before building this.)
2. **Few big immutable files.** One Parquet per (dataset, month, resolution
   group); each row is a tile, pixels stored as a DEFLATE GeoTIFF blob. Rows are
   sorted (band, date, x, y) so DuckDB row-group statistics prune reads.
3. **Zero remote compute.** STAC search plus HTTP range reads from public COGs.
   Cloud share and every other derivation is computed locally. No quotas.
4. **Data is lossless and native.** Native CRS, native pixel grid, no resampling
   at write time. Display products, if ever needed, are derived, never primary.
5. **Deterministic grid.** The tile lattice is anchored at each CRS origin, so
   the same ground always maps to the same (x, y), across runs and datasets.
   Static COG sources keep only their sub-pixel registration offset and use a
   tile size that divides their file size (240 px), so neighbouring files share
   one lattice and no tile straddles two files. Landsat is the one dated source
   with a fixed non-zero anchor (15 m, see [Landsat](#landsat)); the anchor is a
   property of the dataset, never of the run or the scene.

## Data sources

| Dataset | Product | Source | Auth |
|---------|---------|--------|------|
| `s2` | Sentinel-2 L2A, 13 bands, 10/20/60 m | AWS earth-search (element84) STAC + COG | none |
| `s1` | Sentinel-1 RTC (terrain-corrected gamma0), VV/VH, 10 m | Microsoft Planetary Computer | none |
| `landsat` | Landsat Collection 2 Level-2 (L4-5 TM, 7 ETM+, 8-9 OLI/TIRS), surface reflectance + surface temperature, 30 m, 1982 to today | Microsoft Planetary Computer | none |
| `glo30` | Copernicus GLO-30 DEM, 30 m, static | AWS open data COG | none |
| `glo90` | Copernicus GLO-90 DEM, 90 m, static (same product family as GLO-30, ~1/8 the transfer per cell; for basemap-grade elevation, not per-parcel precision) | AWS open data COG | none |
| `worldcover` | ESA WorldCover 2021, 10 m land cover, static | AWS open data COG | none |
| `soilgrids` | SoilGrids 250 m soil properties (11 props x 6 depths), static | ISRIC VRT/COG | none |
| `chirps` | CHIRPS 2.0 daily precipitation, 0.05 deg (~5 km), mm/day, 1981 to ~2 days ago | UCSB Climate Hazards Center (gzipped GeoTIFF per day) | none |
| `modis_lst` | MODIS land surface temperature (MOD11A1 Terra + MYD11A1 Aqua), daily day/night, 1 km, 2000 to today | Microsoft Planetary Computer | none |
| `era5` | ERA5 reanalysis single levels (2 m temperature, dewpoint, precipitation, wind, radiation, ...), hourly, 0.25 deg, 1940 to ~1 week ago | Google ARCO-ERA5 public bucket (Zarr) | none |
| `era5land` | ERA5-Land reanalysis, hourly, 0.1 deg, land only, 1950 to ~5 days ago | Copernicus Climate Data Store (request API, NetCDF) | free CDS account |
| `era5land_daily` | ERA5-Land daily mean/min/max of the instantaneous fields (temperature, dewpoint, wind, soil moisture, skin temperature), local time zone, 0.1 deg | Copernicus Climate Data Store (request API, NetCDF) | free CDS account |
| `agera5` | AgERA5 v2 daily agrometeorological indicators (Tmean/max/min, RH at fixed hours, dewpoint, wind, solar, precipitation), 0.1 deg, 1979 to ~1 week ago | Copernicus Climate Data Store (request API, NetCDF) | free CDS account |

### Landsat

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

### Meteorology layers (CHIRPS, MODIS LST)

The vault's rules (no account, no remote compute, native grid, lossless) leave
few meteorology sources standing: reanalyses (ERA5, AgERA5) live behind CDS
accounts or Zarr stores, IMERG behind Earthdata login. Two daily products fit
and are spatially honest at their own resolution:

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
is not stored, exactly like an S2 tile outside its granule; `--dry-run` then
keeps reporting it and a re-run looks at it again. CRS is the MODIS sinusoidal
(`+proj=sinu +R=6371007.181`, no EPSG code), 240 px tiles of 222 km anchored
at (0, 0) (the global origin is an exact multiple of the 1200 px scene, so
every scene is 5 x 5 whole tiles). Turkey is MODIS tiles h20v04, h20v05,
h21v04, h21v05: 8 scenes per day. Planetary Computer publishes MODIS about
2-3 weeks after acquisition.

Stored (2026-09-14, Turkey bbox 25.6 35.8 44.9 42.2): `chirps` 2018-01-01 to
2026-09-10, 3175 days x 8 tiles, 181 MB, no missing day (Sep 2026 still prelim);
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
byte-range reads. One request per day per scene (`era5land`: all bands and 24 hours in
one; `agera5`: one per variable with its statistics, run concurrently), area = the whole
10 deg tiles the AOI touches, so stored tiles are always complete. Measured: a day of
Turkey answered in tens of seconds; the service is queued and may take minutes under
load. Re-running an ingest retries only what is missing, as everywhere.

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

```python
series("chirps", lon, lat, "precip", date="2026")            # mm/day per day
series("modis_lst", lon, lat, "aqua_lst_day", date="2026-08") # raw DN, * 0.02 = K
series("era5", lon, lat, "t2m", date="2026-06-01")              # 24 hourly values, K
series("era5land", lon, lat, "t2m", date="2026-06-01")          # same at 0.1 deg
series("agera5", lon, lat, "t2m_max", date="2026-06")           # daily maxima, K
series("era5land_daily", lon, lat, "swvl1_mean", date="2026-06") # daily mean topsoil moisture, m3/m3
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
catalog, reader) never changes.

## Credentials (only for non-anonymous sources)

Everything in the source table above is anonymous except the ERA5-Land family:
`era5land` (hourly), `era5land_daily` (daily statistics) and `agera5` (AgERA5 daily
agrometeorological indicators) pull from the Copernicus Climate Data Store, which needs
a free account.
Keys live in `api/` and are git-ignored; only the `*.example` templates are committed:

```bash
cp api/cdsapirc.example api/cdsapirc     # then paste your CDS Personal Access Token
```

See `api/README.md` for the lookup order (repo `api/`, then `~/.cdsapirc`, then
`CDSAPI_URL` / `CDSAPI_KEY`) and the dataset licences you must accept once on the
CDS website.

## Install

```bash
pip install -e .
```

## CLI

```bash
# main flow: polygon in, covering tiles out (true intersection, not bbox)
geovault ingest --dataset s2 --geojson parcel.geojson --start 2024-06-01 --end 2026-06-01

# bbox also works; bands and scene count can be limited
geovault ingest --dataset s2 --bbox 35.30 36.71 35.33 36.73 \
    --start 2024-06-01 --end 2024-06-30 --cloud-max 80 --bands B4 B8 SCL --max-scenes 2

geovault ingest ... --dry-run            # same arguments: report what is still missing, fetch nothing
geovault coverage s2 --date 2024-06     # what is stored
geovault compact s2                     # seal open month part files (a finished ingest does this itself)
geovault rebuild-catalog s2             # regenerate the catalog from the store
```

Re-running an ingest is safe and cheap: cached tiles are skipped via the catalog,
so the same command serves both the initial backfill and incremental top-ups.
Long scene ingests write to disk every 25 scenes, so an interrupted run keeps
what it fetched and the next run resumes from there.

Failures are per scene, not per run: a scene whose fetch raises is reported as
`FAILED`, the other scenes of its chunk are still written, the failed ids are
listed at the end and the exit code is 2. Re-running the same command retries
exactly those scenes. A native crash inside GDAL cannot be caught this way (the
process dies with no traceback), which is why coverage should be verified from
the catalog, not from the exit code: `--dry-run` with the ingest's own arguments
prints, per scene, how many tiles are not in the store yet and exits 0 only when
nothing is missing. Tiles outside a scene's footprint are never stored, so a
small residue on granule edges is normal; a scene reporting all of its tiles is
a gap. Scenes are fetched
concurrently (`--workers`, default 3; each scene already reads its bands in
parallel), about 2.7x faster than one at a time against the AWS open-data
buckets. The result does not depend on fetch order: when two scenes of the
same day cover the same tile (adjacent MGRS granules overlap at their edges),
the tile with more valid pixels is kept, ties going to the lexically smaller
scene id.

Every ingest logs the same lines to the console and to a run log,
`data/logs/<dataset>_<YYYYmmdd-HHMMSS>.log`, each stamped with the time. Scene
lines carry elapsed time, scenes per minute and the remaining-time estimate,
computed from completed scenes (so concurrent workers finishing out of order do
not skew it). Plain lines rather than a progress bar on purpose: long runs go to
the background with output redirected, and a carriage-return bar is noise in a
file. Logs are run artifacts, they live under `data/` and are not committed.

Note on clouds: `--cloud-max` filters on the scene-level (whole MGRS square or
WRS scene) average. Keep it loose (around 80) and select per tile at read time
using the stored `cloud_pct`, which is computed locally per tile from SCL pixels
(S2) or `qa_pixel` bits (Landsat). S1 has no cloud value.

Note on AOI selection: a tile is taken when its WGS84 envelope intersects the
AOI polygon. UTM tiles are slightly rotated in lat/lon, so the envelopes of
neighbouring tiles overlap a little across the shared edge, and a polygon that
ends exactly on a tile edge still pulls the neighbour in. Expect a few extra
tiles along AOI borders; the tile is the unit, not the polygon.

## Consumers

AGRO_DATA_LOCAL (TOPRAQ_AGRODB) reads this vault directly: the parcel analysis
layer (`/api/ee/*`: GLO-30 elevation, Sentinel-2 indices and composites,
Sentinel-1 backscatter products, Landsat surface temperature and indices,
clipped per parcel on demand) and the dated "S2" base layer
(`/api/basetile/s2-<date>/…`, B4/B3/B2 true colour warped to Web Mercator) both
go through the catalog and read blobs only from the files it names. Nothing is
copied out of the vault. Landsat pixels are stored raw; the consumer applies the
scale/offset carried in each blob (see [Landsat](#landsat)).

## Reading from other projects

```python
from geovault.reader import tiles_for_bbox, read_tile, mosaic, clip, series

rows = tiles_for_bbox("s2", (w, s, e, n), band="B4", date="2024-06")  # catalog only
arr, profile = read_tile(rows[0])                                     # one tile
arr, transform, crs = mosaic("s2", bbox, "B4", "2024-06-03")          # bbox mosaic
arr, transform, crs = clip("s2", "parcel.geojson", "B4", "2024-06-03")

# pixel time series at a point: one tile blob opened per date, nothing else
for rec in series("s2", lon, lat, "B4"):
    print(rec["date"], rec["value"], rec["cloud_pct"])
```

Points also work on the ingest side; a small square AOI is built around them:

```bash
geovault ingest --dataset s2 --point 36.735 35.325 --buffer 250 --start ... --end ...
```

The mosaic is nodata-aware (overlapping tiles fill each other's gaps, the tile
with the lowest cloud_pct wins where both have data) and resolves to a single
CRS when the bbox spans a UTM zone boundary.

No Python required on the consumer side; any DuckDB client works:

```sql
SELECT * FROM read_parquet('GEOVAULT/data/catalog/s2.parquet')
WHERE east > ? AND west < ? AND date LIKE '2024-06%';
```

## Storage layout

```
data/
  catalog/<dataset>.parquet                       one row per stored tile
  store/dataset=<d>/<YYYY>/<MM>/r<res>.parquet    sealed month
  store/dataset=<d>/<YYYY>/<MM>/r<res>.part-*.parquet  open parts (hot month)
  store/dataset=<d>/static/r<res>.parquet         static layers
```

Row schema: `band, date, x, y, crs, res, tile_px, west/south/east/north (WGS84),
nodata, cloud_pct, scene_id, data (GeoTIFF blob), size_bytes, fetched_at`.
Logical key: `(dataset, band, date, x, y)`. Tiles are keyed to the ground, never
to a parcel or project, so overlapping AOIs share tiles and deleting an AOI
never invalidates data.

### Write pattern (hot month)

Ingest is a cheap append: each flush writes a new `r<res>.part-<id>.parquet` and
touches nothing else. Once a month spans more than 3 files it is auto-compacted
into a single sealed file at close, and **every finished run seals all the months
it touched**, so part files exist only while a run is in progress (or after a
crash). `geovault compact <dataset>` does the same on demand and doubles as crash
repair. Readers go through the catalog's `file` column, so parts are transparent
to them either way.

### UTM zone boundary rule

Ground near a zone boundary appears in scenes of both zones (with different CRS,
so the dedup key cannot catch it). By default a tile is stored only from the
scene of its center's canonical zone; `--keep-offzone` disables the rule. The
reader's mosaic also prefers the zone of the bbox center.

## Development

```bash
pip install -e . pytest
pytest tests/
```

`examples/` contains small runnable scripts against a locally ingested area.
The data directory can be relocated with the `GEOVAULT_DATA` environment variable.

## Data licensing, attribution and responsible use

GEOVAULT is a personal research and educational tool. It does not host, own or
redistribute any satellite data; it only automates downloads that anyone can
perform from the public sources listed above. If you use this code, YOU are the
one accessing those services, and it is your responsibility to read and follow
each provider's terms and license before fetching or publishing anything:

| Data | License / terms to check |
|------|--------------------------|
| Sentinel-1 / Sentinel-2 | Copernicus Sentinel data legal notice (free use with attribution: "Contains modified Copernicus Sentinel data") |
| Landsat Collection 2 | USGS, public domain; attribution requested: "Landsat imagery courtesy of the U.S. Geological Survey" |
| Copernicus GLO-30 / GLO-90 DEM | Copernicus DEM license (ESA / Airbus terms) |
| ESA WorldCover | CC BY 4.0, attribution required |
| SoilGrids (ISRIC) | CC BY 4.0, attribution required |
| CHIRPS 2.0 | Public domain (CHC/UCSB); cite Funk et al. 2015 |
| MODIS MOD11A1/MYD11A1 | NASA LP DAAC, free; cite Wan et al., attribution requested |
| ERA5 (ARCO-ERA5 mirror) | Copernicus Climate Change Service (C3S) licence: free use with attribution ("Generated using Copernicus Climate Change Service information"); ARCO mirror by Google Research |
| ERA5-Land, AgERA5 (CDS) | C3S licence as above, accepted once per dataset on the CDS website; your account, your requests |
| AWS Open Data buckets | Each dataset's own terms apply, see the registry entry |
| Microsoft Planetary Computer | Microsoft APIs terms of use, per-collection licenses |

Practical rules this project tries to follow and you should too:

- Attribute the data provider in anything you publish, not this repository.
- Do not hammer public endpoints: the built-in caching exists precisely so the
  same tile is never downloaded twice. Keep concurrency modest.
- Anonymous access levels can change; if a provider starts requiring
  registration or keys, comply rather than work around it.
- The stored values are faithful copies of the source pixels, but this project
  gives no warranty of correctness, completeness or fitness for any purpose.
  Verify against the original source before using results in anything that
  matters (publications, agronomic decisions, legal or commercial contexts).

Serving note: this project serves data only to local processes on the same
machine. If you build a public-facing service on top of it, redistribution
terms of each dataset apply to you; check them first.

## Background

The design decisions here came out of reverse-engineering how Google Earth
streams the planet (octree paths, BulkMetadata catalogs, epoch-versioned
immutable URLs, sparse level-of-detail trees) and out of the failure modes of a
previous in-house Earth Engine cache (42k tiny Parquet files, floating grid
anchors, remote reduceRegion calls burning compute quota). GEOVAULT keeps the
good ideas and removes the trips to a metered API.
