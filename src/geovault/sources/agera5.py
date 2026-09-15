"""AgERA5 daily agrometeorological indicators, 0.1 degree, from the Copernicus Climate Data Store.

Needs a free CDS account (see api/README.md), like `era5land`. AgERA5 (C3S, Wageningen) is ERA5
regridded to 0.1 degree with elevation correction and reduced to the DAILY statistics agronomy
uses: mean / max / min air temperature (24 h and day-time / night-time), relative humidity at
fixed local hours (plus a derived daily max/min), dewpoint, vapour pressure (plus the deficit at
max temperature), wind speed, solar radiation, precipitation (plus its liquid/solid/total duration
fraction), reference evapotranspiration (FAO-56 Penman-Monteith), snow thickness (plus its liquid
water equivalent). It is a derived product (like CHIRPS), stored as published: one tile per DAY,
`date` = 'YYYY-MM-DD'. Latency about a week. Version 2.0. `BANDS` covers all 16 CDS variables (v2.0
values checked live against the sis-agrometeorological-indicators constraints, 2026-09-15);
`DEFAULT_BANDS` is a smaller day-to-day subset, the rest reachable with `--bands`.

Request model: one CDS request per (calendar month, variable) covering every day of that month
still missing from the store, with all wanted statistics; the answer is a zip with one NetCDF per
(statistic, day), whose file name carries the product id and an 8-digit date
(e.g. Temperature-Air-2m_Max-Day-Time_C3S-glob-agric_AgERA5_20260601_final-v2.0.0...). Members are
matched to bands by that id and to days by the date in the name. Requests for one item (month) run
concurrently over variables.

CDS charges a large fixed latency per request almost regardless of size (tens of seconds even for
one day), so the lever is fewer, bigger requests: a 10-day batch measured ~13x faster wall-clock
than 10 separate one-day requests. Batching to a full calendar month (matching the store's own
month partitioning) was the natural size, not a hard CDS limit.

Grid: same as ERA5-Land, cell centres on the 0.1 lines, anchor (-0.05, -0.05), 100 px tiles of
10 degrees. NOTE the CDS `area` subsetter for this dataset drops the northern and western edge
rows (asking 42.2 north returned 42.1 as the first row), so the request area is padded by one
cell on those sides; cells absent from the response are nodata (also sea: AgERA5 is land only).

Units are the published ones: K for temperatures, %, hPa, m s-1, J m-2 day-1, mm day-1 (also ET0),
cm (snow thickness LWE), dimensionless 0-1 (the duration fractions).
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date as _date, timedelta

import numpy as np

from ..grid import Grid
from . import common
from .era5land import ANCHOR, CRS, NODATA, RES, TILE_PX, TOL, client, grid_for as _grid, tile_centres

DATASET = "sis-agrometeorological-indicators"
VERSION = "2_0"
SCENE = "agera5-v2"
LATENCY_DAYS = 8

# band -> (cds variable, request key, request value, product-id prefix in the zip member name)
BANDS = {
    "t2m_mean":  ("2m_temperature", "statistic", "24_hour_mean",      "Temperature-Air-2m_Mean-24h"),
    "t2m_max":   ("2m_temperature", "statistic", "24_hour_maximum",   "Temperature-Air-2m_Max-24h"),
    "t2m_min":   ("2m_temperature", "statistic", "24_hour_minimum",   "Temperature-Air-2m_Min-24h"),
    "t2m_dmax":  ("2m_temperature", "statistic", "day_time_maximum",  "Temperature-Air-2m_Max-Day-Time"),
    "t2m_dmean": ("2m_temperature", "statistic", "day_time_mean",     "Temperature-Air-2m_Mean-Day-Time"),
    "t2m_nmin":  ("2m_temperature", "statistic", "night_time_minimum", "Temperature-Air-2m_Min-Night-Time"),
    "t2m_nmean": ("2m_temperature", "statistic", "night_time_mean",   "Temperature-Air-2m_Mean-Night-Time"),
    "rh_06":     ("2m_relative_humidity", "time", "06_00", "Relative-Humidity-2m_06h"),
    "rh_09":     ("2m_relative_humidity", "time", "09_00", "Relative-Humidity-2m_09h"),
    "rh_12":     ("2m_relative_humidity", "time", "12_00", "Relative-Humidity-2m_12h"),
    "rh_15":     ("2m_relative_humidity", "time", "15_00", "Relative-Humidity-2m_15h"),
    "rh_18":     ("2m_relative_humidity", "time", "18_00", "Relative-Humidity-2m_18h"),
    "d2m_mean":  ("2m_dewpoint_temperature", "statistic", "24_hour_mean", "Dew-Point-Temperature-2m_Mean"),
    "vp_mean":   ("vapour_pressure", "statistic", "24_hour_mean", "Vapour-Pressure_Mean"),
    "ws10_mean": ("10m_wind_speed", "statistic", "24_hour_mean", "Wind-Speed-10m_Mean"),
    "tcc_mean":  ("cloud_cover", "statistic", "24_hour_mean", "Cloud-Cover_Mean"),
    "tp":        ("precipitation_flux", None, None, "Precipitation-Flux"),
    "ssr":       ("solar_radiation_flux", None, None, "Solar-Radiation-Flux"),
    "sd":        ("snow_thickness", "statistic", "24_hour_mean", "Snow-Thickness_Mean"),
    "et0":       ("reference_evapotranspiration", None, None, "ReferenceET-PenmanMonteith-FAO56"),
    "vpd_tmax":  ("vapour_pressure_deficit_at_maximum_temperature", None, None,
                  "Vapour-Pressure-Deficit-at-Maximum-Temperature"),
    "swe_mean":  ("snow_thickness_lwe", "statistic", "24_hour_mean", "Snow-Thickness-LWE_Mean-24h"),
    "rain_frac": ("liquid_precipitation_duration_fraction", None, None,
                  "Precipitation-Rain-Duration-Fraction"),
    "snow_frac": ("solid_precipitation_duration_fraction", None, None,
                  "Precipitation-Solid-Duration-Fraction"),
    "precip_frac": ("precipitation_duration_fraction", None, None, "Precipitation-Duration-Fraction"),
    "rhd_max":   ("2m_relative_humidity_derived", "statistic", "24_hour_maximum",
                  "Derived-Relative-Humidity-2m_Max-24h"),
    "rhd_min":   ("2m_relative_humidity_derived", "statistic", "24_hour_minimum",
                  "Derived-Relative-Humidity-2m_Min-24h"),
}
DEFAULT_BANDS = ["t2m_mean", "t2m_max", "t2m_min", "rh_06", "rh_12", "rh_18", "d2m_mean",
                 "ws10_mean", "ssr", "tp"]


class Item:
    """One calendar month: batches every day of it into a single CDS request per variable."""

    def __init__(self, year: int, month: int, days: list):
        self.year = year
        self.month = month
        self.days = days                       # ascending list of date, all in (year, month)
        self.id = f"agera5-{year}-{month:02d}"
        self.properties = {"datetime": f"{days[0].isoformat()}T00:00:00Z"}


def grid_for() -> Grid:
    return _grid()


def search(bbox, start, end, cloud_max=None, limit=None):
    """One item per calendar month touched by [start, end] (clamped to LATENCY_DAYS before
    today), oldest first; `limit` caps the number of MONTHS, not days."""
    d0, d1 = _date.fromisoformat(start), _date.fromisoformat(end)
    d1 = min(d1, _date.today() - timedelta(days=LATENCY_DAYS))
    by_month = {}
    day = d0
    while day <= d1:
        by_month.setdefault((day.year, day.month), []).append(day)
        day += timedelta(days=1)
    items = [Item(y, m, days) for (y, m), days in sorted(by_month.items())]
    return items[:limit] if limit else items


def _plan(item, bbox, bands, skip_keys, aoi) -> dict:
    """band -> [(date, tx, ty)] still to fetch, across every day of the item's month."""
    from ..aoi import tile_filter
    grid = grid_for()
    skip = set(skip_keys or ())
    in_aoi = tile_filter(aoi)
    out = {}
    for band in bands:
        if band not in BANDS:
            continue
        todo = []
        for day in item.days:
            date = day.isoformat()
            for tx, ty in common.plan_tiles(grid, bbox, CRS, band, date, skip, True, in_aoi):
                todo.append((date, tx, ty))
        if todo:
            out[band] = todo
    return out


def plan_scene(item, bbox, bands=None, skip_keys=None, keep_offzone=False, aoi=None) -> dict:
    return {b: len(v) for b, v in _plan(item, bbox, bands or DEFAULT_BANDS, skip_keys, aoi).items()}


def request_area(tiles) -> list:
    """[north, west, south, east] on cell centres, padded one cell north and west (see module doc)."""
    g = grid_for()
    bs = [g.tile_bounds(tx, ty) for tx, ty in tiles]
    w = min(b[0] for b in bs) + RES / 2 - RES
    s = min(b[1] for b in bs) + RES / 2
    e = max(b[2] for b in bs) - RES / 2
    n = max(b[3] for b in bs) - RES / 2 + RES
    return [round(n, 3), round(w, 3), round(s, 3), round(e, 3)]


def fetch_variable(year: int, month: int, days: list, variable: str, bands, tiles, out_path):
    """One CDS request: one AgERA5 variable, every given day of one month, the statistics/times
    of the given bands -> zip path (one NetCDF member per (statistic, day))."""
    req = {"variable": variable, "version": VERSION,
           "year": [f"{year}"], "month": [f"{month:02d}"],
           "day": [f"{d.day:02d}" for d in days],
           "area": request_area(tiles)}
    for b in bands:
        _, key, val, _ = BANDS[b]
        if key:
            req.setdefault(key, []).append(val)
    client().retrieve(DATASET, req).download(str(out_path))
    return out_path


def cut_tile(da, tx: int, ty: int) -> np.ndarray:
    lats, lons = tile_centres(tx, ty)
    sub = da.reindex(lat=lats, lon=lons, method="nearest", tolerance=TOL)
    a = sub.values.astype(np.float32)
    a[~np.isfinite(a)] = NODATA
    return a


def ingest_scene(item, bbox, writer, bands=None, skip_keys=None, log=print,
                 keep_offzone=False, aoi=None) -> int:
    """One month: one request per variable (concurrently) covering every day still missing that
    month; every (band, date, tile) becomes a store row."""
    import re
    import tempfile
    import zipfile
    from pathlib import Path
    import xarray as xr
    bands = bands or DEFAULT_BANDS
    plan = _plan(item, bbox, bands, skip_keys, aoi)
    if not plan:
        return 0
    grid = grid_for()
    by_var = {}
    for b in plan:
        by_var.setdefault(BANDS[b][0], []).append(b)

    def one(variable):
        vbands = by_var[variable]
        band_date_tiles = {b: {} for b in vbands}
        for b in vbands:
            for date, tx, ty in plan[b]:
                band_date_tiles[b].setdefault(date, set()).add((tx, ty))
        days_needed = sorted({_date.fromisoformat(d) for dts in band_date_tiles.values() for d in dts})
        all_tiles = sorted({t for dts in band_date_tiles.values() for s in dts.values() for t in s})
        rows = []
        with tempfile.TemporaryDirectory() as tmp:
            z = fetch_variable(item.year, item.month, days_needed, variable, vbands, all_tiles,
                               Path(tmp) / f"{variable}.zip")
            with zipfile.ZipFile(z) as zf:
                members = zf.namelist()
                for b in vbands:
                    prefix = BANDS[b][3]
                    hits = [m for m in members if Path(m).name.startswith(prefix + "_")]
                    for m in hits:
                        dm = re.search(r"(\d{8})", Path(m).name)
                        if not dm:
                            continue
                        date = f"{dm.group(1)[:4]}-{dm.group(1)[4:6]}-{dm.group(1)[6:]}"
                        tiles = band_date_tiles[b].get(date)
                        if not tiles:
                            continue
                        zf.extract(m, tmp)
                        ds = xr.open_dataset(Path(tmp) / m)
                        try:
                            var = [v for v in ds.data_vars if v != "crs"][0]
                            da = ds[var]
                            if "time" in da.dims:
                                da = da.isel(time=0)
                            for tx, ty in tiles:
                                a = cut_tile(da, tx, ty)
                                if np.all(a == NODATA):
                                    continue
                                blob = common.encode_geotiff(a, grid, tx, ty, CRS, NODATA)
                                rows.append((b, date, tx, ty, CRS, RES, TILE_PX,
                                             common.wgs84_bounds(grid, tx, ty, CRS), NODATA, None,
                                             SCENE, blob, int((a != NODATA).sum())))
                        finally:
                            ds.close()
        return rows

    written = 0
    with ThreadPoolExecutor(max_workers=min(4, len(by_var))) as ex:
        for rows in ex.map(one, list(by_var)):
            for row in rows:
                written += 1 if writer.add(*row) else 0
    log(f"    {written} tiles ({len(plan)} bands, {len(by_var)} requests, {len(item.days)} days)")
    return written
