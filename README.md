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

## Data sources

| Dataset | Product | Source | Auth |
|---------|---------|--------|------|
| `s2` | Sentinel-2 L2A, 13 bands, 10/20/60 m | AWS earth-search (element84) STAC + COG | none |
| `s1` | Sentinel-1 RTC (terrain-corrected gamma0), VV/VH, 10 m | Microsoft Planetary Computer | none |
| `glo30` | Copernicus GLO-30 DEM, 30 m, static | AWS open data COG | none |
| `worldcover` | ESA WorldCover 2021, 10 m land cover, static | AWS open data COG | none |
| `soilgrids` | SoilGrids 250 m soil properties (11 props x 6 depths), static | ISRIC VRT/COG | none |

Static datasets need no date range:

```bash
geovault ingest --dataset glo30 --geojson parcel.geojson
geovault ingest --dataset worldcover --geojson parcel.geojson
geovault ingest --dataset soilgrids --geojson parcel.geojson            # topsoil defaults
geovault ingest --dataset soilgrids --geojson parcel.geojson --bands clay_0-5cm clay_5-15cm
```

SoilGrids is stored in its native Goode Homolosine projection (no resampling);
the reader's clip reprojects the polygon, not the pixels.

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

geovault coverage s2 --date 2024-06     # what is stored
geovault compact s2                     # seal open month part files
geovault rebuild-catalog s2             # regenerate the catalog from the store
```

Re-running an ingest is safe and cheap: cached tiles are skipped via the catalog,
so the same command serves both the initial backfill and incremental top-ups.

Note on clouds: `--cloud-max` filters on the scene-level (whole MGRS square)
average. Keep it loose (around 80) and select per tile at read time using the
stored `cloud_pct`, which is computed locally per tile from SCL pixels.

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

Ingest is a cheap append: each run writes a new `r<res>.part-<id>.parquet` and
touches nothing else. Once a month spans more than 3 files it is auto-compacted
into a single sealed file at close; `geovault compact <dataset>` does the same
on demand and doubles as crash repair. Readers go through the catalog's `file`
column, so parts are completely transparent.

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
| Copernicus GLO-30 DEM | Copernicus DEM license (ESA / Airbus terms) |
| ESA WorldCover | CC BY 4.0, attribution required |
| SoilGrids (ISRIC) | CC BY 4.0, attribution required |
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
