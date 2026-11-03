"""Sentinel-3 SYNERGY V10: 10-day NDVI composites at 1 km (SY_2_V10, OLCI + SLSTR, "VGT-S10"
continuity product), via Microsoft Planetary Computer. Free, no account (SAS signing).

Why it is here: the one anonymous NDVI record that runs without a hole from October 2019 to
today at a steady 10-day cadence, from a sensor family that continues after MODIS Terra/Aqua
end in 2027. Coarser and less validated for crops than MODIS, but gapless.

STAC collection `sentinel-3-synergy-v10-l2-netcdf`. One item per satellite (S3A, S3B) per
dekad per continent window; the `_EUROPE` window (-11..62 E, 25..75 N) covers Turkey whole,
so only EUROPE items are ingested and the others are ignored. Both satellites are kept as
separate bands (`s3a_ndvi`, `s3b_ndvi`); S3A has a known hole in September 2020. Each item is
a .SEN3 folder of netCDF4/HDF5 files; the `ndvi` asset is NDVI.nc with one uint8 variable
(scale 0.004, offset -0.08, fill 255, valid 0-250) chunked 1401 x 2044. Only the chunks under
the requested tiles are fetched, through h5py over an HTTP file (about 6 MB per dekad for
Turkey instead of the 21 MB file).

Grid: plate carree, 1/112 degree pixels whose EDGES sit at (k + 1/2) / 112 (pixel centres on
the k/112 lines, as the file's lat/lon bounds show), tiles of 56 px = 0.5 degree, anchored at
1/224. A tile index therefore means the same ground in every dekad and both satellites.
Scene date = dekad start (01, 11, 21 of the month). cloud_pct = share of fill pixels.
The processing baseline changed from 002 to 003 in 2026 (visible in the product name).

Extras: `pip install -e .[syn]` (h5py, fsspec, aiohttp).
"""

import os
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import planetary_computer as pc
from pystac_client import Client

from ..grid import Grid
from . import common

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "sentinel-3-synergy-v10-l2-netcdf"
WINDOW = "EUROPE"                      # continent window whose bbox holds all of Turkey
CRS = "EPSG:4326"
RES = 1.0 / 112.0
TILE_PX = 56                           # 0.5 degree tiles
ANCHOR = 1.0 / 224.0                   # pixel edges at (k + 1/2) / 112
NODATA = 255
SCALE = 0.004
OFFSET = -0.08
VAR = "NDVI"
WORKERS = 2

SATS = {"Sentinel-3A": "s3a", "Sentinel-3B": "s3b"}
ASSETS = {"ndvi": ("ndvi", NODATA, SCALE, OFFSET)}
ALL_BANDS = [f"{s}_{k}" for s in SATS.values() for k in ASSETS]
DEFAULT_BANDS = list(ALL_BANDS)


def grid_for() -> Grid:
    return Grid(CRS, RES, TILE_PX, ANCHOR, ANCHOR)


def _sat(item) -> str:
    p = str(item.properties.get("platform", ""))
    if p in SATS:
        return SATS[p]
    return "s3a" if item.id.startswith("S3A") else "s3b"


def _date(item) -> str:
    return item.properties["datetime"][:10]


def _bands_of(item, bands):
    sat = _sat(item)
    out = []
    for b in bands:
        s, _, k = b.partition("_")
        if s == sat and k in ASSETS and ASSETS[k][0] in item.assets:
            out.append((b, *ASSETS[k]))
    return out


def search(bbox, start, end, cloud_max=None, limit=None):
    """STAC search -> EUROPE-window items of both satellites, oldest first. The item datetime
    is set to the dekad start so the CLI and the store key on the period start."""
    client = Client.open(STAC_URL, modifier=pc.sign_inplace)
    items = [i for i in client.search(
        collections=[COLLECTION], bbox=list(bbox), datetime=f"{start}/{end}",
    ).items() if i.id.endswith("_" + WINDOW)]
    for i in items:
        i.properties["datetime"] = i.properties["start_datetime"]
    items.sort(key=lambda i: (i.properties["datetime"], i.id))
    return items[:limit] if limit else items


def _open(href):
    """h5py File over an HTTP range-read file object (fsspec); the SAS token is in the href."""
    import fsspec
    import h5py
    f = fsspec.open(href, mode="rb", block_size=4 * 1024 * 1024).open()
    return h5py.File(f, "r")


def _window(h5, grid, tx, ty):
    """(row0, col0) of the tile's top-left pixel in the file, or None when the tile is outside
    the file. The file's lattice is checked against the grid lattice (edges on the half-pixel)."""
    lat = h5["latitude"]
    lon = h5["longitude"]
    lat0, lon0 = float(lat[0]), float(lon[0])          # centres of the top-left pixel
    nrow, ncol = lat.shape[0], lon.shape[0]
    top, left = lat0 + RES / 2, lon0 - RES / 2
    w, s, e, n = grid.tile_bounds(tx, ty)
    r0 = (top - n) / RES
    c0 = (w - left) / RES
    if abs(r0 - round(r0)) > 1e-6 or abs(c0 - round(c0)) > 1e-6:
        raise ValueError("SYN V10 file lattice does not match the s3_syn_v10 grid")
    r0, c0 = int(round(r0)), int(round(c0))
    if r0 < 0 or c0 < 0 or r0 + TILE_PX > nrow or c0 + TILE_PX > ncol:
        return None
    return r0, c0


def plan_scene(item, bbox, bands=None, skip_keys=None, keep_offzone=False, aoi=None) -> dict:
    from ..aoi import tile_filter
    bands = bands or DEFAULT_BANDS
    skip_keys = skip_keys or set()
    grid, date = grid_for(), _date(item)
    in_aoi = tile_filter(aoi)
    out = {}
    for band, *_ in _bands_of(item, bands):
        n = len(common.plan_tiles(grid, bbox, CRS, band, date, skip_keys, True, in_aoi))
        if n:
            out[band] = n
    return out


def ingest_scene(item, bbox, writer, bands=None, skip_keys=None, log=print,
                 keep_offzone=False, aoi=None) -> int:
    from ..aoi import tile_filter
    bands = bands or DEFAULT_BANDS
    skip_keys = skip_keys or set()
    grid, date = grid_for(), _date(item)
    in_aoi = tile_filter(aoi)
    specs = _bands_of(item, bands)
    if not specs:
        return 0

    def fetch_band(spec):
        band, key, nodata, scale, offset = spec
        todo = common.plan_tiles(grid, bbox, CRS, band, date, skip_keys, True, in_aoi)
        if not todo:
            return []
        rows = []
        h5 = _open(item.assets[key].href)
        try:
            var = h5[VAR]
            fill = int(var.attrs["_FillValue"][0]) if "_FillValue" in var.attrs else nodata
            # sort by row so neighbouring tiles hit the same HDF5 chunks back to back
            for tx, ty in sorted(todo, key=lambda t: (-t[1], t[0])):
                win = _window(h5, grid, tx, ty)
                if win is None:
                    continue
                r0, c0 = win
                arr = np.asarray(var[r0:r0 + TILE_PX, c0:c0 + TILE_PX], dtype=np.uint8)
                if np.all(arr == fill):
                    continue
                cloud = round(float((arr == fill).sum()) / arr.size * 100.0, 2)
                blob = common.encode_geotiff(arr, grid, tx, ty, CRS, fill, scale, offset)
                rows.append((band, date, tx, ty, CRS, RES, TILE_PX,
                             common.wgs84_bounds(grid, tx, ty, CRS), fill, cloud, item.id,
                             blob, int((arr != fill).sum())))
        finally:
            h5.close()
        return rows

    written = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for rows in ex.map(fetch_band, specs):
            for row in rows:
                written += 1 if writer.add(*row) else 0
    log(f"    {written} tiles")
    return written
