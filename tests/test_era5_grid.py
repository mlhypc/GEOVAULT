"""Offline checks for the ERA5 adapter's lattice and index math (no network)."""

import numpy as np

from geovault.sources import era5


def test_turkey_is_one_tile():
    g = era5.grid_for()
    tiles = list(g.tiles_for_bounds(25.6, 35.8, 44.9, 42.2))
    assert tiles == [(1, 1)]
    assert g.tile_bounds(1, 1) == (24.875, 24.875, 49.875, 49.875)


def test_index_window_matches_store_axes():
    rows, cols, valid = era5.tile_index_window(1, 1)
    # northernmost centre 49.75 -> row (90-49.75)/0.25 = 161; southernmost 25.0 -> 260
    assert rows[0] == 161 and rows[-1] == 260
    # westernmost centre 25.0 -> col 100; easternmost 49.75 -> col 199
    assert cols[0] == 100 and cols[-1] == 199
    assert valid.all()


def test_index_window_wraps_and_clips():
    rows, cols, valid = era5.tile_index_window(-1, 2)      # lon -25.125..-0.125, lat 49.875..74.875
    assert cols[0] == 1340 and cols[-1] == 1439             # wrapped onto the 0..360 axis
    assert valid.all()
    rows, cols, valid = era5.tile_index_window(0, 3)        # lat 74.875..99.875: beyond the pole
    assert (~valid).sum() == 39 and valid.sum() == 61   # centres 99.75..75.0: 39 rows above the pole


def test_hour_index():
    from datetime import datetime
    assert era5.hour_index(datetime(1900, 1, 1, 0)) == 0
    assert era5.hour_index(datetime(1900, 1, 2, 0)) == 24


def test_read_tile_from_fake_array():
    arr = np.arange(721 * 1440, dtype=np.float32).reshape(1, 721, 1440)
    a = era5.read_tile(arr, 0, 1, 1)
    assert a.shape == (100, 100)
    assert a[0, 0] == 161 * 1440 + 100
    assert a[-1, -1] == 260 * 1440 + 199
