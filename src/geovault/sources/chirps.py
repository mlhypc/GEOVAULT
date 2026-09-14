"""CHIRPS 2.0 daily precipitation (Climate Hazards Center, UCSB), anonymous.

Global 50S-50N, 0.05 degree (~5 km), EPSG:4326, one GeoTIFF per day, mm/day as
float32, 1981 to a few days ago. Two streams under data.chc.ucsb.edu:

    global_daily/tifs/p05/<YYYY>/chirps-v2.0.<YYYY>.<MM>.<DD>.tif.gz          final
    prelim/global_daily/tifs/p05/<YYYY>/chirps-v2.0.<YYYY>.<MM>.<DD>.tif.gz   preliminary

Final lags about three weeks after month end (station data merged in); prelim
is ~2 days behind real time. A day is fetched from final when it exists, else
from prelim, and a stored prelim tile is REPLACED the day final appears
(scene_id tells them apart: chirps-final / chirps-prelim; compaction keeps the
newest fetched_at).

Why this is the one meteorology source that fits the vault's rules: no
account, no remote compute, native grid, lossless float32. The one deviation is
that the files are gzip-compressed plain GeoTIFFs, so a day cannot be
range-read: the whole global file (2-11 MB) is downloaded, decompressed in
memory and tiled locally. One file per day covers any AOI, so a Turkey ingest
costs exactly one download per day.

Grid: 0.05 degree pixels, 100 px tiles = 5 degrees, lattice anchored at (0, 0)
(the file origin -180/50 is a multiple of 5, so tile edges fall on file edges).
Turkey is ~10 tiles. nodata is -9999 (the files carry no nodata tag; set here).
Sea is nodata too, so a coastal tile is stored with its land pixels only.

Band: `precip` (mm/day). cloud_pct is None (not a satellite product).
"""

import gzip
import io
import re
from datetime import date as _date, timedelta

import numpy as np
import rasterio
import requests

from ..grid import Grid
from . import common

BASE = "https://data.chc.ucsb.edu/products/CHIRPS-2.0"
FINAL_DIR = BASE + "/global_daily/tifs/p05/{year}/"
PRELIM_DIR = BASE + "/prelim/global_daily/tifs/p05/{year}/"
FNAME = "chirps-v2.0.{y}.{m:02d}.{d:02d}.tif.gz"
_LIST_RE = re.compile(r'chirps-v2\.0\.(\d{4})\.(\d{2})\.(\d{2})\.tif\.gz')

CRS = "EPSG:4326"
RES = 0.05
TILE_PX = 100                 # 5 degrees; -180 and 50 are multiples of 5 -> anchor (0, 0)
NODATA = -9999.0
BAND = "precip"
DEFAULT_BANDS = [BAND]
SCENE_FINAL = "chirps-final"
SCENE_PRELIM = "chirps-prelim"
TIMEOUT = 120


class Item:
    """Minimal STAC-like item the CLI understands: .id and .properties['datetime']."""

    def __init__(self, day: _date, kind: str):
        self.day = day
        self.kind = kind                                   # 'final' | 'prelim'
        base = PRELIM_DIR if kind == "prelim" else FINAL_DIR
        self.href = base.format(year=day.year) + FNAME.format(y=day.year, m=day.month, d=day.day)
        self.id = f"chirps-{kind}-{day.isoformat()}"
        self.properties = {"datetime": f"{day.isoformat()}T00:00:00Z"}

    @property
    def scene_id(self):
        return SCENE_FINAL if self.kind == "final" else SCENE_PRELIM


def grid_for() -> Grid:
    return Grid(CRS, RES, TILE_PX, 0.0, 0.0)


def _listing(url) -> set:
    """Dates present in one year directory of a stream (one HTTP GET); empty set on 404."""
    r = requests.get(url, timeout=TIMEOUT)
    if r.status_code == 404:
        return set()
    r.raise_for_status()
    return {_date(int(y), int(m), int(d)) for y, m, d in _LIST_RE.findall(r.text)}


def search(bbox, start, end, cloud_max=None, limit=None):
    """Days in [start, end] available on the server, final preferred over prelim, oldest first.
    bbox and cloud_max are accepted for CLI symmetry and ignored (global daily files)."""
    d0, d1 = _date.fromisoformat(start), _date.fromisoformat(end)
    final, prelim = set(), set()
    for year in range(d0.year, d1.year + 1):
        final |= _listing(FINAL_DIR.format(year=year))
        prelim |= _listing(PRELIM_DIR.format(year=year))
    items = []
    day = d0
    while day <= d1:
        if day in final:
            items.append(Item(day, "final"))
        elif day in prelim:
            items.append(Item(day, "prelim"))
        day += timedelta(days=1)
    return items[:limit] if limit else items


def _prelim_keys_in_store(dataset, day) -> set:
    """(band, date, x, y) of this day's tiles that are stored from the prelim stream.
    A final item must re-fetch those, so they are removed from skip_keys."""
    from .. import catalog
    import duckdb
    p = catalog.path(dataset)
    if not p.exists():
        return set()
    con = duckdb.connect()
    rows = con.execute(
        f"SELECT band, date, x, y FROM read_parquet('{p.as_posix()}') "
        f"WHERE date = ? AND scene_id = ?", [day.isoformat(), SCENE_PRELIM]).fetchall()
    con.close()
    return set(rows)


def _effective_skip(item, skip_keys, dataset):
    skip_keys = set(skip_keys or ())
    if item.kind == "final":
        skip_keys -= _prelim_keys_in_store(dataset, item.day)
    return skip_keys


def _todo(item, bbox, skip_keys, aoi, dataset="chirps"):
    from ..aoi import tile_filter
    grid = grid_for()
    skip = _effective_skip(item, skip_keys, dataset)
    in_aoi = tile_filter(aoi)
    date = item.day.isoformat()
    # keep_offzone=True: a single global lat/lon CRS, the UTM zone rule does not apply
    return grid, common.plan_tiles(grid, bbox, CRS, BAND, date, skip, True, in_aoi)


def plan_scene(item, bbox, bands=None, skip_keys=None, keep_offzone=False, aoi=None) -> dict:
    """band -> tiles this day would still fetch (nothing is downloaded)."""
    _, todo = _todo(item, bbox, skip_keys, aoi)
    return {BAND: len(todo)} if todo else {}


def _download(href) -> bytes:
    r = requests.get(href, timeout=TIMEOUT)
    r.raise_for_status()
    return gzip.decompress(r.content)


def ingest_scene(item, bbox, writer, bands=None, skip_keys=None, log=print,
                 keep_offzone=False, aoi=None) -> int:
    """One day: download the global file once, cut the tiles over bbox/aoi, store them."""
    grid, todo = _todo(item, bbox, skip_keys, aoi, writer.dataset)
    if not todo:
        return 0
    date = item.day.isoformat()
    raw = _download(item.href)
    written = 0
    with rasterio.MemoryFile(raw) as mem, mem.open() as ds:
        for tx, ty in todo:
            arr = common.read_tile_window(ds, grid, tx, ty, NODATA)
            if arr is None:
                continue                       # open sea
            arr = arr.astype(np.float32, copy=False)
            blob = common.encode_geotiff(arr, grid, tx, ty, CRS, NODATA)
            if writer.add(BAND, date, tx, ty, CRS, RES, TILE_PX,
                          common.wgs84_bounds(grid, tx, ty, CRS), NODATA, None,
                          item.scene_id, blob, int((arr != NODATA).sum())):
                written += 1
    log(f"    {written} tiles ({item.kind})")
    return written
