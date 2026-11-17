"""Offline tests for the native rectangle of a WGS84 bbox (common.bbox_to_crs) and the
tiles planned from it. A parallel is a curve in UTM: the south edge of a wide bbox dips
below its two corners near the central meridian, and a rectangle made from the corners
alone leaves that strip, and the tiles in it, out of the plan."""

import numpy as np
import pytest
from pyproj import Transformer

from geovault.grid import Grid
from geovault.sources import common

SINU = "+proj=sinu +lon_0=0 +x_0=0 +y_0=0 +R=6371007.181 +units=m +no_defs"
EVERYWHERE = lambda bounds: True

# (bbox, crs, a point just inside the bbox where its edge bulges beyond the corners)
BULGES = [
    ((32.5, 36.04, 33.5, 36.24), "EPSG:32636", (33.0, 36.0405)),   # one degree: a 116 m dip
    ((30.0, 36.0, 36.0, 42.0), "EPSG:32636", (33.0, 36.001)),      # a whole zone: south edge
    ((26.0, 36.0, 45.0, 42.0), "EPSG:32636", (33.0, 36.05)),       # three zones wide
    ((26.0, 36.0, 45.0, 42.0), "EPSG:32637", (39.0, 36.05)),
    ((30.0, -42.0, 36.0, -36.0), "EPSG:32736", (33.0, -36.001)),   # southern hemisphere: north edge
    ((10.0, -5.0, 20.0, 5.0), SINU, (19.999, 0.01)),               # sinusoidal: east edge at the equator
]


def planned(bbox, crs, res=10, px=256):
    grid = Grid(crs, res, px)
    return grid, set(common.plan_tiles(grid, bbox, crs, "B", "2026-01-01", set(), True, EVERYWHERE))


@pytest.mark.parametrize("bbox, crs, point", BULGES)
def test_the_tile_under_a_bulging_edge_is_planned(bbox, crs, point):
    grid, tiles = planned(bbox, crs)
    x, y = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(*point)
    assert grid.tile_of_point(x, y) in tiles


@pytest.mark.parametrize("bbox, crs", [(b, c) for b, c, _ in BULGES] + [
    ((32.99, 37.0, 33.01, 37.02), "EPSG:32636"),                   # a parcel
    ((-119.5, 36.55, -119.3, 36.7), "EPSG:32611"),
    ((26.0, 36.0, 45.0, 42.0), "EPSG:4326"),
])
def test_every_point_of_the_bbox_lies_in_its_native_rectangle(bbox, crs):
    w, s, e, n = bbox
    rng = np.random.default_rng(0)
    edge = np.linspace(0.0, 1.0, 2001)
    lon = np.concatenate([rng.uniform(w, e, 4000), w + (e - w) * edge, w + (e - w) * edge,
                          np.full(2001, w), np.full(2001, e)])
    lat = np.concatenate([rng.uniform(s, n, 4000), np.full(2001, s), np.full(2001, n),
                          s + (n - s) * edge, s + (n - s) * edge])
    xs, ys = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(lon, lat)
    nw, ns, ne, nn = common.bbox_to_crs(bbox, crs)
    slack = 1.0 if crs != "EPSG:4326" else 1e-9                     # a metre, in native units
    assert xs.min() >= nw - slack and xs.max() <= ne + slack
    assert ys.min() >= ns - slack and ys.max() <= nn + slack


def test_a_small_bbox_plans_the_tiles_its_corners_span():
    """A bbox no larger than one step is reprojected by its corners, as it always was."""
    bbox, crs = (32.99, 37.0, 33.01, 37.02), "EPSG:32636"
    grid, tiles = planned(bbox, crs)
    tr = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    xs, ys = tr.transform([bbox[0], bbox[2], bbox[0], bbox[2]], [bbox[1], bbox[1], bbox[3], bbox[3]])
    assert tiles == set(grid.tiles_for_bounds(min(xs), min(ys), max(xs), max(ys)))


def test_candidate_tiles_follow_the_edges_too():
    bbox, crs = (30.0, 36.0, 36.0, 42.0), "EPSG:32636"
    grid = Grid(crs, 10, 256)
    x, y = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(33.0, 36.001)
    assert grid.tile_of_point(x, y) in set(common.candidate_tiles(grid, bbox, crs))


def test_a_bbox_given_the_other_way_round_is_the_same_bbox():
    crs = "EPSG:32636"
    want = common.bbox_to_crs((30.0, 36.0, 36.0, 42.0), crs)
    for swapped in ((36.0, 36.0, 30.0, 42.0), (30.0, 42.0, 36.0, 36.0), (36.0, 42.0, 30.0, 36.0)):
        assert common.bbox_to_crs(swapped, crs) == pytest.approx(want, abs=1e-6)


def test_an_empty_part_of_an_aoi_gives_no_candidates():
    from shapely.geometry import GeometryCollection, Polygon, box
    grid = Grid("EPSG:32636", 10, 256)
    part = box(32.99, 37.0, 33.01, 37.02)
    aoi = GeometryCollection([part, Polygon()])
    assert common.candidate_tiles(grid, aoi.bounds, "EPSG:32636", aoi) == \
        common.candidate_tiles(grid, part.bounds, "EPSG:32636", part)


def test_candidates_of_an_aoi_follow_the_edges_of_each_part():
    from shapely.geometry import MultiPolygon, box
    grid, crs = Grid("EPSG:32636", 10, 256), "EPSG:32636"
    aoi = MultiPolygon([box(30.0, 36.0, 36.0, 42.0), box(32.99, 37.0, 33.01, 37.02)])
    x, y = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(33.0, 36.001)
    assert grid.tile_of_point(x, y) in set(common.candidate_tiles(grid, aoi.bounds, crs, aoi))


@pytest.mark.parametrize("bbox", [(-1.5, 52.0, -1.4, 52.1), (-1.5, 52.0, -1.49, 52.01), (-1.5, 52.0, -1.5, 52.0)])
def test_a_geographic_crs_on_another_datum_gets_its_shift(bbox):
    """OSGB36 lies about a hundred metres from WGS84: the rectangle is the shifted bbox,
    whatever was asked of the same CRS before."""
    crs = "EPSG:4277"
    common.bbox_to_crs((11.0, 41.0, 11.2, 41.2), crs)
    xs, ys = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(
        [bbox[0], bbox[2], bbox[0], bbox[2]], [bbox[1], bbox[1], bbox[3], bbox[3]])
    got = common.bbox_to_crs(bbox, crs)
    assert got == pytest.approx((min(xs), min(ys), max(xs), max(ys)), abs=1e-7)
    assert abs(got[0] - bbox[0]) > 1e-4                              # shifted, not passed through
