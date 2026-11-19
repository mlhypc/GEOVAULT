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
   Earth's BulkMetadata design.)
2. **Few big immutable files.** One Parquet per (dataset, month, resolution
   group); each row is a tile, pixels stored as a DEFLATE GeoTIFF blob. Rows are
   sorted (band, date, x, y) and cut into row groups of about 4 MB of tiles, so a
   read that names a band, a date or a tile fetches only the groups their min/max
   statistics leave (see [File layout](docs/storage.md#file-layout-row-groups)). Float tiles
   are encoded with both TIFF predictors (2 horizontal, 3 floating-point) and the
   smaller blob kept; see [Blob encoding](docs/storage.md#blob-encoding).
3. **Zero remote compute.** STAC search plus HTTP range reads from public COGs.
   Cloud share and every other derivation is computed locally. No quotas.
4. **Data is lossless and native.** Native CRS, native pixel grid, no resampling
   at write time. Display products, if ever needed, are derived, never primary.
5. **Deterministic grid.** The tile lattice is anchored at each CRS origin, so
   the same ground always maps to the same (x, y), across runs and datasets.
   Static COG sources keep only their sub-pixel registration offset and use a
   tile size that divides their file size (240 px), so neighbouring files share
   one lattice and no tile straddles two files. Landsat is the one dated source
   with a fixed non-zero anchor (15 m, see [Landsat](docs/sources.md#landsat)); the anchor is a
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
| `nasadem` | NASADEM (SRTM 2000 reprocessed), surface model, 30 m, EGM96, static; public domain | Microsoft Planetary Computer | none |
| `aw3d30` | JAXA ALOS World 3D 30 m v3.2 (PRISM optical stereo 2006-2011), surface model, EGM96, static; JAXA terms (commercial use allowed) | Microsoft Planetary Computer | none |
| `gedtm30` | GEDTM30 v1.2 global terrain model (bare earth, ML ensemble), 30 m, EGM2008, bands DTM / STD / MASK, static; CC BY 4.0 | OpenGeoHub global COG | none |
| `fabdem` | FABDEM V1-2 (GLO-30 with forests and buildings removed), bare earth, 30 m, EGM2008, static; **CC BY-NC-SA 4.0, non-commercial only** | University of Bristol (GeoTIFFs read inside 10x10 degree zips) | none |
| `3dep_1m` | USGS 3DEP 1 m DEM (lidar bare earth), US only, NAD83 / UTM, NAVD88, static; public domain | USGS TNM API + prd-tnm COG | none |
| `gem15` | GEM-15 v1 (Google, experimental): surface model with the spread of heights inside each pixel (5 bands) and terrain model, 15 m, UTM, EGM96, 61 S to 84 N where the product has data, static; CC BY 4.0 | Google Cloud public bucket (two COGs per 1 degree cell) | none |
| `soilgrids` | SoilGrids 250 m soil properties (11 props x 6 depths), static | ISRIC VRT/COG | none |
| `chirps` | CHIRPS 2.0 daily precipitation, 0.05 deg (~5 km), mm/day, 1981 to ~2 days ago | UCSB Climate Hazards Center (gzipped GeoTIFF per day) | none |
| `modis_lst` | MODIS land surface temperature (MOD11A1 Terra + MYD11A1 Aqua), daily day/night, 1 km, 2000 to today | Microsoft Planetary Computer | none |
| `modis_vi` | MODIS vegetation indices (MOD13Q1 Terra + MYD13Q1 Aqua), 16-day NDVI/EVI + reliability, 250 m, 2000 to today (PC has no scenes 2025-07-12 to 2025-11-01) | Microsoft Planetary Computer | none |
| `modis_vi_1km` | the same MOD13Q1 / MYD13Q1 composites read from the COGs' 4x overview (926 m, nodata-aware mean of the 250 m pixels) + `good` = share of good/marginal 250 m pixels per km; same tile footprint and numbering as `modis_vi`, about 9x less download | Microsoft Planetary Computer | none |
| `s3_syn_v10` | Sentinel-3 SYNERGY V10 10-day NDVI (OLCI + SLSTR), 1/112 deg (~1 km), S3A + S3B as separate bands, gapless Oct 2019 to today; HDF5 chunk reads of the EUROPE window | Microsoft Planetary Computer | none |
| `cgls_ndvi300` | Copernicus Global Land NDVI 300 m 10-daily V3 (OLCI), 1/336 deg, bands ndvi / unc / nobs / qflag, Oct 2019 to today; global COGs range-read from the CDSE object store | Copernicus Data Space (STAC anonymous, data needs S3 keys) | free CDSE account + S3 key pair (`api/cdse_s3`) |
| `modis_fire` | MODIS thermal anomalies and fire (MOD14A1 Terra + MYD14A1 Aqua), daily fire mask, maximum fire radiative power, QA, 1 km, 2000 to today | Microsoft Planetary Computer | none |
| `modis_burned` | MODIS burned area (MCD64A1, Terra + Aqua combined), monthly, day of burn + uncertainty + QA, 500 m, Nov 2000 to today | Microsoft Planetary Computer | none |
| `era5` | ERA5 reanalysis single levels (2 m temperature, dewpoint, precipitation, wind, radiation, ...), hourly, 0.25 deg, 1940 to ~1 week ago | Google ARCO-ERA5 public bucket (Zarr) | none |
| `era5land` | ERA5-Land reanalysis, hourly, 0.1 deg, land only, 1950 to ~5 days ago | Copernicus Climate Data Store (request API, NetCDF) | free CDS account |
| `era5land_daily` | ERA5-Land daily mean/min/max of the instantaneous fields (temperature, dewpoint, wind, soil moisture, skin temperature), local time zone, 0.1 deg | Copernicus Climate Data Store (request API, NetCDF) | free CDS account |
| `agera5` | AgERA5 v2 daily agrometeorological indicators (Tmean/max/min, RH at fixed hours, dewpoint, wind, solar, precipitation), 0.1 deg, 1979 to ~1 week ago | Copernicus Climate Data Store (request API, NetCDF) | free CDS account |
| `pardig_nrt` | PARDIG-NRT precipitation rate (Google WeatherNext, machine-learning estimate trained on GPM CORRA), 15-minute, 0.1 deg, 2016 to a few hours ago (published in 6-hourly batches); experimental | Google Cloud public bucket `gs://pardig_1` (Zarr v3) | none |
| `pardig_lr` | PARDIG-LR precipitation rate, the long-record variant (inputs ERA5, GridSat-B1, passive microwave), 15-minute, 0.1 deg, 1999-2023, static archive; experimental | Google Cloud public bucket `gs://pardig_1` (Zarr v3) | none |

What the bands of a source hold, how its files are laid out and what to know before
reading its values is in [docs/sources.md](docs/sources.md) (Landsat, the fire layers,
the meteorology layers, GEM-15). To add a dataset, see
[docs/adding-a-source.md](docs/adding-a-source.md).

## Credentials (only for non-anonymous sources)

Everything in the source table above is anonymous except the ERA5-Land family and
`cgls_ndvi300`. `era5land` (hourly), `era5land_daily` (daily statistics) and `agera5`
(AgERA5 daily agrometeorological indicators) pull from the Copernicus Climate Data
Store, which needs a free account; `cgls_ndvi300` reads its files from the Copernicus
Data Space object store, which needs a free key pair (`api/cdse_s3`).
Keys live in `api/` and are git-ignored; only the `*.example` templates are committed:

```bash
cp api/cdsapirc.example api/cdsapirc     # then paste your CDS Personal Access Token
```

See `api/README.md` for the lookup order (repo `api/`, then `~/.cdsapirc`, then
`CDSAPI_URL` / `CDSAPI_KEY`) and the dataset licences you must accept once on the
CDS website.

## Install

Python 3.11 or newer.

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
geovault reencode agera5                # re-compress stored tiles with the current encoder (lossless)
geovault rebuild-catalog s2             # regenerate the catalog from the store
geovault datasets                       # the datasets this version can ingest, one line each
geovault --version
```

A static dataset (an elevation model, land cover, soil) takes no dates:
`geovault ingest --dataset gem15 --geojson parcel.geojson`.

Re-running an ingest is safe and cheap: stored tiles are skipped via the catalog,
so the same command serves both the initial backfill and incremental top-ups.
Tiles are written to disk every 10 scenes and, for any ingest, whenever the
buffer exceeds `--flush-mb` (default 256 MB), so an interrupted run keeps what
it fetched. After a crash: `geovault compact <dataset>` seals the part files
left behind (the next ingest does this too), then re-run the same ingest
command; it fetches only the tiles that are still missing.

Failures are per scene, not per run (a write that fails at the close of a chunk is
the exception, see [Write pattern](docs/storage.md#write-pattern-hot-month)): a scene whose fetch or write raises is reported as
`FAILED`, the other scenes of its chunk are still written, the failed ids are
listed at the end and the exit code is 2. Re-running the same command retries
exactly those scenes. A static ingest does the same for a file that did not open
(and `gem15` for a tile whose read failed): it is left out, counted in the last
line and the exit code is 2. A native crash inside GDAL cannot be caught this way (the
process dies with no traceback), which is why coverage should be verified from
the catalog, not from the exit code: `--dry-run` with the ingest's own arguments
prints, per scene, how many tiles are not in the store yet and exits 0 only when
nothing is missing. A Sentinel-2, Sentinel-1 or Landsat scene is planned only
over the tiles inside its raster; tiles there that hold no data (beyond the
edge of the swath) are never stored, so a small residue on scene edges is
normal; a scene reporting all of its tiles is a gap. Scenes are fetched
concurrently (`--workers`, default 3; each scene already reads its bands in
parallel), about 2.7x faster than one at a time against the AWS open-data
buckets. The result does not depend on the order scenes are fetched in within
a run: scenes of one overpass that each hold part of a tile are merged into one
tile (see [Acquisitions](docs/storage.md#acquisitions-two-overpasses-on-one-day)).

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

Applications read the vault the same way: look tiles up in the catalog, then read
blobs only from the files it names (for example a parcel analysis API clipping
elevation, Sentinel-2 indices or Landsat temperature per parcel on demand, or a map
server warping dated true-colour tiles to Web Mercator). Nothing is copied out of the
vault. Landsat pixels are stored raw; the consumer applies the scale/offset carried
in each blob (see [Landsat](docs/sources.md#landsat)).

## Reading from other projects

```python
from geovault.reader import tiles_for_bbox, read_tile, mosaic, clip, series

rows = tiles_for_bbox("s2", (w, s, e, n), band="B4", date="2024-06")  # catalog only
arr, profile = read_tile(rows[0])                                     # one tile
arr, transform, crs = mosaic("s2", bbox, "B4", "2024-06-03")          # bbox mosaic
arr, transform, crs = clip("s2", "parcel.geojson", "B4", "2024-06-03")

# pixel time series at a point: one tile blob opened per date, nothing else
for rec in series("s2", lon, lat, "B4"):
    print(rec["date"], rec["acq"], rec["value"], rec["cloud_pct"])

# a date with two overpasses: name the one to read
from geovault.reader import acquisitions
for acq in acquisitions("s1", bbox, "VV", "2026-03-01"):      # e.g. morning and evening pass
    arr, transform, crs = mosaic("s1", bbox, "VV", "2026-03-01", acq=acq)
```

Points also work on the ingest side; a small square AOI is built around them:

```bash
geovault ingest --dataset s2 --point 36.735 35.325 --buffer 250 --start ... --end ...
```

The mosaic is nodata-aware (overlapping tiles of one acquisition fill each
other's gaps, the tile with the lowest cloud_pct wins where both have data) and
resolves to a single
CRS when the bbox spans a UTM zone boundary (see
[UTM zones](docs/storage.md#utm-zones-which-crs-a-tile-is-stored-in)).

`mosaic` and `clip` mark a pixel without an observation with the band's own
nodata value (`read_tile(row)[1]["nodata"]`): where no stored tile covers it,
where the tile holds nodata, and for `clip` outside the polygon. `masked=True`
returns a numpy masked array with exactly those pixels masked, so no nodata
value has to be known:

```python
arr, transform, crs = clip("chirps", "parcel.geojson", "precip", "2026-07-14", masked=True)
arr.mean()                                    # over the observed pixels inside the polygon
```

Some bands have no nodata value because every value is a measurement (for
example the `max_frp`, `qa` and `sample` bands of `modis_fire`, `qa` and
`uncertainty` of `modis_burned`). They can only be read with `masked=True`;
without it `mosaic` and `clip` raise a `ValueError` instead of filling the gaps
with a value that would read as data. Work on such an array through its mask
(`arr.mask`, `arr.compressed()`, the masked reductions): `np.asarray(arr)` and
`arr.filled()` drop the mask and leave an arbitrary number in the masked pixels.

No Python required on the consumer side; any DuckDB client works:

```sql
SELECT * FROM read_parquet('GEOVAULT/data/catalog/s2.parquet')
WHERE east > ? AND west < ? AND date LIKE '2024-06%';
```

(A tile of a projected CRS across the antimeridian has `west > east`; a query near 180
degrees has to allow for it, as `catalog.bbox_sql` does. A request bbox across the
antimeridian is refused: ask for its two parts.)

## Storage layout

One Parquet file per (dataset, month, resolution), one row per tile, the pixels of a
tile as a GeoTIFF blob, and one small catalog per dataset that says which tiles exist.
The key of a tile is `(dataset, band, date, acq, crs, x, y)`; tiles are keyed to the
ground, never to a parcel or a project. [docs/storage.md](docs/storage.md) describes
the files on disk, the row schema, two overpasses on one day, how a month is written
and sealed, the row groups, the blob encoding and how UTM zones are handled.

## Development

```bash
pip install -e ".[dev]"                 # the library and pytest
pip install -e ".[dev,cds,pardig]"      # and what the tests of two adapters import
pytest tests/
```

Without the `cds` and `pardig` extras the tests that need them are skipped, and two
tests of what Windows does with a file that is held open run on Windows only.

The tests need no network and no account, and write only to temporary folders.

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
| NASADEM | NASA, public domain |
| ALOS AW3D30 (v3.2) | JAXA terms: free including commercial use, attribution required |
| GEDTM30 (OpenGeoHub) | CC BY 4.0, attribution required |
| FABDEM V1-2 | **CC BY-NC-SA 4.0, non-commercial only**; commercial licence from Fathom |
| USGS 3DEP 1 m DEM | Public domain; USGS credit requested |
| GEM-15 (Google) | CC BY 4.0, attribution required: "This dataset is produced by Google"; version 1 is experimental |
| ESA WorldCover | CC BY 4.0, attribution required |
| SoilGrids (ISRIC) | CC BY 4.0, attribution required |
| CHIRPS 2.0 | Public domain (CHC/UCSB); cite Funk et al. 2015 |
| MODIS MOD11A1/MYD11A1 | NASA LP DAAC, free; cite Wan et al., attribution requested |
| MODIS MOD14A1/MYD14A1, MCD64A1 | NASA LP DAAC, free; cite Giglio et al., attribution requested |
| ERA5 (ARCO-ERA5 mirror) | Copernicus Climate Change Service (C3S) licence: free use with attribution ("Generated using Copernicus Climate Change Service information"); ARCO mirror by Google Research |
| ERA5-Land, AgERA5 (CDS) | C3S licence as above, accepted once per dataset on the CDS website; your account, your requests |
| PARDIG (Google WeatherNext) | CC BY 4.0, attribution required; experimental research product, not an official forecast or warning source |
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
