"""ERA5 reanalysis, hourly, 0.25 degree, from Google's ARCO-ERA5 public bucket. Anonymous.

Store: gs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3 (Zarr, blosc/lz4,
float32, 262 single-level variables plus 37 pressure levels; only single-level variables are
exposed here). Updated monthly: the final ERA5 reaches `valid_time_stop` (about 3 months back),
the preliminary ERA5T reaches `valid_time_stop_era5t` (about a week back). Both are read from the
store's own attributes at search time, nothing is hard-coded.

ERA5 is a model reanalysis, not a satellite product: the only anonymous, current, gridded source
of 2 m air temperature, dewpoint, wind and radiation we found (ERA5-Land 0.1 degree exists only
behind Copernicus / DestinE accounts). It is the coarsest layer in the vault: one 0.25 degree cell
is roughly 28 x 22 km over Turkey.

Deviation from the vault's rules, stated plainly: the source is Zarr, not COG, so this adapter
needs `zarr` and `gcsfs` (optional extra `era5`), and the store is chunked (1 hour, whole globe),
so reading Turkey costs the global chunk anyway: about 3 MB per hour per variable, ~350 MB per
day for the six default bands, while the stored tiles are a few hundred KB. Bandwidth, not disk.

Grid: cell CENTRES sit on multiples of 0.25 (lon 0..359.75 east, lat 90..-90 south), so cell
edges are offset by 0.125 and the lattice anchor is (-0.125, -0.125). 100 px tiles = 25 degrees;
Turkey falls in one tile (lon 24.875..49.875, lat 24.875..49.875). Longitudes are wrapped to the
0..360 axis of the store when reading; tiles are keyed on -180..180 like everything else.

Time: one tile per HOUR, `date` = 'YYYY-MM-DDTHH' (the store's month partition uses the first 7
characters, catalog prefix queries 'YYYY-MM-DD' return the 24 hours). Daily statistics are a
read-time derivation, never stored. A day whose hours come from ERA5T is stored with
scene_id `era5t` and replaced by the final ERA5 the day it becomes available (re-run the same
ingest); final rows carry scene_id `era5`.

Values are stored RAW in ERA5 units (K, m of water, J m-2 accumulated over the hour, m s-1, Pa,
m3 m-3). Nothing is rescaled. nodata -9999 marks cells the store has no value for.
"""

import math
from concurrent.futures import ThreadPoolExecutor
from datetime import date as _date, datetime, timedelta

import numpy as np

from ..grid import Grid
from . import common

STORE_URL = "gs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3"
CRS = "EPSG:4326"
RES = 0.25
TILE_PX = 100                 # 25 degrees
ANCHOR = -0.125               # cell edges are 0.125 off the degree lines (centres on the lines)
NODATA = -9999.0
NROW, NCOL = 721, 1440
EPOCH = datetime(1900, 1, 1)  # store time axis: hours since 1900-01-01
SCENE_FINAL = "era5"
SCENE_PRELIM = "era5t"
WORKERS = 6                   # bands read concurrently (each thread opens its own store handle)

# short band code -> ARCO variable name (ERA5 single levels)
BANDS = {
    "t2m":   "2m_temperature",
    "d2m":   "2m_dewpoint_temperature",
    "tp":    "total_precipitation",
    "u10":   "10m_u_component_of_wind",
    "v10":   "10m_v_component_of_wind",
    "ssrd":  "surface_solar_radiation_downwards",
    "sp":    "surface_pressure",
    "msl":   "mean_sea_level_pressure",
    "tcc":   "total_cloud_cover",
    "skt":   "skin_temperature",
    "e":     "evaporation",
    "pev":   "potential_evaporation",
    "sd":    "snow_depth",
    "i10fg": "instantaneous_10m_wind_gust",
    "swvl1": "volumetric_soil_water_layer_1",
    "swvl2": "volumetric_soil_water_layer_2",
    "swvl3": "volumetric_soil_water_layer_3",
    "swvl4": "volumetric_soil_water_layer_4",
    "stl1":  "soil_temperature_level_1",
    "mx2t":  "maximum_2m_temperature_since_previous_post_processing",
    "mn2t":  "minimum_2m_temperature_since_previous_post_processing",
}
DEFAULT_BANDS = ["t2m", "d2m", "tp", "u10", "v10", "ssrd"]


class Item:
    """Minimal STAC-like item the CLI understands: .id and .properties['datetime']. One per day."""

    def __init__(self, day: _date, kind: str):
        self.day = day
        self.kind = kind                                   # 'final' | 'prelim'
        self.id = f"era5-{kind}-{day.isoformat()}"
        self.properties = {"datetime": f"{day.isoformat()}T00:00:00Z"}

    @property
    def scene_id(self):
        return SCENE_FINAL if self.kind == "final" else SCENE_PRELIM


def grid_for() -> Grid:
    return Grid(CRS, RES, TILE_PX, ANCHOR, ANCHOR)


def open_store():
    """Read-only handle on the consolidated Zarr group (anonymous). One per thread: the fsspec
    layer underneath is not safe to share across threads."""
    import zarr
    return zarr.open_group(STORE_URL, mode="r", storage_options={"token": "anon"}, use_consolidated=True)


def store_bounds(group=None) -> tuple:
    """(final_stop, era5t_stop) as dates, from the store's attributes."""
    g = group or open_store()
    a = dict(g.attrs)
    return _date.fromisoformat(a["valid_time_stop"]), _date.fromisoformat(a["valid_time_stop_era5t"])


def hour_index(t: datetime) -> int:
    return int((t - EPOCH).total_seconds() // 3600)


def tile_index_window(tx: int, ty: int):
    """Store row/col indices of one tile: rows are a plain slice (lat 90 -> -90), columns an index
    array wrapped onto the 0..360 longitude axis. Rows outside the pole are clipped and reported
    as (row0_valid, nrows_valid) so the caller pads with nodata."""
    g = grid_for()
    w, s, e, n = g.tile_bounds(tx, ty)
    r0 = int(round((90.0 - n + RES / 2) / RES))         # row of the northernmost cell centre
    rows = np.arange(r0, r0 + TILE_PX)
    c0 = int(round((w + RES / 2) / RES))                # col of the westernmost centre, -180..180 axis
    cols = np.mod(np.arange(c0, c0 + TILE_PX), NCOL)    # wrap onto 0..360
    valid = (rows >= 0) & (rows < NROW)
    return rows, cols, valid


def search(bbox, start, end, cloud_max=None, limit=None):
    """Days in [start, end] the store holds (up to the ERA5T stop), oldest first. Days after the final
    stop are ERA5T (preliminary). bbox and cloud_max are accepted for CLI symmetry and ignored."""
    d0, d1 = _date.fromisoformat(start), _date.fromisoformat(end)
    final_stop, prelim_stop = store_bounds()
    d1 = min(d1, prelim_stop)
    items = []
    day = d0
    while day <= d1:
        items.append(Item(day, "final" if day <= final_stop else "prelim"))
        day += timedelta(days=1)
    return items[:limit] if limit else items


def _prelim_keys_in_store(dataset, day: _date) -> set:
    """(band, date, x, y) of this day's hours stored from ERA5T; a final item must re-fetch those."""
    from .. import catalog
    import duckdb
    p = catalog.path(dataset)
    if not p.exists():
        return set()
    con = duckdb.connect()
    rows = con.execute(
        f"SELECT band, date, x, y FROM read_parquet('{p.as_posix()}') "
        f"WHERE date LIKE ? AND scene_id = ?", [day.isoformat() + "%", SCENE_PRELIM]).fetchall()
    con.close()
    return set(rows)


def _hours(day: _date):
    return [f"{day.isoformat()}T{h:02d}" for h in range(24)]


def _plan(item, bbox, bands, skip_keys, aoi, dataset="era5") -> dict:
    """band -> [(hour_date, tx, ty)] still to fetch."""
    from ..aoi import tile_filter
    grid = grid_for()
    skip = set(skip_keys or ())
    if item.kind == "final":
        skip -= _prelim_keys_in_store(dataset, item.day)
    in_aoi = tile_filter(aoi)
    out = {}
    for band in bands:
        if band not in BANDS:
            continue
        todo = []
        for hd in _hours(item.day):
            # keep_offzone=True: one global lat/lon CRS, the UTM zone rule does not apply
            for tx, ty in common.plan_tiles(grid, bbox, CRS, band, hd, skip, True, in_aoi):
                todo.append((hd, tx, ty))
        if todo:
            out[band] = todo
    return out


def plan_scene(item, bbox, bands=None, skip_keys=None, keep_offzone=False, aoi=None) -> dict:
    """band -> number of (hour, tile) rows this day would still fetch (nothing is read)."""
    plan = _plan(item, bbox, bands or DEFAULT_BANDS, skip_keys, aoi)
    return {b: len(v) for b, v in plan.items()}


def read_tile(arr, hidx: int, tx: int, ty: int) -> np.ndarray:
    """One (hour, tile) as a (TILE_PX, TILE_PX) float32 array in store units, nodata where the
    store has no cell (beyond the poles) or NaN."""
    rows, cols, valid = tile_index_window(tx, ty)
    out = np.full((TILE_PX, TILE_PX), NODATA, np.float32)
    if not valid.any():
        return out
    r = rows[valid]
    block = arr[hidx, r.min():r.max() + 1, :]            # one global chunk read; slice columns locally
    sub = block[:, cols].astype(np.float32)
    sub[~np.isfinite(sub)] = NODATA
    out[valid, :] = sub
    return out


def ingest_scene(item, bbox, writer, bands=None, skip_keys=None, log=print,
                 keep_offzone=False, aoi=None) -> int:
    """One day: for each band read the 24 hourly global chunks and store the tiles over bbox/aoi."""
    bands = bands or DEFAULT_BANDS
    plan = _plan(item, bbox, bands, skip_keys, aoi, writer.dataset)
    if not plan:
        return 0
    grid = grid_for()

    def fetch_band(band):
        g = open_store()
        arr = g[BANDS[band]]
        rows = []
        by_hour = {}
        for hd, tx, ty in plan[band]:
            by_hour.setdefault(hd, []).append((tx, ty))
        for hd, tiles in by_hour.items():
            hidx = hour_index(datetime.fromisoformat(hd))
            for tx, ty in tiles:
                a = read_tile(arr, hidx, tx, ty)
                if np.all(a == NODATA):
                    continue
                blob = common.encode_geotiff(a, grid, tx, ty, CRS, NODATA)
                rows.append((band, hd, tx, ty, CRS, RES, TILE_PX,
                             common.wgs84_bounds(grid, tx, ty, CRS), NODATA, None, item.scene_id,
                             blob, int((a != NODATA).sum())))
        return rows

    written = 0
    with ThreadPoolExecutor(max_workers=min(WORKERS, len(plan))) as ex:
        for rows in ex.map(fetch_band, list(plan)):
            for row in rows:
                written += 1 if writer.add(*row) else 0
    log(f"    {written} tiles ({item.kind}, {len(plan)} bands x 24 h)")
    return written
