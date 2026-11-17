"""Offline tests for the UTM zone rule of the ingest plan.

Sentinel-2 granules follow the MGRS grid: ground near a zone line, and near the equator,
comes in granules of two CRS on the same day. One of the two is kept, but every point must
stay in a kept tile of the CRS MGRS assigns to it. Landsat and Sentinel-1 scenes take the
zone of the scene, not of the ground, so no rule by location applies to them."""

from types import SimpleNamespace

import pytest
from pyproj import Transformer

from geovault.grid import Grid
from geovault.sources import common, landsat, s1_pc, s2_earthsearch

TILE_PX = 256


def mgrs_crs(lon, lat):
    """CRS of the MGRS grid zone a point lies in, written out from the MGRS definition:
    regular 6 degree zones, zone 32 widened to 3..12 E between 56 and 64 N, and four wide
    zones between 72 and 84 N (31: 0..9, 33: 9..21, 35: 21..33, 37: 33..42 E)."""
    zone = int((lon + 180) // 6) + 1
    if 56 <= lat < 64 and 3 <= lon < 12:
        zone = 32
    if 72 <= lat < 84 and 0 <= lon < 42:
        zone = 31 if lon < 9 else 33 if lon < 21 else 35 if lon < 33 else 37
    return f"EPSG:{(32600 if lat >= 0 else 32700) + zone}"


def tile_at(lon, lat, crs, res):
    grid = Grid(crs, res, TILE_PX)
    x, y = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(lon, lat)
    return grid, grid.tile_of_point(x, y)


def frange(a, b, step):
    n = int(round((b - a) / step))
    return [a + i * step for i in range(n + 1)]


# (name, longitudes, latitudes): dense samples across every kind of MGRS boundary, and
# through the whole width of the zones that depart from the regular 6 degrees
REGIONS = [
    ("zone line 36E, Turkey", frange(35.9, 36.1, 0.004), frange(36.0, 42.0, 0.37)),
    ("zone line 30E, southern hemisphere", frange(29.9, 30.1, 0.004), frange(-30.0, -24.0, 0.41)),
    ("zone line 72W, both hemispheres", frange(-72.1, -71.9, 0.004), frange(-40.0, 40.0, 3.7)),
    ("antimeridian", frange(179.9, 179.996, 0.004) + frange(-180.0, -179.9, 0.004),
     frange(-50.0, 50.0, 4.3)),
    ("equator", frange(33.0, 33.3, 0.05), frange(-0.05, 0.05, 0.0011)),
    ("equator on a zone line", frange(35.95, 36.05, 0.004), frange(-0.05, 0.05, 0.0023)),
    ("Norway, 0E to 12E between 56 and 64 N", frange(-0.2, 12.2, 0.05), frange(55.8, 64.2, 0.13)),
    ("Svalbard, 0E to 42E between 72 and 84 N", frange(-0.5, 42.5, 0.25), frange(71.8, 84.0, 0.31)),
]


@pytest.mark.parametrize("res", [10, 20, 60])
@pytest.mark.parametrize("name,lons,lats", REGIONS, ids=[r[0] for r in REGIONS])
def test_every_point_is_in_a_kept_tile_of_its_own_mgrs_zone(name, lons, lats, res):
    lost = []
    for lat in lats:
        for lon in lons:
            crs = mgrs_crs(lon, lat)
            grid, (tx, ty) = tile_at(lon, lat, crs, res)
            if not common.tile_is_canonical(grid, tx, ty, crs):
                lost.append((lon, lat, crs))
    assert not lost, f"{len(lost)} points lost, e.g. {lost[:3]}"


@pytest.mark.parametrize("name,lons,lats", REGIONS, ids=[r[0] for r in REGIONS])
def test_mgrs_epsg_names_the_zone_of_a_point(name, lons, lats):
    wrong = [(lon, lat) for lat in lats for lon in lons
             if common.mgrs_epsg(lon, lat) != mgrs_crs(lon, lat)]
    assert not wrong, f"{len(wrong)} points, e.g. {wrong[:3]}"


@pytest.mark.parametrize("res", [10, 20, 60])
def test_tiles_far_inside_another_zone_are_dropped(res):
    """A tile lying 1.5 tiles past the line is not kept by the neighbouring zone, so each
    zone reaches at most about one tile over the line."""
    step = res * TILE_PX * 1.5 / 88_000          # 1.5 tiles east of 36E at 37 N, in degrees
    grid, (tx, ty) = tile_at(36.0 + step, 37.0, "EPSG:32636", res)
    assert not common.tile_is_canonical(grid, tx, ty, "EPSG:32636")
    grid, (tx, ty) = tile_at(36.0 - step, 37.0, "EPSG:32637", res)
    assert not common.tile_is_canonical(grid, tx, ty, "EPSG:32637")
    north = res * TILE_PX * 1.5 / 110_000        # 1.5 tiles off the equator
    grid, (tx, ty) = tile_at(33.2, north, "EPSG:32736", res)
    assert not common.tile_is_canonical(grid, tx, ty, "EPSG:32736")
    grid, (tx, ty) = tile_at(33.2, -north, "EPSG:32636", res)
    assert not common.tile_is_canonical(grid, tx, ty, "EPSG:32636")


def test_zones_that_mgrs_does_not_use():
    """Bergen is zone 32 in MGRS (regular zone 31); Ny-Alesund is zone 33 (regular zone 32)."""
    for lon, lat, own, other in [(5.3, 60.4, "EPSG:32632", "EPSG:32631"),
                                 (11.9, 78.9, "EPSG:32633", "EPSG:32632")]:
        grid, (tx, ty) = tile_at(lon, lat, own, 10)
        assert common.tile_is_canonical(grid, tx, ty, own)
        grid, (tx, ty) = tile_at(lon, lat, other, 10)
        assert not common.tile_is_canonical(grid, tx, ty, other)


def test_a_crs_without_a_utm_zone_has_no_zone_rule():
    grid = Grid("EPSG:4326", 0.05, 100)
    assert common.tile_is_canonical(grid, 7, 7, "EPSG:4326")


def item(crs, assets):
    """A STAC item as the three adapters read it, with the fields each takes its
    acquisition from."""
    return SimpleNamespace(id="scene", assets={a: None for a in assets}, properties={
        "datetime": "2026-07-02T08:00:00Z", "proj:code": crs, "platform": "satellite",
        "s2:datatake_id": "GS2A_20260702T080000_000001_N05.11", "sat:absolute_orbit": 1,
        "landsat:wrs_path": "175"})


def test_s2_plan_follows_the_mgrs_rule():
    bergen = (5.25, 60.35, 5.35, 60.40)
    plan = s2_earthsearch.plan_scene(item("EPSG:32632", ["red"]), bergen, bands=["B4"])
    assert plan.get("B4", 0) > 0
    assert s2_earthsearch.plan_scene(item("EPSG:32631", ["red"]), bergen, bands=["B4"]) == {}
    both = s2_earthsearch.plan_scene(item("EPSG:32631", ["red"]), bergen, bands=["B4"],
                                     keep_offzone=True)
    assert both.get("B4", 0) > 0


@pytest.mark.parametrize("src,asset,band", [(landsat, "red", "red"), (s1_pc, "vv", "VV")],
                         ids=["landsat", "s1"])
def test_scene_zone_sources_store_every_scene_in_its_own_crs(src, asset, band):
    """Ground 27 km east of the 36E line, imaged by a scene in the zone west of it; ground
    in the southern hemisphere in a northern CRS (Landsat always uses the northern one)."""
    for crs, bbox in [("EPSG:32636", (36.28, 36.98, 36.32, 37.02)),
                      ("EPSG:32655", (149.0, -35.4, 149.1, -35.3))]:
        full = src.plan_scene(item(crs, [asset]), bbox, bands=[band], keep_offzone=True)
        assert full.get(band, 0) > 0
        assert src.plan_scene(item(crs, [asset]), bbox, bands=[band]) == full
