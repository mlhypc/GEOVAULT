# Adding a source

A dataset is one module in `src/geovault/sources/` and one line in `REGISTRY`
(`sources/__init__.py`). The engine (grid, store, catalog, reader) does not change for
a new dataset. This page lists what the command line asks a module for; the modules
named as examples are the shortest ones of their kind.

## What every source keeps to

- **One adapter per published product, named after it.** A derived or fused product is
  made by the project that needs it, from tiles read out of the vault.
- **Stored as published.** Native CRS, native pixel lattice, native data type and
  nodata value, no resampling. A tile is a window of the source raster.
- **One lattice per product.** A `Grid` (`grid.py`) is a CRS, a pixel size, a tile size
  in pixels and an anchor. The anchor is a property of the product, never of a file or
  a run, so the same ground always gets the same tile numbers. Pick a tile size that
  divides the size of the source files where they abut; where they overlap or their
  edges cut through tiles, say in the adapter which file a pixel is taken from
  (`dem_3dep_1m`, `dem_gem15`).
- **Zero remote compute.** List what exists, then read the windows that are needed with
  HTTP range requests. No service that computes on the provider's side.
- **A partly read tile is not stored.** A tile that is stored is skipped by every later
  run, so a tile that could not be read whole (a file that failed to open, a scene that
  raised) is left out and reported, and the same command fetches it later. A static
  source counts what it left out with `writer.leave_out(n)`; the command then exits
  with 2, as it does for a dated source when a scene failed.
- **The first sentence of the module docstring** is what `geovault datasets` prints. The
  rest of it is where the next reader learns the product: its bands, its licence, its
  vertical datum, how its files are laid out and what is odd about them.

## A static source

A layer without dates (an elevation model, land cover, soil): `date` is the empty
string and the tiles live in `static/`. Example: `dem_glo30.py`, 32 lines.

```python
STATIC = True
RES_LIST = [1.0 / 3600.0]            # pixel sizes, in the units of the CRS
DEFAULT_BANDS = ["DEM"]

def ingest(bbox, writer, bands=None, skip_keys=None, aoi=None, log=print) -> int:
    """Store the tiles over bbox / aoi that are not in skip_keys; return how many."""
```

- `bbox` is `(west, south, east, north)` in WGS84 and `aoi` a shapely geometry or
  `None`; `aoi.tile_filter(aoi)` gives the test a tile's WGS84 bounds have to pass.
- `skip_keys` is the set of `(band, "", crs, x, y)` already stored.
- A `workers` argument is passed on from `--workers` when `ingest` takes one.
- For files that tile cleanly, `static_cog.ingest_assets` does the whole job: give it
  the URLs, the band name and the tile size.

## A dated source

A layer with dates: the command line finds the scenes of a date range and stores them
one by one, a few at a time. Example: `chirps.py`.

```python
RES = 0.05                           # or BAND_TO_ASSET = {band: (asset key, pixel size)}

def search(bbox, start, end, cloud_max=None, limit=None) -> list:
    """The scenes over bbox between two dates, oldest first."""

def ingest_scene(item, bbox, writer, bands=None, skip_keys=None, log=print,
                 keep_offzone=False, aoi=None) -> int:
    """Store the tiles of one scene that are not in skip_keys; return how many."""
```

- A scene is any object with an `id` and `properties["datetime"]` (ISO 8601). A scene
  that spans several days also gives `start_datetime` and `end_datetime`, so that the
  months it writes to are known before it is read.
- `skip_keys` is the set of `(band, date, crs, x, y)` already stored in those months.
- `ingest_scene` is called from several threads at once; `Writer.add` is safe to call
  from them, an open raster is not shared between them.
- Optional: `plan_scene(item, bbox, bands, skip_keys, keep_offzone, aoi)` returns
  `{band: tiles still to fetch}` without reading anything. With it the dataset supports
  `--dry-run`, the way to check that an ingest left no gap.
- Optional: `ACQUISITIONS = True` for a source that can image the same ground twice on
  one date. Tiles are then added with `acq=` (the name of the overpass), `skip_keys`
  becomes the mapping `Writer.existing_scenes` returns, and scenes of one overpass
  that each hold part of a tile are merged when the month is compacted. See
  `s1_pc.py` and [Acquisitions](storage.md#acquisitions-two-overpasses-on-one-day).

## Writing a tile

```python
writer.add(band, date, x, y, crs, res, tile_px, wgs84_bounds, nodata, cloud_pct,
           scene_id, blob, valid_px, acq=None)
```

- `blob` comes from `common.encode_geotiff(arr, grid, x, y, crs, nodata)`, lossless.
- `wgs84_bounds` from `common.wgs84_bounds(grid, x, y, crs)`.
- `valid_px` is the number of pixels that hold an observation
  (`common.observed(arr, nodata)`; a nodata value of NaN counts like any other). A tile
  without one is not stored.
- `scene_id` says where the pixels came from: the scene, the file, the cells.
- `cloud_pct` is `None` for anything that is not an optical image.

## Before it is done

- Tests that need no network: the adapter run on small files written by the test, laid
  out the way the product lays its files out (`tests/test_gem15.py`,
  `tests/test_dem_sources.py`).
- One run against the real source into a temporary data folder
  (`GEOVAULT_DATA=/tmp/vault geovault ingest ...`), on a small area, and its tiles
  compared with the source read directly.
- A row in the source table of the README, the licence in its licensing table, a
  section in [sources.md](sources.md) when the product needs more than a row, and an
  entry in the changelog.
