"""Offline checks for the CDS adapters (ERA5-Land, AgERA5): lattice, request area, tile cutting."""

import numpy as np
import xarray as xr

from geovault.sources import agera5, era5land, era5land_daily

TR = (25.6, 35.8, 44.9, 42.2)


def test_era5land_turkey_tiles_and_area():
    g = era5land.grid_for()
    tiles = list(g.tiles_for_bounds(*TR))
    assert sorted(tiles) == [(2, 3), (2, 4), (3, 3), (3, 4), (4, 3), (4, 4)]
    assert g.tile_bounds(3, 3) == (29.95, 29.95, 39.95, 39.95)
    # area on cell centres: whole tiles 20..50 E, 30..50 N
    assert era5land.request_area(tiles) == [49.9, 20.0, 30.0, 49.9]


def test_agera5_area_is_padded_north_west():
    tiles = list(agera5.grid_for().tiles_for_bounds(*TR))
    assert agera5.request_area(tiles) == [50.0, 19.9, 30.0, 49.9]


def test_tile_centres():
    lats, lons = era5land.tile_centres(3, 3)
    assert lats[0] == 39.9 and lats[-1] == 30.0 and len(lats) == 100
    assert lons[0] == 30.0 and lons[-1] == 39.9


def test_cut_tile_matches_nearest_and_pads():
    # a response covering only part of tile (3,3): lat 39.9..35.0, lon 30.0..34.9
    lat = np.round(39.9 - 0.1 * np.arange(50), 4)
    lon = np.round(30.0 + 0.1 * np.arange(50), 4)
    vals = np.arange(2500, dtype=np.float32).reshape(50, 50)
    da = xr.DataArray(vals, coords={"latitude": lat, "longitude": lon}, dims=("latitude", "longitude"))
    a = era5land.cut_tile(da, 3, 3)
    assert a.shape == (100, 100)
    assert a[0, 0] == 0 and a[49, 49] == 2499
    assert a[50, 0] == era5land.NODATA and a[0, 50] == era5land.NODATA


def test_agera5_request_shape():
    b = ["t2m_mean", "t2m_dmax"]
    by = {agera5.BANDS[x][0] for x in b}
    assert by == {"2m_temperature"}
    assert agera5.BANDS["rh_12"][1] == "time" and agera5.BANDS["tp"][1] is None


def test_era5land_daily_band_split():
    assert era5land_daily.split_band("t2m_max") == ("t2m", "max")
    assert era5land_daily.split_band("swvl1_mean") == ("swvl1", "mean")
    assert era5land_daily.split_band("lai_hv_mean") == ("lai_hv", "mean")
    assert era5land_daily.split_band("t2m_median") is None
    assert era5land_daily.split_band("tp_sum") is None       # accumulated: refused by the service
    assert era5land_daily.split_band("nope_max") is None
