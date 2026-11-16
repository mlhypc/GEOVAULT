"""Offline tests for reader.mosaic and reader.clip: which value marks a pixel without an
observation (no stored tile, nodata inside a tile, outside the polygon), bands that have no
nodata value at all, the masked return, and, where the request is stored in more than one
CRS, which CRS the result takes and when it warns that ground was left out."""

import numpy as np
import pytest
from pyproj import Transformer
from rasterio.transform import Affine
from shapely.geometry import box

from geovault import catalog, reader, store
from geovault.sources.common import encode_blob

CRS = "EPSG:32635"
RES = 10
PX = 10
X0, Y1 = 500000.0, 4200000.0            # upper-left corner of tile (0, 0)
SIZE = RES * PX
TO_WGS = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)


def tile_bounds(tx, ty):
    """Native (w, s, e, n) of a test tile; ty grows southwards."""
    w, n = X0 + tx * SIZE, Y1 - ty * SIZE
    return w, n - SIZE, w + SIZE, n


def wgs_box(w, s, e, n):
    xs, ys = TO_WGS.transform([w, e, w, e], [s, s, n, n])
    return min(xs), min(ys), max(xs), max(ys)


def add_tile(writer, band, date, tx, ty, arr, nodata, cloud_pct=None):
    w, s, e, n = tile_bounds(tx, ty)
    blob = encode_blob(arr, CRS, Affine(RES, 0, w, 0, -RES, n), nodata)
    writer.add(band, date, tx, ty, CRS, RES, PX, wgs_box(w, s, e, n), nodata, cloud_pct,
               "scene", blob, int(arr.size))


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """One dataset on a 2 x 2 tile block. Bands:
    f     float32, nodata -9999, all four tiles, one nodata pixel in tile (0, 0)
    u     uint8, nodata 255, all four tiles, every pixel the real value 0
    n     int32 without nodata, three tiles (tile (1, 1) is not stored), real zeros
    nan   float32, nodata NaN, tile (0, 0) only, one NaN pixel
    two   int16, nodata -1, tile (0, 0) on two dates: the clean one has a nodata pixel
    two_n int32 without nodata, tile (0, 0) on two dates"""
    monkeypatch.setattr(store, "STORE", tmp_path / "store")
    monkeypatch.setattr(catalog, "STORE", tmp_path / "store")
    monkeypatch.setattr(catalog, "CATALOG", tmp_path / "catalog")
    w = store.Writer("edge")
    for tx in (0, 1):
        for ty in (0, 1):
            f = np.full((PX, PX), 5.5, np.float32)
            if (tx, ty) == (0, 0):
                f[0, 0] = -9999.0
            add_tile(w, "f", "2026-01-01", tx, ty, f, -9999.0)
            add_tile(w, "u", "2026-01-01", tx, ty, np.zeros((PX, PX), np.uint8), 255)
            if (tx, ty) != (1, 1):
                n = np.zeros((PX, PX), np.int32)
                n[5, 5] = 70
                add_tile(w, "n", "2026-01-01", tx, ty, n, None)
    nan = np.full((PX, PX), 2.0, np.float32)
    nan[3, 3] = np.nan
    add_tile(w, "nan", "2026-01-01", 0, 0, nan, float("nan"))
    clean = np.full((PX, PX), 1, np.int16)
    clean[0, 0] = -1
    add_tile(w, "two", "2026-01-01", 0, 0, clean, -1, cloud_pct=10.0)
    add_tile(w, "two", "2026-01-02", 0, 0, np.full((PX, PX), 2, np.int16), -1, cloud_pct=50.0)
    add_tile(w, "two_n", "2026-01-01", 0, 0, np.full((PX, PX), 1, np.int32), None, cloud_pct=10.0)
    add_tile(w, "two_n", "2026-01-02", 0, 0, np.full((PX, PX), 2, np.int32), None, cloud_pct=50.0)
    w.close()
    catalog.rebuild("edge")


def block_bbox():
    """WGS84 bbox strictly inside the 2 x 2 block, touching all four tiles."""
    return wgs_box(X0 + 5, Y1 - 2 * SIZE + 5, X0 + 2 * SIZE - 5, Y1 - 5)


def inner_polygon():
    """Polygon over the central 100 m of the 200 m block: part of every tile is outside."""
    return box(*wgs_box(X0 + 50, Y1 - 150, X0 + 150, Y1 - 50))


def test_mosaic_keeps_values_and_nodata(vault):
    arr, transform, crs = reader.mosaic("edge", block_bbox(), "f", "2026-01-01")
    assert arr.shape == (2 * PX, 2 * PX) and crs == CRS
    assert (transform.c, transform.f) == (X0, Y1)
    assert arr[0, 0] == -9999.0
    assert (arr == 5.5).sum() == arr.size - 1


def test_clip_fills_outside_with_the_band_nodata(vault):
    arr, _, _ = reader.clip("edge", inner_polygon(), "f", "2026-01-01")
    inside = arr == 5.5
    assert 0 < inside.sum() < arr.size
    assert set(np.unique(arr[~inside]).tolist()) == {-9999.0}


def test_clip_does_not_fill_outside_with_a_real_value(vault):
    """uint8 band whose real value is 0 and whose nodata is 255."""
    arr, _, _ = reader.clip("edge", inner_polygon(), "u", "2026-01-01")
    inside = arr == 0
    assert 0 < inside.sum() < arr.size
    assert set(np.unique(arr[~inside]).tolist()) == {255}


def test_band_without_nodata_needs_masked(vault):
    with pytest.raises(ValueError, match="masked=True"):
        reader.mosaic("edge", block_bbox(), "n", "2026-01-01")
    with pytest.raises(ValueError, match="masked=True"):
        reader.clip("edge", inner_polygon(), "n", "2026-01-01")


def test_masked_mosaic_of_a_band_without_nodata(vault):
    arr, _, _ = reader.mosaic("edge", block_bbox(), "n", "2026-01-01", masked=True)
    assert isinstance(arr, np.ma.MaskedArray) and arr.shape == (2 * PX, 2 * PX)
    assert arr.mask[PX:, PX:].all()                 # tile (1, 1) is not stored
    assert arr.mask.sum() == PX * PX                # real zeros elsewhere stay unmasked
    assert arr.sum() == 3 * 70 and arr.count() == 3 * PX * PX


def test_masked_mosaic_masks_nodata_pixels(vault):
    arr, _, _ = reader.mosaic("edge", block_bbox(), "f", "2026-01-01", masked=True)
    assert arr.mask.sum() == 1 and arr.mask[0, 0]
    assert arr.fill_value == np.float32(-9999.0)
    assert (arr.filled() == reader.mosaic("edge", block_bbox(), "f", "2026-01-01")[0]).all()


def test_masked_clip(vault):
    plain, _, _ = reader.clip("edge", inner_polygon(), "u", "2026-01-01")
    arr, _, _ = reader.clip("edge", inner_polygon(), "u", "2026-01-01", masked=True)
    assert (arr.mask == (plain == 255)).all()
    n, _, _ = reader.clip("edge", inner_polygon(), "n", "2026-01-01", masked=True)
    assert n.mask[PX:, PX:].all() and 0 < n.count() < 3 * PX * PX


def test_nan_nodata(vault):
    bbox = wgs_box(X0 + 5, Y1 - SIZE + 5, X0 + SIZE - 5, Y1 - 5)
    arr, _, _ = reader.mosaic("edge", bbox, "nan", "2026-01-01")
    assert np.isnan(arr[3, 3]) and (arr == 2.0).sum() == arr.size - 1
    m, _, _ = reader.mosaic("edge", bbox, "nan", "2026-01-01", masked=True)
    assert m.mask.sum() == 1 and m.mask[3, 3]


def test_cleanest_tile_wins_and_the_other_fills_its_gaps(vault):
    bbox = wgs_box(X0 + 5, Y1 - SIZE + 5, X0 + SIZE - 5, Y1 - 5)
    arr, _, _ = reader.mosaic("edge", bbox, "two", "2026-01")
    assert arr[0, 0] == 2 and (arr == 1).sum() == arr.size - 1
    n, _, _ = reader.mosaic("edge", bbox, "two_n", "2026-01", masked=True)
    assert (n == 1).all() and not n.mask.any()


# --- one CRS per mosaic: the warning when that leaves ground out -------------------------

EAST = (36.03, 37.000, 36.05, 37.010)          # zone 37 ground, more than a tile from 36E
FAR = (35.950, 37.030, 35.955, 37.040)         # zone 36 ground, also stored in zone 37 here
REGION = (35.95, 37.00, 36.05, 37.04)          # centre in zone 37


def full(val):
    return lambda lon: np.full(lon.shape, val, np.uint16)


def line_store(tmp_path, monkeypatch, dataset, layers):
    """A store of 10 m tiles. layers: (crs, WGS84 boxes whose tiles are stored, pixels) with
    pixels a function of each pixel's longitude returning the uint16 tile (0 = nodata)."""
    from geovault.grid import Grid
    from geovault.sources import common
    monkeypatch.setattr(store, "STORE", tmp_path / "store")
    monkeypatch.setattr(catalog, "STORE", tmp_path / "store")
    monkeypatch.setattr(catalog, "CATALOG", tmp_path / "catalog")
    w = store.Writer(dataset)
    everywhere = lambda bounds: True
    half = (np.arange(256) + 0.5) * 10
    for crs, boxes, pixels in layers:
        grid = Grid(crs, 10, 256)
        to_wgs = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        todo = {t for b in boxes
                for t in common.plan_tiles(grid, b, crs, "b", "2026-06-01", set(), True, everywhere)}
        for tx, ty in sorted(todo):
            west, _, _, north = grid.tile_bounds(tx, ty)
            x, y = np.meshgrid(west + half, north - half)
            arr = pixels(to_wgs.transform(x, y)[0])
            blob = common.encode_geotiff(arr, grid, tx, ty, crs, 0)
            w.add("b", "2026-06-01", tx, ty, crs, 10, 256, common.wgs84_bounds(grid, tx, ty, crs),
                  0, 0.0, "scene", blob, int((arr != 0).sum()))
    w.close()
    catalog.rebuild(dataset)


@pytest.fixture
def zone_line(tmp_path, monkeypatch):
    """Tiles at the 36E line. Zone 36 covers the whole REGION; zone 37 covers only the tiles
    over EAST and over FAR, so the ground between them is in zone 36 tiles alone."""
    line_store(tmp_path, monkeypatch, "line",
               [("EPSG:32636", [REGION], full(36)), ("EPSG:32637", [EAST, FAR], full(37))])


def test_warns_when_the_other_zone_holds_ground_the_mosaic_lacks(zone_line):
    with pytest.warns(UserWarning, match="leaves out ground"):
        arr, _, crs = reader.mosaic("line", REGION, "b", "2026-06-01")
    assert crs == "EPSG:32637" and set(np.unique(arr).tolist()) <= {0, 37}


def test_silent_when_the_other_zone_is_redundant(zone_line, recwarn):
    """Both zones hold tiles over EAST; the zone 36 ones add nothing there."""
    assert reader.tiles_for_bbox("line", EAST, "b") != []
    arr, _, crs = reader.mosaic("line", EAST, "b", "2026-06-01")
    assert crs == "EPSG:32637" and (arr == 37).all()
    assert reader.clip("line", box(*EAST), "b", "2026-06-01")[2] == "EPSG:32637"
    assert len(recwarn) == 0


def test_clip_judges_the_polygon_not_its_bbox(zone_line, recwarn):
    """The two parts lie in zone 37 tiles; only the ground between them, inside the bbox
    of the polygon but outside the polygon, is in zone 36 tiles alone."""
    from shapely.geometry import MultiPolygon
    parts = MultiPolygon([box(*EAST), box(*FAR)])
    arr, _, crs = reader.clip("line", parts, "b", "2026-06-01")
    assert crs == "EPSG:32637" and (arr == 37).sum() > 0
    assert len(recwarn) == 0
    with pytest.warns(UserWarning, match="leaves out ground"):
        reader.mosaic("line", parts.bounds, "b", "2026-06-01")


def west_edge_of_zone_37(lat):
    """Longitude where the zone 37 tiles over EAST end in the west, at one latitude."""
    rows = [r for r in reader.tiles_for_bbox("line", EAST, "b") if r["crs"] == "EPSG:32637"]
    x = min(reader.read_tile(r)[1]["transform"].c for r in rows)
    y = Transformer.from_crs("EPSG:4326", "EPSG:32637", always_xy=True).transform(36.03, lat)[1]
    return Transformer.from_crs("EPSG:32637", "EPSG:4326", always_xy=True).transform(x, y)[0]


def test_clip_warns_for_a_polygon_on_left_out_ground(zone_line):
    with pytest.warns(UserWarning, match="leaves out ground"):
        arr, _, crs = reader.clip("line", box(35.96, 37.00, 36.05, 37.01), "b", "2026-06-01")
    assert crs == "EPSG:32637"


def test_warns_for_a_parcel_sized_request(zone_line):
    """A 270 m parcel across the west edge of the zone 37 tiles: far smaller than a tile."""
    edge = west_edge_of_zone_37(37.005)
    assert edge > 36.003                           # the parcel centre stays in zone 37
    parcel = box(edge - 0.0013, 37.004, edge + 0.0017, 37.006)
    with pytest.warns(UserWarning, match="leaves out ground"):
        arr, _, crs = reader.clip("line", parcel, "b", "2026-06-01", masked=True)
    assert crs == "EPSG:32637" and 0 < arr.count()
    with pytest.warns(UserWarning, match="leaves out ground"):
        reader.mosaic("line", parcel.bounds, "b", "2026-06-01")


def test_warns_when_the_kept_tiles_hold_nodata_where_the_other_zone_has_data(tmp_path, monkeypatch):
    """Both zones store tiles over EAST, but zone 37 has no observation west of 36.04 E."""
    line_store(tmp_path, monkeypatch, "half",
               [("EPSG:32636", [EAST], full(36)),
                ("EPSG:32637", [EAST], lambda lon: np.where(lon < 36.04, 0, 37).astype(np.uint16))])
    with pytest.warns(UserWarning, match="leaves out ground"):
        arr, _, crs = reader.mosaic("half", EAST, "b", "2026-06-01")
    assert crs == "EPSG:32637" and (arr == 0).any()


def test_silent_when_the_other_zone_holds_no_observation(tmp_path, monkeypatch, recwarn):
    """Zone 36 tiles lie over ground the mosaic lacks, but they are empty there."""
    line_store(tmp_path, monkeypatch, "empty",
               [("EPSG:32636", [REGION], lambda lon: np.where(lon < 36.03, 0, 36).astype(np.uint16)),
                ("EPSG:32637", [EAST], full(37))])
    wide = (36.005, 37.000, 36.05, 37.010)         # reaches west of the zone 37 tiles
    arr, _, crs = reader.mosaic("empty", wide, "b", "2026-06-01")
    assert crs == "EPSG:32637" and len(recwarn) == 0


def test_silent_at_the_edge_of_a_swath_stored_in_both_zones(tmp_path, monkeypatch, recwarn):
    """The same swath, ending at 36.04 E, resampled to both CRS: along its edge the two
    hold different pixels, a pixel apart, which is not ground left out."""
    swath = lambda val: lambda lon: np.where(lon < 36.04, val, 0).astype(np.uint16)
    line_store(tmp_path, monkeypatch, "swath",
               [("EPSG:32636", [EAST], swath(36)), ("EPSG:32637", [EAST], swath(37))])
    parcel = box(36.0385, 37.004, 36.0415, 37.006)
    arr, _, crs = reader.clip("swath", parcel, "b", "2026-06-01", masked=True)
    assert crs == "EPSG:32637" and 0 < arr.count() < arr.size
    reader.mosaic("swath", (36.031, 37.001, 36.049, 37.009), "b", "2026-06-01")
    assert len(recwarn) == 0


def test_prefers_the_mgrs_zone_of_the_request(tmp_path, monkeypatch, recwarn):
    """South-west Norway: MGRS assigns 3 to 6 E to zone 32, the regular zone there is 31."""
    area = (3.20, 60.00, 3.30, 60.02)
    line_store(tmp_path, monkeypatch, "norway",
               [("EPSG:32631", [(3.20, 60.00, 3.22, 60.02)], full(31)), ("EPSG:32632", [area], full(32))])
    arr, _, crs = reader.mosaic("norway", area, "b", "2026-06-01")
    assert crs == "EPSG:32632" and (arr[arr != 0] == 32).all() and len(recwarn) == 0


def test_series_gives_one_record_per_crs(zone_line):
    recs = reader.series("line", 36.04, 37.005, "b")
    assert sorted((r["date"], r["crs"], r["value"]) for r in recs) == [
        ("2026-06-01", "EPSG:32636", 36), ("2026-06-01", "EPSG:32637", 37)]


def test_series_skips_a_nan_nodata_pixel(vault):
    lon, lat = TO_WGS.transform(X0 + 35, Y1 - 35)          # pixel (3, 3) of tile (0, 0)
    assert reader.series("edge", lon, lat, "nan") == []
    lon, lat = TO_WGS.transform(X0 + 55, Y1 - 35)
    assert [r["value"] for r in reader.series("edge", lon, lat, "nan")] == [2.0]


def test_warns_for_a_small_part_of_a_multipolygon(zone_line):
    """Three small parcels far apart: one over FAR and one over EAST, on zone 37 tiles, and
    one between them on ground only zone 36 holds. A 32 x 32 lattice over their common
    bbox has no point in the middle parcel, which sits on a corner between four of them."""
    from shapely.geometry import MultiPolygon
    w, s, e, n = 35.951, 37.001, 36.049, 37.039
    kept = [box(w, n - 0.0008, w + 0.001, n), box(e - 0.001, s, e, s + 0.0008)]
    x, y = w + 14 * (e - w) / 32, s + 8 * (n - s) / 32
    lost = box(x - 0.0004, y - 0.0003, x + 0.0004, y + 0.0003)
    assert {r["crs"] for r in reader.tiles_for_bbox("line", lost.bounds, "b")} == {"EPSG:32636"}
    with pytest.warns(UserWarning, match="leaves out ground"):
        arr, _, crs = reader.clip("line", MultiPolygon(kept + [lost]), "b", "2026-06-01")
    assert crs == "EPSG:32637"


def test_warns_for_a_thin_polygon(zone_line):
    """An L of two arms a few metres wide, one of them on ground only zone 36 holds: no
    point of a 32 x 32 lattice over its bbox falls inside it."""
    from shapely.geometry import Polygon
    edge = west_edge_of_zone_37(37.005)
    w, e = edge - 0.006, edge + 0.006
    thin = Polygon([(w, 37.0050), (e, 37.0050), (e, 37.0100), (e - 0.0001, 37.0100),
                    (e - 0.0001, 37.00506), (w, 37.00506)])
    with pytest.warns(UserWarning, match="leaves out ground"):
        reader.clip("line", thin, "b", "2026-06-01")


def test_series_does_not_read_the_tile_next_to_the_point(zone_line):
    """A point 3 m west of the zone 37 tiles lies in none of them, but inside the WGS84
    envelope of the nearest one: UTM tiles are rotated in longitude and latitude."""
    edge = west_edge_of_zone_37(37.005)
    west = reader.series("line", edge - 0.00003, 37.005, "b")
    assert [(r["crs"], r["value"]) for r in west] == [("EPSG:32636", 36)]
    east = reader.series("line", edge + 0.00003, 37.005, "b")
    assert sorted(r["crs"] for r in east) == ["EPSG:32636", "EPSG:32637"]
