"""Offline tests for tiles across the antimeridian. Such a tile of a projected CRS has
ground at both ends of the longitude scale; its WGS84 bounds are stored with west > east,
as a bbox is in GeoJSON and STAC, instead of a span around the whole world."""

import numpy as np
import pytest
from pyproj import Transformer
from shapely.geometry import box

from geovault import catalog, cli, reader, store
from geovault.aoi import tile_filter
from geovault.grid import Grid
from geovault.sources import common, dem_3dep_1m

CRS = "EPSG:32660"                       # UTM zone 60, 174 E to 180
GRID = Grid(CRS, 10, 256)
TO_UTM = Transformer.from_crs("EPSG:4326", CRS, always_xy=True)
TO_WGS = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)
ACROSS = GRID.tile_of_point(*TO_UTM.transform(179.99, 52.0))       # 179.97 E .. 179.99 W
BEFORE = GRID.tile_of_point(*TO_UTM.transform(179.90, 52.0))       # wholly east longitudes
ELSEWHERE = (33.0, 51.9, 33.1, 52.1)                               # the same latitude, far away


def test_a_tile_across_the_antimeridian_has_west_greater_than_east():
    w, s, e, n = common.wgs84_bounds(GRID, *ACROSS, CRS)
    assert 179.9 < w < 180 and -180 < e < -179.9 and s < 52.0 < n
    assert 0.037 < (e - w) % 360 < 0.041                            # 2.56 km at 52 N, turned a little


def test_other_tiles_keep_west_below_east():
    w, s, e, n = common.wgs84_bounds(GRID, *BEFORE, CRS)
    assert 179.8 < w < e < 180


def test_the_aoi_filter_takes_both_ends_of_such_a_tile():
    bounds = common.wgs84_bounds(GRID, *ACROSS, CRS)
    assert tile_filter(box(179.98, 51.99, 179.995, 52.01))(bounds)          # east longitudes
    assert tile_filter(box(-179.999, 51.99, -179.995, 52.01))(bounds)       # west longitudes
    assert not tile_filter(box(*ELSEWHERE))(bounds)
    assert not tile_filter(box(179.5, 51.99, 179.9, 52.01))(bounds)         # short of the tile


def test_the_zone_rule_keeps_a_tile_across_the_antimeridian():
    assert common.tile_is_canonical(GRID, *ACROSS, CRS)                      # zone 60 ground
    zone1 = Grid("EPSG:32601", 10, 256)
    to1 = Transformer.from_crs("EPSG:4326", "EPSG:32601", always_xy=True)
    assert common.tile_is_canonical(zone1, *zone1.tile_of_point(*to1.transform(-179.99, 52.0)), "EPSG:32601")
    far = GRID.tile_of_point(*TO_UTM.transform(-178.0, 52.0))                # zone 1 ground, zone 60 numbers
    assert not common.tile_is_canonical(GRID, *far, CRS)


def test_the_3dep_zone_test_takes_both_ends_of_such_a_tile():
    bounds = common.wgs84_bounds(GRID, *ACROSS, CRS)
    assert dem_3dep_1m.touches_zone(bounds, 60) and dem_3dep_1m.touches_zone(bounds, 1)
    assert not dem_3dep_1m.touches_zone(bounds, 59) and not dem_3dep_1m.touches_zone(bounds, 2)
    before = common.wgs84_bounds(GRID, *BEFORE, CRS)
    assert dem_3dep_1m.touches_zone(before, 60) and not dem_3dep_1m.touches_zone(before, 1)


def test_a_request_is_sampled_by_the_width_of_such_a_tile():
    """An eighth of the tile's width in longitude, not of east minus west."""
    w, s, e, n = common.wgs84_bounds(GRID, *ACROSS, CRS)
    row = {"west": w, "south": s, "east": e, "north": n}
    lon, lat = reader._request_points((-179.9, 52.0, -178.9, 52.001), None, row)
    assert np.unique(lon).size == int(np.ceil(1.0 / (((e - w) % 360) / 8))) > 200
    assert np.unique(lat).size == 32


@pytest.fixture
def dateline(tmp_path, monkeypatch):
    """The tile across the antimeridian and its neighbour: 100 on east longitudes, 200 on west."""
    monkeypatch.setattr(store, "STORE", tmp_path / "store")
    monkeypatch.setattr(catalog, "STORE", tmp_path / "store")
    monkeypatch.setattr(catalog, "CATALOG", tmp_path / "catalog")
    w = store.Writer("d")
    half = (np.arange(256) + 0.5) * 10
    for tx, ty in (ACROSS, BEFORE):
        west, _, _, north = GRID.tile_bounds(tx, ty)
        x, y = np.meshgrid(west + half, north - half)
        arr = np.where(TO_WGS.transform(x, y)[0] > 0, 100, 200).astype(np.uint16)
        w.add("b", "2026-06-01", tx, ty, CRS, 10, 256, common.wgs84_bounds(GRID, tx, ty, CRS),
              0, 0.0, "scene", common.encode_geotiff(arr, GRID, tx, ty, CRS, 0), arr.size)
    w.close()
    catalog.rebuild("d")


def found(bbox):
    return sorted((r["x"], r["y"]) for r in reader.tiles_for_bbox("d", bbox, "b"))


def test_a_bbox_finds_the_tile_from_either_side(dateline):
    assert found((179.98, 51.99, 179.995, 52.01)) == [ACROSS]
    assert found((-179.999, 51.99, -179.995, 52.01)) == [ACROSS]
    assert found((179.89, 51.99, 179.91, 52.01)) == [BEFORE]


def test_a_bbox_elsewhere_at_that_latitude_finds_nothing(dateline):
    assert found(ELSEWHERE) == []
    assert catalog.coverage("d", bbox=ELSEWHERE) == []
    assert catalog.coverage("d", bbox=(179.98, 51.99, 179.995, 52.01)) == [("2026-06-01", "b", 1, 0.0)]
    assert catalog.coverage("d", bbox=(-179.999, 51.99, -179.995, 52.01)) == [("2026-06-01", "b", 1, 0.0)]
    assert catalog.coverage("d", bbox=(179.5, 51.99, 180.0, 52.01)) == [("2026-06-01", "b", 2, 0.0)]


@pytest.mark.parametrize("bbox", [(179.0, 51.9, -179.0, 52.1), (33.0, 52.1, 33.1, 51.9),
                                  (33.0, 51.9, float("nan"), 52.1), (-float("inf"), 51.9, 33.1, 52.1)])
def test_a_bbox_across_the_antimeridian_is_refused(dateline, bbox):
    """west > east would match no tile on either side: an error, not an empty answer."""
    with pytest.raises(ValueError, match="west <= east"):
        reader.tiles_for_bbox("d", bbox, "b")
    with pytest.raises(ValueError, match="west <= east"):
        catalog.coverage("d", bbox=bbox)


def test_the_coverage_command_refuses_such_a_bbox(dateline, capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main(["coverage", "d", "--bbox", "179", "51.9", "-179", "52.1"])
    assert stop.value.code == 2 and "west <= east" in capsys.readouterr().err


def test_series_and_mosaic_read_both_sides(dateline):
    assert [r["value"] for r in reader.series("d", 179.99, 52.0, "b")] == [100]
    assert [r["value"] for r in reader.series("d", -179.995, 52.0, "b")] == [200]
    arr, _, crs = reader.mosaic("d", (-179.999, 51.995, -179.995, 52.005), "b", "2026-06-01")
    assert crs == CRS and set(np.unique(arr)) == {100, 200}
    assert reader.mosaic("d", ELSEWHERE, "b", "2026-06-01") is None


def test_a_geographic_grid_is_read_on_each_side_from_its_own_tile(tmp_path, monkeypatch):
    """A geographic grid has no tile across the antimeridian: the tile ending past 180 E
    and the tile starting before 180 W each keep the longitudes of the grid."""
    monkeypatch.setattr(store, "STORE", tmp_path / "store")
    monkeypatch.setattr(catalog, "STORE", tmp_path / "store")
    monkeypatch.setattr(catalog, "CATALOG", tmp_path / "catalog")
    crs, grid = "EPSG:4326", Grid("EPSG:4326", 0.25, 100, -0.125, -0.125)
    w = store.Writer("g")
    for value, lon in ((100, 179.9), (200, -179.9)):
        tx, ty = grid.tile_of_point(lon, 52.0)
        arr = np.full((100, 100), value, np.uint16)
        w.add("b", "2026-06-01", tx, ty, crs, 0.25, 100, common.wgs84_bounds(grid, tx, ty, crs),
              0, 0.0, "scene", common.encode_geotiff(arr, grid, tx, ty, crs, 0), arr.size)
    w.close()
    catalog.rebuild("g")
    assert [r["value"] for r in reader.series("g", 179.9, 52.0, "b")] == [100]
    assert [r["value"] for r in reader.series("g", -179.9, 52.0, "b")] == [200]
