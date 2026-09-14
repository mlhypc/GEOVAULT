"""ERA5-Land DAILY statistics, 0.1 degree, from the Copernicus Climate Data Store.

The CDS "derived-era5-land-daily-statistics" service reduces the hourly ERA5-Land fields to one
value per day (mean, minimum or maximum) in a chosen time zone, on ECMWF's side. Same grid,
same account and licence family as `era5land` (see api/README.md); a separate dataset licence
must be accepted once on the CDS website.

Why keep it next to `era5land`: most agronomic use wants daily Tmin/Tmax/Tmean, daily rain and
daily radiation, and pulling 24 hourly fields to compute them locally costs 24x the transfer. This
adapter stores the daily numbers as published, one tile per DAY (`date` = 'YYYY-MM-DD'), like
AgERA5; the hourly `era5land` store stays the raw record. The reduction day follows TIME_ZONE
(default UTC+03:00, Turkey), so a "day" here is a local calendar day, not a UTC day.

Bands are `<variable>_<statistic>`, e.g. `t2m_max`, `d2m_mean`, `swvl1_mean`. Each band is one
CDS request per day (one variable, one statistic: the service returns one NetCDF per request);
requests of one day run concurrently. Units are the raw ERA5-Land units (K, m s-1, Pa, m3 m-3),
nodata -9999 (sea too).

LIMIT (from the service, verified 2026-09-14): daily statistics are NOT offered for the
ACCUMULATED fields (total precipitation, radiation, evaporation, runoff, snowfall); the request
fails with "Daily statistics of accumulated variables are not supported". Those bands are
rejected here. Daily rain and radiation come from `agera5` (`tp`, `ssr`) or from differencing the
hourly `era5land` store.

Grid as `era5land`: cell centres on the 0.1 lines, anchor (-0.05, -0.05), 100 px tiles of 10
degrees, response cells matched by nearest coordinate. Latency about 5 days (ERA5T); the
preliminary/final flag is not exposed by this service, scene_id is `era5land-daily`.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date as _date, timedelta

import numpy as np

from ..grid import Grid
from . import common
from .era5land import (BANDS as HOURLY_BANDS, CRS, NODATA, RES, TILE_PX, client, cut_tile,
                       grid_for as _grid, request_area)

DATASET = "derived-era5-land-daily-statistics"
SCENE = "era5land-daily"
LATENCY_DAYS = 6
TIME_ZONE = "utc+03:00"
FREQUENCY = "1_hourly"
STATS = {"mean": "daily_mean", "min": "daily_minimum", "max": "daily_maximum"}
# accumulated ERA5-Land fields: the daily-statistics service refuses them (see module doc)
ACCUMULATED = {"tp", "ssrd", "strd", "e", "pev", "ro", "sf"}

DEFAULT_BANDS = ["t2m_mean", "t2m_min", "t2m_max", "d2m_mean", "u10_mean", "v10_mean",
                 "swvl1_mean", "swvl2_mean", "skt_max", "skt_min"]


def split_band(band: str):
    """'t2m_max' -> ('t2m', 'max'); None when the band is not a known variable/statistic pair."""
    var, _, stat = band.rpartition("_")
    if var in HOURLY_BANDS and var not in ACCUMULATED and stat in STATS:
        return var, stat
    return None


class Item:
    def __init__(self, day: _date):
        self.day = day
        self.id = f"era5land-daily-{day.isoformat()}"
        self.properties = {"datetime": f"{day.isoformat()}T00:00:00Z"}


def grid_for() -> Grid:
    return _grid()


def search(bbox, start, end, cloud_max=None, limit=None):
    d0, d1 = _date.fromisoformat(start), _date.fromisoformat(end)
    d1 = min(d1, _date.today() - timedelta(days=LATENCY_DAYS))
    items = []
    day = d0
    while day <= d1:
        items.append(Item(day))
        day += timedelta(days=1)
    return items[:limit] if limit else items


def _plan(item, bbox, bands, skip_keys, aoi) -> dict:
    from ..aoi import tile_filter
    grid = grid_for()
    skip = set(skip_keys or ())
    in_aoi = tile_filter(aoi)
    date = item.day.isoformat()
    out = {}
    for band in bands:
        if split_band(band) is None:
            continue
        todo = common.plan_tiles(grid, bbox, CRS, band, date, skip, True, in_aoi)
        if todo:
            out[band] = todo
    return out


def plan_scene(item, bbox, bands=None, skip_keys=None, keep_offzone=False, aoi=None) -> dict:
    return {b: len(v) for b, v in _plan(item, bbox, bands or DEFAULT_BANDS, skip_keys, aoi).items()}


def fetch_band(day: _date, band: str, tiles, out_path):
    """One CDS request: one variable, one daily statistic, the area of the tiles -> NetCDF path."""
    var, stat = split_band(band)
    req = {
        "variable": [HOURLY_BANDS[var]],
        "year": f"{day.year}", "month": f"{day.month:02d}", "day": [f"{day.day:02d}"],
        "daily_statistic": STATS[stat], "time_zone": TIME_ZONE, "frequency": FREQUENCY,
        "area": request_area(tiles),
    }
    client().retrieve(DATASET, req).download(str(out_path))
    return out_path


def ingest_scene(item, bbox, writer, bands=None, skip_keys=None, log=print,
                 keep_offzone=False, aoi=None) -> int:
    """One day: one request per band (concurrently), every (band, tile) becomes a store row."""
    import tempfile
    from pathlib import Path
    import xarray as xr
    bands = bands or DEFAULT_BANDS
    plan = _plan(item, bbox, bands, skip_keys, aoi)
    if not plan:
        return 0
    grid = grid_for()
    date = item.day.isoformat()

    def one(band):
        rows = []
        with tempfile.TemporaryDirectory() as tmp:
            nc = fetch_band(item.day, band, plan[band], Path(tmp) / f"{band}.nc")
            ds = xr.open_dataset(nc)
            try:
                var = split_band(band)[0]
                name = var if var in ds else [v for v in ds.data_vars][0]
                tdim = "valid_time" if "valid_time" in ds.dims else "time"
                da = ds[name].isel({tdim: 0}) if tdim in ds[name].dims else ds[name]
                for tx, ty in plan[band]:
                    a = cut_tile(da, tx, ty)
                    if np.all(a == NODATA):
                        continue
                    blob = common.encode_geotiff(a, grid, tx, ty, CRS, NODATA)
                    rows.append((band, date, tx, ty, CRS, RES, TILE_PX,
                                 common.wgs84_bounds(grid, tx, ty, CRS), NODATA, None, SCENE,
                                 blob, int((a != NODATA).sum())))
            finally:
                ds.close()
        return rows

    written = 0
    with ThreadPoolExecutor(max_workers=min(4, len(plan))) as ex:
        for rows in ex.map(one, list(plan)):
            for row in rows:
                written += 1 if writer.add(*row) else 0
    log(f"    {written} tiles ({len(plan)} bands, {len(plan)} requests)")
    return written
