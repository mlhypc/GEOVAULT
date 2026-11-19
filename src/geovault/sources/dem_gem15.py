"""GEM-15: Google's surface and terrain elevation models at 15 m, version 1 (experimental).

Elevation from stereo satellite imagery, aggregated to 15 m: a surface model (DSM) with
the spread of the finer heights inside each pixel, and a terrain model (DTM). Float32
metres, NaN where there is no value (water, snow and ice, areas left out). The heights
are above the EGM96 geoid; that is in the product's documentation, not in the files.
Public bucket, no account. Licence CC BY 4.0, attribution "This dataset is produced by
Google". The publisher calls version 1 experimental.

Bands, named as published: dsm, dsm_percentile_5, dsm_percentile_50, dsm_percentile_95
and dsm_std (one file of five bands per cell), and dtm (a second file).

The product is cut into cells of 1 x 1 degree, each in the UTM zone it lies in, all
cells of a zone on one 15 m lattice. A file is padded with NaN far past its cell and
holds values about 1 km past it on every side, and there the cells do not agree: they
were made one by one. Measured on the DTM where 35 E meets 38.5 N, two cells differ by
0.7 m in the median over the ground both hold, by 9.5 m at most; at the corner of four
cells at 35 E, 39 N by 0.1 to 0.3 m in the median. So a tile on a cell line is put
together from the cells that meet in it, every pixel from the cell its centre lies in
(a cell owns west <= longitude < west + 1 and south <= latitude < south + 1), and what a
cell holds past its own ground is not stored. The step between two cells stays where the
product has it, on the degree line.

A tile holds the pixels of the cells of its own UTM zone. Ground of a cell in the
neighbouring zone is left empty in it and stored in that zone's tiles, so no ground is
stored twice.

A tile is stored once every cell it takes pixels from has been opened, or is known not
to exist (open water, a country the product leaves out: the bucket answers 404). When a
cell fails for any other reason the tile is left out and the run says so; it would
otherwise be stored without that cell's pixels and skipped as stored by every later run.
"""

import math
import time

import numpy as np
import rasterio
import requests
from rasterio.errors import RasterioIOError
from rasterio.windows import Window

from ..aoi import tile_filter
from ..grid import Grid
from ..log import fmt_duration
from . import common, static_cog

STATIC = True
DSM_BANDS = ["dsm", "dsm_percentile_5", "dsm_percentile_50", "dsm_percentile_95", "dsm_std"]
ASSETS = {"dsm": DSM_BANDS, "dtm": ["dtm"]}     # file of a cell -> its bands, in file order
DEFAULT_BANDS = DSM_BANDS + ["dtm"]
RES_LIST = [15.0]
TILE_PX = 256           # 3.84 km; the files are tiled in blocks of 512, two tiles a side
NODATA = float("nan")
SOURCE = "google-gem-15-v1"

_BASE = "https://storage.googleapis.com/climate-and-sustainability/gem-15"
_MISSING, _FAILED = "missing", "failed"
PROGRESS_EVERY = 500    # tiles between progress lines


def cell_id(lat: int, lon: int) -> str:
    """Name of the cell with south-west corner (lat, lon): w35s38, wm41sm11."""
    name = lambda v: f"m{-v}" if v < 0 else str(v)
    return f"w{name(lon)}s{name(lat)}"


def group_id(lat: int, lon: int) -> str:
    """Name of the 10 x 10 degree folder holding the cell: p30_p30, m20_m50, p0_m10."""
    name = lambda v: f"{'m' if v < 0 else 'p'}{abs(v)}"
    return f"{name(lat // 10 * 10)}_{name(lon // 10 * 10)}"


def asset_url(lat: int, lon: int, asset: str) -> str:
    return f"{_BASE}/{group_id(lat, lon)}/{cell_id(lat, lon)}/{asset}/zall/mosaic.tif"


def cells_of(bounds) -> list:
    """South-west corners (lat, lon) of the cells that WGS84 bounds (w, s, e, n) touch;
    w > e is a tile across the antimeridian."""
    w, s, e, n = bounds
    if w <= e:
        lons = range(math.floor(w), math.ceil(e))
    else:
        lons = [*range(math.floor(w), 180), *range(-180, math.ceil(e))]
    return [(lat, lon) for lat in range(math.floor(s), math.ceil(n)) for lon in lons]


def _exists(url: str):
    """True when the bucket holds the file, False when it answers that it does not
    (404), None when it gives any other answer or none: only a 404 says there is no
    such cell."""
    try:
        status = requests.head(url, timeout=60).status_code
    except requests.RequestException:
        return None
    return True if status == 200 else False if status == 404 else None


class _Cells:
    """The files of one asset, opened when a tile first needs them and kept open for
    the run. A cell is an open dataset, _MISSING (the product has no such cell) or
    _FAILED (it could not be asked for or opened this time)."""

    def __init__(self, asset: str, log):
        self.asset, self.log, self.cells = asset, log, {}

    def get(self, lat: int, lon: int):
        if (lat, lon) not in self.cells:
            self.cells[(lat, lon)] = self._open(lat, lon)
        return self.cells[(lat, lon)]

    def _open(self, lat: int, lon: int):
        url, name = asset_url(lat, lon, self.asset), f"{cell_id(lat, lon)}/{self.asset}"
        there = _exists(url)
        if there is False:
            return _MISSING
        if there is None:
            self.log(f"    {name}: FAILED, the bucket did not answer for it")
            return _FAILED
        try:
            ds = rasterio.open(url)
        except RasterioIOError as exc:
            self.log(f"    {name}: FAILED to open ({exc})")
            return _FAILED
        try:
            _check(ds, self.asset, name)
        except ValueError:
            ds.close()
            raise
        return ds

    def close(self):
        for ds in self.cells.values():
            if ds not in (_MISSING, _FAILED):
                ds.close()


def _check(ds, asset: str, name: str) -> None:
    """Refuse a file that is not what version 1 publishes: tiles are cut from the files
    pixel for pixel, which holds only on the product's own lattice."""
    t = ds.transform
    on_lattice = (t.a == RES_LIST[0] and t.e == -RES_LIST[0] and not t.b and not t.d
                  and t.c % RES_LIST[0] == 0 and t.f % RES_LIST[0] == 0)
    nodata_is_nan = ds.nodata is not None and ds.nodata != ds.nodata
    if (not on_lattice or ds.count != len(ASSETS[asset]) or ds.dtypes[0] != "float32"
            or not nodata_is_nan or ds.crs is None or ds.crs.to_epsg() is None):
        raise ValueError(
            f"{name}: not a GEM-15 version 1 file (expected {len(ASSETS[asset])} float32 "
            f"bands with nodata NaN on a 15 m lattice in a UTM zone; found {ds.count} "
            f"{ds.dtypes[0]} bands, nodata {ds.nodata}, transform {tuple(t)[:6]}, CRS {ds.crs})")


def _crs(ds) -> str:
    return f"EPSG:{ds.crs.to_epsg()}"


def _compose(owners, grid, tx, ty, count, inside):
    """(array of `count` bands, the cells that gave a value) for one tile: every pixel
    from the cell its centre lies in. owners: (lat, lon, dataset) of the cells of the
    tile's CRS that the tile touches. inside: the tile lies wholly in its one cell, so
    no pixel has to be looked up."""
    px = grid.tile_px
    w, s, e, n = grid.tile_bounds(tx, ty)
    out = np.full((count, px, px), np.nan, np.float32)
    if not inside:
        centre = (np.arange(px) + 0.5) * grid.res
        x, y = np.meshgrid(w + centre, n - centre)
        lon, lat = common._transformer(grid.crs, "EPSG:4326").transform(x, y)
        cell_lon, cell_lat = np.floor(lon), np.floor(lat)
    used = []
    for lat0, lon0, ds in owners:
        own = None if inside else (cell_lon == lon0) & (cell_lat == lat0)
        if own is not None and not own.any():
            continue
        b = ds.bounds
        if e <= b.left or w >= b.right or n <= b.bottom or s >= b.top:
            continue
        # the files are on the tile lattice (_check), so the window is whole pixels. A
        # window inside the file, as nearly all are, is read as it is: asking for a
        # boundless read has rasterio build a virtual raster around every one
        t = ds.transform
        window = Window(round((w - t.c) / t.a), round((n - t.f) / t.e), px, px)
        whole = (window.col_off >= 0 and window.row_off >= 0
                 and window.col_off + px <= ds.width and window.row_off + px <= ds.height)
        arr = ds.read(window=window) if whole else ds.read(window=window, boundless=True,
                                                           fill_value=np.nan)
        if arr.shape != out.shape:
            raise ValueError(f"{cell_id(lat0, lon0)}: read {arr.shape} for a tile of {out.shape}")
        if own is None:
            out = arr
        else:
            out[:, own] = arr[:, own]
        if np.isfinite(arr if own is None else arr[:, own]).any():
            used.append(cell_id(lat0, lon0))
    return out, used


def _ingest_asset(cells, bbox, bands, skip_keys, aoi, in_aoi, writer, log) -> tuple:
    """Store the tiles of one asset (the DSM file or the DTM file of every cell) over
    bbox / AOI. Returns (tiles stored, cells and tiles left out because a cell failed)."""
    names = ASSETS[cells.asset]
    by_crs, missing, failed = {}, 0, 0
    for lat, lon in static_cog.degree_cells(bbox, aoi):
        ds = cells.get(lat, lon)
        if ds is _MISSING:
            missing += 1
        elif ds is _FAILED:
            failed += 1                     # none of its tiles is planned below
        else:
            by_crs.setdefault(_crs(ds), []).append((lat, lon))
    log(f"  {cells.asset}: {sum(len(v) for v in by_crs.values())} cells in "
        f"{len(by_crs)} UTM zones, {missing} cells without data")
    stored = 0
    t0 = time.time()
    for crs, own_cells in sorted(by_crs.items()):
        grid = Grid(crs, RES_LIST[0], TILE_PX)
        todo = set()
        for lat, lon in own_cells:
            part = (max(bbox[0], lon), max(bbox[1], lat), min(bbox[2], lon + 1), min(bbox[3], lat + 1))
            if part[0] < part[2] and part[1] < part[3]:
                todo.update(grid.tiles_for_bounds(*common.bbox_to_crs(part, crs)))
        todo = sorted(todo, key=lambda t: (t[1], t[0]))     # row by row: neighbours share blocks
        for k, (tx, ty) in enumerate(todo, 1):
            if k % PROGRESS_EVERY == 0:
                log(f"    {cells.asset} {crs}: {k}/{len(todo)} tiles looked at, {stored} tiles "
                    f"stored, elapsed {fmt_duration(time.time() - t0)}")
            wanted = [b for b in bands if (b, "", crs, tx, ty) not in skip_keys]
            if not wanted:
                continue
            wb = common.wgs84_bounds(grid, tx, ty, crs)
            if not in_aoi(wb):
                continue
            touched = [(lat, lon, cells.get(lat, lon)) for lat, lon in cells_of(wb)]
            # a cell that failed was never opened, so its UTM zone is not known: the tile
            # waits for it even where it would turn out to be a cell of the next zone
            if any(ds is _FAILED for _, _, ds in touched):
                failed += 1
                continue
            owners = [(lat, lon, ds) for lat, lon, ds in touched
                      if ds is not _MISSING and _crs(ds) == crs]
            if not owners:
                continue
            inside = len(touched) == 1
            try:
                arr, used = _compose(owners, grid, tx, ty, len(names), inside)
            except RasterioIOError as exc:
                log(f"    {cells.asset} {crs} tile {tx}/{ty}: FAILED to read ({exc})")
                failed += 1
                continue
            scene = f"{SOURCE}:{'|'.join(sorted(used))}"
            for band in wanted:
                tile = arr[names.index(band)]
                valid = int(np.count_nonzero(np.isfinite(tile)))
                if valid and writer.add(band, "", tx, ty, crs, grid.res, TILE_PX, wb, NODATA,
                                        None, scene, common.encode_geotiff(tile, grid, tx, ty, crs, NODATA),
                                        valid):
                    stored += 1
    return stored, failed


def ingest(bbox, writer, bands=None, skip_keys=None, aoi=None, log=print) -> int:
    bands = bands or DEFAULT_BANDS
    unknown = [b for b in bands if b not in DEFAULT_BANDS]
    if unknown:
        raise ValueError(f"gem15 has no band {', '.join(unknown)}; its bands are {', '.join(DEFAULT_BANDS)}")
    skip_keys = skip_keys or set()
    in_aoi = tile_filter(aoi)
    written = left_out = 0
    with rasterio.Env(**static_cog.GDAL_ENV):
        for asset, names in ASSETS.items():
            wanted = [b for b in names if b in bands]
            if not wanted:
                continue
            cells = _Cells(asset, log)
            try:
                stored, failed = _ingest_asset(cells, bbox, wanted, skip_keys, aoi, in_aoi, writer, log)
            finally:
                cells.close()
            log(f"    {asset}: {stored} tiles")
            written += stored
            left_out += failed
    if left_out:
        writer.leave_out(left_out)
        log(f"    {left_out} cells or tiles were left out because a cell could not be opened or "
            f"read; run the same command again to fetch them")
    return written
