"""ERA5-Land reanalysis, hourly, 0.1 degree, from the Copernicus Climate Data Store (CDS).

Needs a free CDS account (see api/README.md): the ONE non-anonymous source family in the vault,
kept because nothing anonymous offers current 2 m temperature, dewpoint, wind, radiation and soil
moisture at 0.1 degree (~11 x 9 km over Turkey; ERA5 in the `era5` adapter is 0.25). ERA5-Land is
land only: sea cells are NaN in the source and nodata here.

Access model, stated plainly as a deviation from "zero remote compute": the CDS API takes a
request (variables, day, hours, area), builds a NetCDF on ECMWF's side and hands back a file; no
byte-range reads. Small requests (one day, a handful of variables, one country) answered in
seconds during our tests, but the service is queued and can take minutes under load. One request
per DAY per ingest scene: all requested bands, 24 hours, the area = union of the tiles the AOI
touches (whole 10 degree tiles, so a stored tile is always complete). Turkey = 6 tiles ~ 30 x 20
degrees, about 60k cells x 24 h x N bands, a few tens of MB per day.

Grid: cell CENTRES on the 0.1 degree lines (25.6, 25.7, ...), so edges are offset by 0.05 and the
lattice anchor is (-0.05, -0.05). 100 px tiles = 10 degrees. Coordinates are matched by nearest
value with a 0.01 degree tolerance; cells the response does not contain are nodata.

Time: one tile per HOUR, `date` = 'YYYY-MM-DDTHH' like `era5`. Preliminary (ERA5T) vs final is
read from the response's `expver` coordinate (0005 = ERA5T, 0001 = final): a day with any ERA5T
hour is stored with scene_id `era5t`, else `era5land`. ERA5-Land final trails real time by two to
three months; a re-run re-fetches stored ERA5T days older than PRELIM_RECHECK_DAYS and the final
rows replace them (newest fetched_at wins at compaction).

Values are stored RAW in ERA5-Land units. Note the accumulation convention: `tp`, `ssrd`, `e`,
`pev`, `ro` and the other flux/accumulation fields are accumulated since 00 UTC of the same day,
so the value at hour H is the running total through H, and hour 00 of a day holds the previous
day's 24 h total. Hourly amounts are differences of consecutive hours; readers derive them.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date as _date, datetime, timedelta

import numpy as np

from ..grid import Grid
from . import common

DATASET = "reanalysis-era5-land"
CRS = "EPSG:4326"
RES = 0.1
TILE_PX = 100                 # 10 degrees
ANCHOR = -0.05                # cell centres on the 0.1 lines, edges 0.05 off
NODATA = -9999.0
SCENE_FINAL = "era5land"
SCENE_PRELIM = "era5t"
LATENCY_DAYS = 6              # ERA5T reaches CDS about 5 days behind real time
PRELIM_RECHECK_DAYS = 60      # stored ERA5T days older than this are re-fetched (final may exist)
TOL = 0.01

# short band code (ECMWF short name, also the NetCDF variable name) -> CDS request variable
BANDS = {
    "t2m":   "2m_temperature",
    "d2m":   "2m_dewpoint_temperature",
    "tp":    "total_precipitation",
    "u10":   "10m_u_component_of_wind",
    "v10":   "10m_v_component_of_wind",
    "ssrd":  "surface_solar_radiation_downwards",
    "strd":  "surface_thermal_radiation_downwards",
    "sp":    "surface_pressure",
    "skt":   "skin_temperature",
    "e":     "total_evaporation",
    "pev":   "potential_evaporation",
    "ro":    "runoff",
    "sd":    "snow_depth_water_equivalent",
    "sf":    "snowfall",
    "swvl1": "volumetric_soil_water_layer_1",
    "swvl2": "volumetric_soil_water_layer_2",
    "swvl3": "volumetric_soil_water_layer_3",
    "swvl4": "volumetric_soil_water_layer_4",
    "stl1":  "soil_temperature_level_1",
    "stl2":  "soil_temperature_level_2",
    "lai_hv": "leaf_area_index_high_vegetation",
    "lai_lv": "leaf_area_index_low_vegetation",
}
DEFAULT_BANDS = ["t2m", "d2m", "tp", "u10", "v10", "ssrd", "swvl1", "swvl2"]
HOURS = [f"{h:02d}:00" for h in range(24)]


class Item:
    """Minimal STAC-like item the CLI understands. One per day."""

    def __init__(self, day: _date):
        self.day = day
        self.id = f"era5land-{day.isoformat()}"
        self.properties = {"datetime": f"{day.isoformat()}T00:00:00Z"}


def grid_for() -> Grid:
    return Grid(CRS, RES, TILE_PX, ANCHOR, ANCHOR)


def client():
    import cdsapi
    from .. import credentials
    url, key = credentials.cds()
    return cdsapi.Client(url=url, key=key, quiet=True, progress=False)


def search(bbox, start, end, cloud_max=None, limit=None):
    """Days in [start, end] up to LATENCY_DAYS before today, oldest first. Whether a day is final
    or ERA5T is only known from the response, so items carry no kind."""
    d0, d1 = _date.fromisoformat(start), _date.fromisoformat(end)
    d1 = min(d1, _date.today() - timedelta(days=LATENCY_DAYS))
    items = []
    day = d0
    while day <= d1:
        items.append(Item(day))
        day += timedelta(days=1)
    return items[:limit] if limit else items


def _prelim_keys_in_store(dataset, day: _date) -> set:
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


def _plan(item, bbox, bands, skip_keys, aoi, dataset="era5land") -> dict:
    """band -> [(hour_date, tx, ty)] still to fetch. Stored ERA5T rows of a day old enough to have a
    final version are not skipped, so the final replaces them."""
    from ..aoi import tile_filter
    grid = grid_for()
    skip = set(skip_keys or ())
    if item.day <= _date.today() - timedelta(days=PRELIM_RECHECK_DAYS):
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
    """band -> number of (hour, tile) rows this day would still fetch (nothing is requested)."""
    plan = _plan(item, bbox, bands or DEFAULT_BANDS, skip_keys, aoi)
    return {b: len(v) for b, v in plan.items()}


def request_area(tiles) -> list:
    """CDS `area` [north, west, south, east] covering whole tiles, on cell CENTRES (CDS interprets
    the box as the centre range: asking 35.8..42.2 returned rows 35.8, 35.9, ..., 42.2)."""
    g = grid_for()
    bs = [g.tile_bounds(tx, ty) for tx, ty in tiles]
    w = min(b[0] for b in bs) + RES / 2
    s = min(b[1] for b in bs) + RES / 2
    e = max(b[2] for b in bs) - RES / 2
    n = max(b[3] for b in bs) - RES / 2
    return [round(n, 3), round(w, 3), round(s, 3), round(e, 3)]


def fetch_day(day: _date, bands, tiles, out_path):
    """One CDS request: the bands, all 24 hours, the area of the given tiles -> NetCDF file path."""
    req = {
        "variable": [BANDS[b] for b in bands],
        "year": f"{day.year}", "month": f"{day.month:02d}", "day": [f"{day.day:02d}"],
        "time": HOURS, "area": request_area(tiles),
        "data_format": "netcdf", "download_format": "unarchived",
    }
    client().retrieve(DATASET, req).download(str(out_path))
    return out_path


def tile_centres(tx: int, ty: int):
    """(lats descending, lons ascending) of the cell centres of one tile."""
    w, s, e, n = grid_for().tile_bounds(tx, ty)
    lats = np.round(n - RES / 2 - RES * np.arange(TILE_PX), 4)
    lons = np.round(w + RES / 2 + RES * np.arange(TILE_PX), 4)
    return lats, lons


def cut_tile(da, tx: int, ty: int) -> np.ndarray:
    """(TILE_PX, TILE_PX) float32 array of one tile from a 2-D (latitude, longitude) DataArray;
    cells absent from the response, and NaN (sea), become nodata."""
    lats, lons = tile_centres(tx, ty)
    sub = da.reindex(latitude=lats, longitude=lons, method="nearest", tolerance=TOL)
    a = sub.values.astype(np.float32)
    a[~np.isfinite(a)] = NODATA
    return a


def ingest_scene(item, bbox, writer, bands=None, skip_keys=None, log=print,
                 keep_offzone=False, aoi=None) -> int:
    """One day = one CDS request; every requested (band, hour, tile) becomes a store row."""
    import tempfile
    from pathlib import Path
    import xarray as xr
    bands = bands or DEFAULT_BANDS
    plan = _plan(item, bbox, bands, skip_keys, aoi, writer.dataset)
    if not plan:
        return 0
    tiles = sorted({(tx, ty) for rows in plan.values() for _, tx, ty in rows})
    grid = grid_for()
    with tempfile.TemporaryDirectory() as tmp:
        nc = fetch_day(item.day, list(plan), tiles, Path(tmp) / f"{item.id}.nc")
        ds = xr.open_dataset(nc)
        try:
            tdim = "valid_time" if "valid_time" in ds.dims else "time"
            expver = ds["expver"].values if "expver" in ds else None
            prelim = expver is not None and np.any(np.asarray(expver).astype(str) == "0005")
            scene = SCENE_PRELIM if prelim else SCENE_FINAL
            hours = [str(np.datetime_as_string(t, unit="h")) for t in ds[tdim].values]   # 'YYYY-MM-DDTHH'
            written = 0
            for band, rows in plan.items():
                if band not in ds:
                    continue
                want = {(hd, tx, ty) for hd, tx, ty in rows}
                for i, hd in enumerate(hours):
                    da = ds[band].isel({tdim: i})
                    for tx, ty in tiles:
                        if (hd, tx, ty) not in want:
                            continue
                        a = cut_tile(da, tx, ty)
                        if np.all(a == NODATA):
                            continue                       # open sea tile
                        blob = common.encode_geotiff(a, grid, tx, ty, CRS, NODATA)
                        if writer.add(band, hd, tx, ty, CRS, RES, TILE_PX,
                                      common.wgs84_bounds(grid, tx, ty, CRS), NODATA, None, scene,
                                      blob, int((a != NODATA).sum())):
                            written += 1
        finally:
            ds.close()
    log(f"    {written} tiles ({scene}, {len(plan)} bands x {len(hours)} h)")
    return written
