"""Offline unit tests for the grid and AOI math (no network, no data dir)."""

from shapely.geometry import Polygon

from geovault.aoi import tile_filter
from geovault.grid import Grid
from geovault.sources.common import canonical_epsg


def test_grid_roundtrip():
    g = Grid("EPSG:32636", 10, 256)
    tx, ty = g.tile_of_point(527531.0, 4063217.0)
    w, s, e, n = g.tile_bounds(tx, ty)
    assert w <= 527531.0 < e
    assert s <= 4063217.0 < n
    assert (e - w) == 2560.0


def test_grid_deterministic():
    g = Grid("EPSG:32636", 10, 256)
    assert g.tile_of_point(0.0, 0.0) == (0, 0)
    assert g.tile_of_point(-0.1, -0.1) == (-1, -1)
    assert g.tile_of_point(2560.0, 2560.0) == (1, 1)


def test_tiles_for_bounds_covers_rect():
    g = Grid("EPSG:32636", 20, 256)
    tiles = list(g.tiles_for_bounds(0.0, 0.0, 10240.0, 5120.0))
    assert (0, 0) in tiles and (1, 0) in tiles
    assert len(tiles) == 2 * 1


def test_canonical_epsg():
    assert canonical_epsg(35.3, 36.7) == "EPSG:32636"
    assert canonical_epsg(36.5, 36.7) == "EPSG:32637"
    assert canonical_epsg(35.3, -20.0) == "EPSG:32736"


def test_tile_filter_polygon():
    tri = Polygon([(0, 0), (10, 0), (0, 10)])
    f = tile_filter(tri)
    assert f((1, 1, 2, 2)) is True or f((1, 1, 2, 2)) == True
    assert not f((9, 9, 10, 10))
    assert tile_filter(None)((999, 999, 1000, 1000))
