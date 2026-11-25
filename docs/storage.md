# Storage layout

```
data/
  catalog/<dataset>.parquet                       one row per stored tile
  store/dataset=<d>/<YYYY>/<MM>/r<res>.parquet    sealed month
  store/dataset=<d>/<YYYY>/<MM>/r<res>.part-*.parquet  open parts (hot month)
  store/dataset=<d>/static/r<res>.parquet         static layers
  catalog/<dataset>.parquet.<id>.tmp              a catalog being rebuilt; one left by a killed run is never read, delete it
  store/dataset=<d>/**/*.parquet.tmp              a write in progress; one left by a killed run is never read, delete it
  store/dataset=<d>/**/*.parquet.old              a sealed month moved aside for its new file; never delete one whose month has no sealed file (see Write pattern)
  store/dataset=<d>/**/*.parquet.lock             a month being compacted or re-encoded; one left by a killed run locks nothing, delete it or leave it
  cache/<dataset>/<archive folder>/               downloaded source objects (PARDIG archives); delete when no ingest runs
  logs/<dataset>_<timestamp>.log                  one log per ingest run
```

Row schema: `band, date, x, y, crs, res, tile_px, west/south/east/north (WGS84),
nodata, cloud_pct, scene_id, data (GeoTIFF blob), size_bytes, fetched_at, acq,
valid_px`. `valid_px` is the number of pixels holding an observation. The bounds hold
the ground of the tile, and sometimes more. Those of a tile of a projected CRS across
the antimeridian have `west > east`, as a bbox has in GeoJSON and STAC: its ground
runs from `west` up to 180 and from -180 up to `east`. A tile that holds a pole spans
-180 to 180. A tile of a geographic grid keeps the longitudes of the grid, which can
run past 180.
Logical key: `(dataset, band, date, acq, crs, x, y)`. The CRS is part of the key because
UTM tile numbers restart in every zone: the same `(x, y)` in zones 35 and 36 is ground
six degrees apart. `acq` is part of it because a satellite can image the same ground
twice on one date (next section); it is NULL for every source where that cannot happen.
The catalog carries both, and the reader uses them to pick the right tile. Tiles are
keyed to the ground, never to a parcel or project, so overlapping AOIs share tiles and
deleting an AOI never invalidates data.

## Acquisitions: two overpasses on one day

`date` is a calendar day (UTC), and one day can hold two images of the same ground: a
Sentinel-1 morning and evening pass (on one tile in western Turkey, 528 of the 936
days with data), or two Sentinel-2 satellites (2B and 2A ten minutes apart on
2025-04-14 and 2025-11-09). They are different measurements, so `s2`, `s1` and
`landsat` tiles carry the acquisition they come from in `acq`, which names the
overpass within its date, and two overpasses are two tiles:

| Source | `acq` | Example |
|---|---|---|
| `s2` | datatake id without the processing baseline | `GS2C_20260305T081821_007813` |
| `s1` | platform (lower case) and absolute orbit | `sentinel-1a_63436` |
| `landsat` | platform (lower case) and WRS path | `landsat-9_175` |

The same `acq` comes back on other dates (a Landsat satellite repeats a path every 16
days); it is the date and `acq` together that name one overpass. An overpass that
crosses 00:00 UTC falls on two dates and is stored as two.

One overpass is delivered cut into scenes (MGRS granules, Sentinel-1 slices, Landsat
rows), and a tile across a cut is partly empty in each of them. Scenes of the same
acquisition are therefore merged into one tile when the month is compacted: the scene
with the most observed pixels comes first, ties going to the smaller scene id, and the
others only fill pixels that are still empty. `scene_id` then names every scene, joined
by `|`, and `cloud_pct` is the mean of the scenes' values weighted by the pixels each
contributes. Where scenes overlap their pixels can differ slightly (each granule is
processed on its own); the first scene's are kept, so a tile built from three or more
scenes can differ by those pixels depending on which of them were ingested together.

A later run completes a tile: a stored tile without an empty pixel is never fetched
again; a partly empty one is skipped only for the scenes it already holds, so a scene
found by a later, wider or resumed ingest still adds its part. A scene a tile already
holds is not fetched again, so a scene published anew under the same id does not
replace its pixels.

Reading: `series` returns one record per acquisition, each naming its `acq`.
On an exact date `mosaic` and `clip` never blend two overpasses: `acq=` names the one
to read, and without it they return the one observed on most of the request and warn,
naming it and the others with the number of sample points each observed. An overpass
whose tiles reach the request without an observation on it does not count.
`reader.acquisitions` lists the overpasses with a tile over a bbox. No call
composites the overpasses of one day; read each and combine them as the data allows.
A date prefix (`"2026-03"`) remains a composite over everything it matches, and
`acq=` then narrows it to the acquisitions of that name.

## Write pattern (hot month)

Ingest is a cheap append: each flush writes a new `r<res>.part-<id>.parquet` and
touches nothing else. The Writer flushes every 10 scenes and whenever its buffer
holds more than `--flush-mb` of tiles (default 256 MB; a 900 MB WorldCover run
is four parts). Once a month spans more than 3 files it is auto-compacted
into a single sealed file at close, and **every finished run seals all the months
it touched**, so part files exist only while a run is in progress, after a crash,
or when another program held one open while its month was sealed. `geovault compact
<dataset>` does the same on demand and doubles as repair after either. Readers go through the catalog's `file` column, so parts are transparent
to them either way. Compaction is also where rows sharing a key are resolved: a
re-fetched tile keeps its newest copy, and the scenes of one acquisition are merged
into one tile. Until then such a tile is one row per scene, each partly empty, and
readers see those rows. `Writer.close()` merges the months written since the last
merge; after `close(merge=False)`, or when another process held the month, call
`store.compact(dataset)`. When an ingest could not seal a month it says so and names
the command.

What a failure costs. A part that cannot be written (disk full, a file in the way)
raises; the other months of that flush are still written and the tiles that are not
on disk stay in the Writer's buffer, so a later flush of the same Writer stores them.
In `geovault ingest` an automatic flush that fails is reported on the scenes that add
a tile while the cause lasts, and a failure at the close of a chunk ends the run:
what was flushed before is kept, a re-run fetches the rest.

A compaction puts the new month file in place before it deletes a part, so a file
another program holds open costs no tiles. A part that cannot be deleted stays an
open part whose tiles are in the sealed file too (the catalog lists them twice)
until the next compaction. A reader that holds the sealed file the way DuckDB does,
sharing it for deletion, keeps reading the old file, which is moved aside as
`.parquet.old` and deleted; a program that holds it without that sharing (a plain
file handle, pyarrow) makes the compaction fail and leaves the month as it was. A
sealed file that stays aside, because the run was killed between the two renames or
the file could not be moved back, leaves its month without a sealed file until
`geovault compact` puts it back. Files are not synced to disk before they are
renamed: a power cut right after a compaction can cost the month.

Only one process compacts or re-encodes a month at a time. While it does, it keeps
the file `r<res>.parquet.lock` open and locked through the operating system, and it
deletes the file when it is done. The operating system drops the lock when the
process ends, however it ends, so a lock file left behind by a killed run locks
nothing: the next compaction or re-encoding of that month takes the lock and deletes
the file, and deleting it by hand does no harm. The file holds the number of the
process that has the lock. A lock file that cannot be opened or locked for another
reason (no right to write in the folder, a file system that keeps no locks) is
reported as an error; the data folder has to be on a file system that keeps such
locks, as local disks do.

## File layout (row groups)

A month file is sorted (band, date, x, y) and cut into row groups: a group ends once
it holds 4 MB of tiles, or 2048 tiles. Every group carries the min/max of its key
columns and a reader fetches a group whole, so the size of a group is what one tile
costs to read. Measured on one month file per dataset, one tile read through DuckDB,
with the file as DuckDB's own writer cuts it (no group under 2048 tiles, so these
month files are one to six groups) and with groups of 4 MB:

| dataset | tile | DuckDB writer | 4 MB |
|---|---|---|---|
| `s1` | 211 KB | 112 ms | 5 ms |
| `glo30` | 163 KB | 55 ms | 5 ms |
| `s2` | 92 KB | 88 ms | 6 ms |
| `landsat` | 78 KB | 19 ms | 4 ms |
| `modis_vi` | 39 KB | 40 ms | 5 ms |
| `modis_lst` | 24 KB | 29 ms | 6 ms |
| `agera5` | 22 KB | 27 ms | 5 ms |
| `pardig_nrt` | 1.6 KB | 4 ms | 3 ms (2048 tiles are 3 MB: same layout) |

Smaller is not better: the footer lists every group and is read on every query. The
same `s2` month at other targets:

| group | groups | one tile | listing its keys |
|---|---|---|---|
| 0.5 MB | 532 | 14.5 ms | 78 ms |
| 2 MB | 144 | 6.2 ms | 19 ms |
| 4 MB | 73 | 5.6 ms | 11 ms |
| 16 MB | 19 | 10.8 ms | 4 ms |

Listing keys is what a catalog rebuild and the skip set of an ingest do, so those
pay for the layout: the catalog query over 171 `s2` month files takes 0.9 s instead
of 0.2 s. Either way the file size differs by less than 1%. The files are written with pyarrow, because DuckDB's `COPY` cannot
write a group smaller than 2048 rows. Min/max statistics are kept for the key
columns, `scene_id` and the bounds; `nodata` and `cloud_pct` have none, since a NaN
is left out of them and a filter on such a column would then skip its group.

## Blob encoding

Every tile is a single-band DEFLATE GeoTIFF; the codec choice is lossless and per
tile. Integer tiles (S2, Landsat, MODIS, WorldCover) use predictor 2. Float tiles
are encoded twice, with predictor 2 and with the floating-point predictor 3, and the
smaller one is stored (the choice is a TIFF tag, so any reader decodes either).
Measured on Turkey tiles: predictor 3 is 12-17% smaller on smooth continuous fields
(AgERA5 temperatures, ET0, radiation; S1 gamma0 -7%) and 30% LARGER on sparse
quantised ones (precipitation), which is why neither is right for a whole dataset.

Where the volume comes from, measured on AgERA5 (9.6 GB of blobs before the predictor
choice, 8.5 GB after, for 12 bands x 6 tiles x 18.7 years; 21 KB per 100 x 100 float32
tile): the source NetCDF itself is 48 KB for
the same tile (zlib + shuffle), and the values are full float32 with no quantisation
(unique-value step = one float32 ulp), so the size is the entropy of the data, not a
conversion overhead. The only large lever left is lossy: rounding the mantissa to 16
bits (max error 0.002 K on temperature, 8e-6 relative) would save a further ~35%,
14 bits (0.008 K) ~45%. The vault does not do this; the lossless rule stands.

`geovault reencode <dataset>` rewrites every stored tile with the current encoder,
month by month (merging open parts like compaction, taking the same lock), and keeps
a blob only when the new encoding is smaller. Pixels, georeferencing, nodata and
scale/offset are unchanged bit-for-bit; run it after an encoder change to bring old
rows up to date. Done 2026-09-18: `agera5` 404k tiles 9.62 -> 8.52 GB (-11%, 17 min),
`s1` 15k tiles 3.47 -> 3.22 GB (-7%). `chirps` gains nothing (predictor 2 already wins
on precipitation) and is left as is.

Reading goes the other way through `geovault.blob.decode`, which every function of
the reader that returns pixels uses and a project that fetches blobs with its own
query can call:

```python
from geovault.blob import decode
arr, profile = decode(blob)         # what rasterio returns for the GeoTIFF: band 1 and the profile
```

A blob is a GeoTIFF any tool opens, but opening a small one through GDAL costs more
than its pixels do (0.54 ms for a 50 x 50 float32 tile, 1.5 ms for 256 x 256 uint16),
and a series at a point opens one tile per date. `decode` inflates the strips with
zlib and undoes the predictor with numpy: 0.03 ms for that 50 x 50 tile, 0.67 ms for
the one of 256 x 256. The result is the same bit for bit and key for key: the profile
of each kind of tile (size, data type, nodata, CRS) is taken from GDAL once, for the
first tile of the kind, which is also decoded both ways as a check. A GeoTIFF that is
not laid out as the encoder writes a tile, and a tile whose strips are not what its
size calls for, is read by GDAL.

## UTM zones: which CRS a tile is stored in

Tiles are stored in the CRS their scene comes in, and the CRS is part of the tile key.
What that means near a zone line depends on how the product is gridded.

**Sentinel-2** follows the MGRS grid. Granules of neighbouring zones overlap by tens of
kilometres, and so do the granules north and south of the equator, so the same ground
arrives on the same day in two CRS; the key cannot tell they are the same ground. One of
the two is kept: a tile is stored when it touches the ground MGRS assigns to its CRS (the
6 degree zone on one side of the equator; zone 32 widened to 3-12 E between 56 and 64 N;
the wide zones 31, 33, 35 and 37 between 72 and 84 N). Every point has a granule in its
own zone and lies in a kept tile of it, so nothing is lost. Each zone keeps tiles up to
one tile past the line, so a strip about one tile wide on average, at most two, is stored
in both CRS (measured along the 36 E line between 36 and 42 N: 2.6 km on average at 10 m,
5.4 km at 20 m, 15 km at 60 m). `--keep-offzone` stores every tile of every granule
instead.

**Landsat and Sentinel-1** scenes come in the zone of the scene, not of the ground:
within half a scene's width of a zone line (up to about 90 km for Landsat, more for
Sentinel-1) the same ground is imaged in one zone's CRS on some dates and in the
neighbour's on others, and Landsat uses the northern CRS on both sides of the equator.
Every scene is stored in its own CRS, with no rule by location: such a rule would drop
those dates, not a duplicate. Two overlapping scenes of one pass can fall in different
zones; their overlap is then stored in both CRS for that date.

For reading this means:

- `mosaic` and `clip` return one CRS per call. Over an area stored in several they use
  the zone MGRS assigns to the bbox center when it has tiles, else the CRS with the most
  tiles, and warn when tiles of another CRS hold observations on ground of the request
  where the result has none (a polygon across a zone line: read each side separately).
  The check compares observed pixels on a lattice of points over the request; a left-out
  strip narrower than an eighth of a tile can go unnoticed on a large request.
- Near a zone line a Landsat or Sentinel-1 series can change CRS from one date to the
  next, so two dates of the same bbox are not always on the same pixel grid. Compare the
  returned `crs` and `transform` before stacking dates.
- `series` returns one record per stored tile at the point, so a point stored in two
  CRS (the Sentinel-2 strip, overlapping Landsat or Sentinel-1 scenes of two zones)
  yields two records for a date; each record names its `crs`.
