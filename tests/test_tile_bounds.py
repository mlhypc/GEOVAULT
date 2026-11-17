"""Offline tests for the WGS84 bounds of a tile (common.wgs84_bounds): whatever the
projection, the ground of the tile lies inside them. The ground is sampled on the edges
and inside the tile, and only where the projection can place it (a point that comes back
to itself from WGS84)."""

import math

import numpy as np
import pytest
from pyproj import Transformer

from geovault.grid import Grid
from geovault.sources import common

SINU = "+proj=sinu +lon_0=0 +x_0=0 +y_0=0 +R=6371007.181 +units=m +no_defs"
MERC_180 = "+proj=merc +lon_0=180 +datum=WGS84 +units=m +no_defs"
MERC_OVER = "+proj=merc +lon_0=0 +over +datum=WGS84 +units=m +no_defs"
IGH = "+proj=igh +lon_0=0 +x_0=0 +y_0=0 +ellps=WGS84 +units=m +no_defs"
MODIS = (231.65635826395834, 463.3127165279165, 926.625433055833)      # pixel sizes, 240 px tiles
SINU_R = 6371007.181


def to(src, dst):
    return Transformer.from_crs(src, dst, always_xy=True)


def ground(grid, tx, ty, edge=200, inner=40):
    """(lon, lat) of points on the edges of a tile and inside it."""
    w, s, e, n = grid.tile_bounds(tx, ty)
    t = np.linspace(0.0, 1.0, edge)
    gx, gy = np.meshgrid(np.linspace(w, e, inner), np.linspace(s, n, inner))
    x = np.concatenate([w + (e - w) * t, w + (e - w) * t, np.full(edge, w), np.full(edge, e), gx.ravel()])
    y = np.concatenate([np.full(edge, s), np.full(edge, n), s + (n - s) * t, s + (n - s) * t, gy.ravel()])
    lon, lat = to(grid.crs, "EPSG:4326").transform(x, y)
    ok = np.isfinite(lon) & np.isfinite(lat)
    bx, by = to("EPSG:4326", grid.crs).transform(lon[ok], lat[ok])
    placed = np.hypot(bx - x[ok], by - y[ok]) < grid.res * 1e-3
    return lon[ok][placed], lat[ok][placed]


def left_out(grid, tx, ty, bounds):
    """How far the ground of a tile reaches past the bounds, in pixels."""
    lon, lat = ground(grid, tx, ty)
    if not lon.size:
        return 0.0
    w, s, e, n = bounds
    width = 360.0 if e - w >= 360 else (e - w) % 360
    east_of_west = (lon - w) % 360
    in_lon = np.where(east_of_west <= width, 0.0, np.minimum(east_of_west - width, 360 - east_of_west))
    in_lat = np.maximum(s - lat, 0) + np.maximum(lat - n, 0)
    metres = np.maximum(in_lon * 111320 * np.cos(np.radians(lat)), in_lat * 110574)
    return float(metres.max()) / grid.res


def around(grid, lon, lat, reach=1):
    tx, ty = grid.tile_of_point(*to("EPSG:4326", grid.crs).transform(lon, lat))
    return [(grid, tx + d, ty) for d in range(-reach, reach + 1)]


def utm_tiles():
    out = []
    for epsg, lon in (("EPSG:32660", 180.0), ("EPSG:32601", -180.0),
                      ("EPSG:32760", 180.0), ("EPSG:32701", -180.0)):
        south = epsg[7] == "7"
        for lat in (0.001, 52.0, 80.0):
            for res in (10, 1000):
                out += around(Grid(epsg, res, 256), lon, -lat if south else lat)
    out += around(Grid("EPSG:32636", 10, 256), 33.0, 39.0)             # far from the antimeridian
    out += around(Grid("EPSG:32636", 1000, 256), 33.0, 39.0)           # across a central meridian
    return out


def polar_tiles():
    out = []
    for epsg in ("EPSG:3413", "EPSG:3031", "EPSG:3995", "EPSG:3976"):
        for res in (100, 1000):
            ts = res * 256
            # the pole on a corner, in the middle, on an edge, off centre, two pixels off an edge
            for ox, oy in ((0, 0), (-ts / 2, -ts / 2), (-ts / 2, 0), (0, -ts / 2),
                           (-ts * 0.3, -ts * 0.7), (2 * res, -ts / 2), (-ts / 2, 1.5 * res)):
                grid = Grid(epsg, res, 256, ox, oy)
                out += [(grid, tx, ty) for tx in range(-2, 2) for ty in range(-2, 2)]
    return out


def sinusoidal_tiles():
    out = []
    for res in MODIS:
        grid = Grid(SINU, res, 240)
        top = round(math.pi / 2 * SINU_R / grid.tile_size) - 1          # the row under the north pole
        out += [(grid, tx, ty) for ty in (top, -top - 1) for tx in range(-3, 3)]
        for lat in (0.5, 39.0, 60.0, 85.0):                             # the two ends of the projection
            end = math.pi * SINU_R * math.cos(math.radians(lat))
            ty = math.floor(math.radians(lat) * SINU_R / grid.tile_size)
            out += [(grid, math.floor(side * end / grid.tile_size) + d, ty)
                    for side in (1, -1) for d in range(-2, 3)]
        out += around(grid, 33.0, 39.0)
    return out


def other_tiles():
    out = []
    for crs in ("EPSG:3832", MERC_180, "EPSG:3857"):
        for res in (10, 1000, 100000):                                  # 100 km pixels: tiles wider than 180 degrees
            for lon in (180.0, 179.999, -179.999, 0.0, 150.0):
                out += around(Grid(crs, res, 256), lon, 60.0)
    out += [(Grid(MERC_OVER, 100000, 256), tx, 0) for tx in (-2, -1, 0, 1)]
    out += [(Grid(IGH, 250.0, 256), tx, ty) for tx in range(40, 60, 3) for ty in range(60, 72, 3)]
    return out


@pytest.mark.parametrize("tiles", [utm_tiles, polar_tiles, sinusoidal_tiles, other_tiles])
def test_the_ground_of_a_tile_lies_inside_its_bounds(tiles):
    checked = 0
    for grid, tx, ty in tiles():
        bounds = common.wgs84_bounds(grid, tx, ty, grid.crs)
        if all(math.isfinite(v) for v in bounds):
            assert left_out(grid, tx, ty, bounds) < 0.01, (grid, tx, ty, bounds)
            checked += 1
    assert checked > 40


def test_the_bounds_of_a_usual_tile_are_those_of_its_ground():
    """No wider than the tile: a tile of 2.56 km in Anatolia, to a hundredth of a pixel."""
    grid = Grid("EPSG:32636", 10, 256)
    for _, tx, ty in around(grid, 33.0, 39.0):
        w, s, e, n = common.wgs84_bounds(grid, tx, ty, grid.crs)
        lon, lat = ground(grid, tx, ty)
        assert (w, s, e, n) == pytest.approx((lon.min(), lat.min(), lon.max(), lat.max()), abs=1e-6)
        assert left_out(grid, tx, ty, (w, s, e, n)) < 1e-4


def test_a_tile_beside_a_pole_reaches_as_far_north_as_its_ground():
    """The point of the tile nearest the pole lies on an edge, not on a corner."""
    grid = Grid("EPSG:3413", 1000, 256, ox=-128000.0, oy=-128000.0)    # the pole in the middle of tile (0, 0)
    w, s, e, n = common.wgs84_bounds(grid, 1, 0, grid.crs)
    corners = to(grid.crs, "EPSG:4326").transform([128000.0, 384000.0] * 2, [-128000.0] * 2 + [128000.0] * 2)[1]
    assert 0 <= n - ground(grid, 1, 0)[1].max() < 1e-4
    assert n - max(corners) > 0.4                                       # over 40 km past the corners
    assert w < e and e - w < 180


@pytest.mark.parametrize("epsg, pole", [("EPSG:3413", 90.0), ("EPSG:3031", -90.0)])
@pytest.mark.parametrize("ox, oy", [(-128000.0, -128000.0), (-128000.0, 0.0), (0.0, -128000.0), (0.0, 0.0)])
def test_a_tile_touching_a_pole_spans_every_longitude(epsg, pole, ox, oy):
    """The pole inside a tile, on an edge or on a corner: each tile touching it."""
    grid = Grid(epsg, 1000, 256, ox, oy)
    touching = [(tx, ty) for tx in (-1, 0) for ty in (-1, 0)
                if grid.tile_bounds(tx, ty)[0] <= 0 <= grid.tile_bounds(tx, ty)[2]
                and grid.tile_bounds(tx, ty)[1] <= 0 <= grid.tile_bounds(tx, ty)[3]]
    assert len(touching) == {(-128000.0, -128000.0): 1, (0.0, 0.0): 4}.get((ox, oy), 2)
    for tx, ty in touching:
        w, s, e, n = common.wgs84_bounds(grid, tx, ty, epsg)
        assert (w, e) == (-180.0, 180.0) and (n if pole > 0 else s) == pole
    beside = common.wgs84_bounds(grid, 3, 3, epsg)
    assert beside[2] - beside[0] < 180 and abs(beside[3]) < 90 and abs(beside[1]) < 90


@pytest.mark.parametrize("res", MODIS)
def test_the_sinusoidal_tiles_under_a_pole_span_every_longitude(res):
    """The top edge of the top row ends a millimetre short of the pole."""
    grid = Grid(SINU, res, 240)
    top = round(math.pi / 2 * SINU_R / grid.tile_size) - 1
    for tx, ty, pole in ((-1, top, 90.0), (0, top, 90.0), (-1, -top - 1, -90.0), (0, -top - 1, -90.0)):
        w, s, e, n = common.wgs84_bounds(grid, tx, ty, SINU)
        assert (w, e) == (-180.0, 180.0) and (n if pole > 0 else s) == pole


def test_a_tile_within_a_pixel_of_a_pole_spans_every_longitude():
    """The tile west of the pole holds it; the tile east of it starts 0.8 or 3 pixels away."""
    near = Grid("EPSG:3413", 1000, 256, ox=800.0, oy=-128000.0)
    w, s, e, n = common.wgs84_bounds(near, 0, 0, near.crs)
    assert (w, e, n) == (-180.0, 180.0, 90.0)
    far = Grid("EPSG:3413", 1000, 256, ox=3000.0, oy=-128000.0)
    w, s, e, n = common.wgs84_bounds(far, 0, 0, far.crs)
    assert 170 < e - w < 180 and 89.9 < n < 90
    assert left_out(far, 0, 0, (w, s, e, n)) < 0.01
    assert common.wgs84_bounds(far, -1, 0, far.crs)[::2] == (-180.0, 180.0)


def test_an_edge_is_followed_between_its_corners():
    """A conic grid with tiles of 256 km: the corners alone leave out 1.8 pixels."""
    grid = Grid("EPSG:5070", 1000, 256)
    assert left_out(grid, 16, 38, common.wgs84_bounds(grid, 16, 38, grid.crs)) < 0.1


def test_a_tile_wider_than_half_the_world_keeps_its_own_side():
    grid = Grid("EPSG:3832", 100000, 256)                               # Mercator centred on 150 E
    w, s, e, n = common.wgs84_bounds(grid, -1, 0, grid.crs)             # 80 W eastwards to 150 E
    assert (w, e) == pytest.approx((-79.97, 150.0), abs=0.01)
    w, s, e, n = common.wgs84_bounds(grid, 0, 0, grid.crs)              # 150 E eastwards to 20 E
    assert (w, e) == pytest.approx((150.0, 19.97), abs=0.01)


def test_a_tile_around_the_world_spans_every_longitude():
    """No gap between the longitudes of its outline is wider than a step along it."""
    half = 20037508.342789244
    grid = Grid("EPSG:3857", 2 * half / 256, 256, -half, -half)        # Web Mercator, zoom 0
    w, s, e, n = common.wgs84_bounds(grid, 0, 0, grid.crs)
    assert (w, e) == (-180.0, 180.0) and s < -85 and n > 85
    east_half = Grid("EPSG:3857", half / 256, 256)                      # zoom 1: 0 to 180 E
    assert common.wgs84_bounds(east_half, 0, 0, east_half.crs)[::2] == pytest.approx((0.0, 180.0))


def test_longitudes_past_180_are_brought_back():
    grid = Grid(MERC_OVER, 100000, 256)
    w, s, e, n = common.wgs84_bounds(grid, 0, 0, MERC_OVER)             # 0 eastwards to 230 E
    assert (w, e) == pytest.approx((0.0, -130.03), abs=0.01)


def test_a_tile_with_a_corner_off_the_projection_has_no_finite_bounds():
    grid = Grid(IGH, 250.0, 256)
    assert not all(math.isfinite(v) for v in common.wgs84_bounds(grid, -285, -134, IGH))
    ease = Grid("EPSG:6933", 1000, 256)                                 # the two north corners come back NaN
    bounds = common.wgs84_bounds(ease, -68, 28, ease.crs)
    assert math.isnan(bounds[1]) and math.isnan(bounds[3])


def test_a_geographic_tile_keeps_the_longitudes_of_its_grid():
    grid = Grid("EPSG:4326", 0.25, 100, -0.125, -0.125)                 # tiles of 25 degrees
    assert common.wgs84_bounds(grid, 7, 1, "EPSG:4326") == pytest.approx((174.875, 24.875, 199.875, 49.875))
    world = Grid("EPSG:4326", 1.0, 360)
    assert common.wgs84_bounds(world, 0, 0, "EPSG:4326") == pytest.approx(world.tile_bounds(0, 0))
