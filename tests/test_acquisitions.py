"""Offline tests for acquisitions in the tile key.

Two overpasses on one day (Sentinel-1 morning and evening, Sentinel-2A and 2B) are two
tiles, not one. Two scenes of one overpass that each hold part of a tile are merged into
one tile."""

import io
import os
from pathlib import Path

import duckdb
import numpy as np
import pytest
import rasterio
from rasterio.transform import Affine

from geovault import catalog, reader, store
from geovault.sources.common import encode_blob

CRS = "EPSG:32636"
RES, PX = 10, 16
X0, Y1 = 300000.0, 4100000.0
DATE = "2026-03-01"
BOUNDS = (33.0, 37.0, 33.01, 37.01)          # a rough WGS84 box around the test tiles


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE", tmp_path / "store")
    monkeypatch.setattr(catalog, "STORE", tmp_path / "store")
    monkeypatch.setattr(catalog, "CATALOG", tmp_path / "catalog")
    return tmp_path


def tile(value, cols=slice(None), dtype=np.uint16, nodata=0):
    """A PX x PX tile holding `value` in the given columns and nodata elsewhere."""
    arr = np.full((PX, PX), nodata, dtype)
    arr[:, cols] = value
    return arr


def add(w, arr, scene, acq, x=0, band="b", date=DATE, cloud=None, nodata=0):
    blob = encode_blob(arr, CRS, Affine(RES, 0, X0 + x * RES * PX, 0, -RES, Y1), nodata)
    valid = int((arr != nodata).sum())
    return w.add(band, date, x, 0, CRS, RES, PX, BOUNDS, nodata, cloud, scene, blob, valid, acq=acq)


def stored(dataset="d"):
    """Every stored row of the dataset as dicts with the decoded array, in key order."""
    files = sorted((store.STORE / f"dataset={dataset}").rglob("*.parquet"))
    con = duckdb.connect()
    cur = con.execute(f"SELECT * FROM {store.scan(files)} ORDER BY band, date, acq, x, scene_id")
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    con.close()
    for r in rows:
        with rasterio.MemoryFile(io.BytesIO(bytes(r["data"]))) as mem, mem.open() as ds:
            r["arr"] = ds.read(1)
    return rows


WEST, EAST = slice(0, 10), slice(6, PX)       # two halves overlapping in columns 6..9


def test_two_scenes_of_one_acquisition_are_merged_into_one_tile(vault):
    w = store.Writer("d")
    assert add(w, tile(100, WEST), "scene_a", "orbit_1", cloud=10.0)
    assert add(w, tile(200, EAST), "scene_b", "orbit_1", cloud=40.0)
    w.close()
    rows = stored()
    assert len(rows) == 1
    r = rows[0]
    assert (r["arr"] != 0).all() and r["valid_px"] == PX * PX
    assert r["scene_id"] == "scene_a|scene_b" and r["acq"] == "orbit_1"
    # the fuller scene (10 columns) is kept where both hold data, the other fills the rest
    assert (r["arr"][:, :10] == 100).all() and (r["arr"][:, 10:] == 200).all()
    # cloud_pct: mean over the scenes, weighted by the pixels each one contributes
    assert r["cloud_pct"] == pytest.approx((10.0 * 10 + 40.0 * 6) / 16)
    assert catalog.rebuild("d") == 1


def test_arrival_order_does_not_change_the_merged_tile(vault):
    blobs = []
    for order in (("scene_a", "scene_b"), ("scene_b", "scene_a")):
        ds = "o_" + order[0]
        w = store.Writer(ds)
        for scene in order:
            add(w, tile(100, WEST) if scene == "scene_a" else tile(200, EAST), scene, "orbit_1")
        w.close()
        blobs.append([bytes(r["data"]) for r in stored(ds)])
    assert len(blobs[0]) == 1 and blobs[0] == blobs[1]


def test_equal_halves_take_the_smaller_scene_id_first(vault):
    w = store.Writer("d")
    add(w, tile(200, slice(4, PX)), "scene_b", "orbit_1")
    add(w, tile(100, slice(0, 12)), "scene_a", "orbit_1")
    w.close()
    (r,) = stored()
    assert (r["arr"][:, :12] == 100).all() and (r["arr"][:, 12:] == 200).all()


def test_two_acquisitions_on_one_day_are_two_tiles(vault):
    w = store.Writer("d")
    assert add(w, tile(100), "morning", "orbit_1")
    assert add(w, tile(200), "evening", "orbit_2")
    w.close()
    store.compact("d")
    rows = stored()
    assert [(r["acq"], r["scene_id"], int(r["arr"][0, 0])) for r in rows] == [
        ("orbit_1", "morning", 100), ("orbit_2", "evening", 200)]


def test_parts_from_separate_runs_are_merged(vault):
    """An interrupted or extended ingest adds the second scene in a later run."""
    w = store.Writer("d")
    add(w, tile(100, WEST), "scene_a", "orbit_1")
    w.close()
    assert stored()[0]["valid_px"] == 10 * PX
    w = store.Writer("d")
    add(w, tile(200, EAST), "scene_b", "orbit_1")
    w.close()
    (r,) = stored()
    assert r["valid_px"] == PX * PX and r["scene_id"] == "scene_a|scene_b"


def test_a_part_left_beside_its_merged_tile_changes_nothing(vault):
    """A part another program holds open cannot be deleted by the compaction that merged
    it: the merged tile then sits next to one of its own scenes until the next one."""
    w = store.Writer("d")
    add(w, tile(100, WEST), "scene_a", "orbit_1", cloud=10.0)
    w.flush()
    add(w, tile(200, EAST), "scene_b", "orbit_1", cloud=30.0)
    w.flush()
    held = store.month_files("d", RES, DATE)[1]
    with open(held, "rb"):
        store.compact_month("d", RES, DATE)
        (merged,) = [r for r in stored() if "|" in r["scene_id"]]
        assert merged["valid_px"] == PX * PX
    store.compact_month("d", RES, DATE)
    (r,) = stored()
    assert (r["scene_id"], r["valid_px"], r["cloud_pct"]) == (
        "scene_a|scene_b", PX * PX, merged["cloud_pct"])
    assert (r["arr"] == merged["arr"]).all()
    assert store.open_months("d") == []


def test_a_third_scene_fills_what_is_left(vault):
    w = store.Writer("d")
    add(w, tile(100, slice(0, 6)), "scene_a", "orbit_1")
    add(w, tile(200, slice(4, 12)), "scene_b", "orbit_1")
    add(w, tile(300, slice(10, PX)), "scene_c", "orbit_1")
    w.close()
    (r,) = stored()
    assert r["scene_id"] == "scene_a|scene_b|scene_c" and (r["arr"] != 0).all()
    # fullest first: b (8 columns), then a and c (6 each) by scene id
    assert (r["arr"][:, 4:12] == 200).all()
    assert (r["arr"][:, :4] == 100).all() and (r["arr"][:, 12:] == 300).all()


def test_the_same_scene_twice_is_one_contribution(vault):
    w = store.Writer("d")
    assert add(w, tile(100, WEST), "scene_a", "orbit_1")
    assert add(w, tile(100, WEST), "scene_a", "orbit_1") is False
    w.close()
    (r,) = stored()
    assert r["scene_id"] == "scene_a" and r["valid_px"] == 10 * PX


def test_float_tiles_with_nan_nodata_merge(vault):
    nan = float("nan")
    a = np.full((PX, PX), nan, np.float32)
    a[:, :10] = 1.5
    b = np.full((PX, PX), nan, np.float32)
    b[:, 6:] = 2.5
    w = store.Writer("d")
    for arr, scene in ((a, "scene_a"), (b, "scene_b")):
        blob = encode_blob(arr, CRS, Affine(RES, 0, X0, 0, -RES, Y1), nan)
        w.add("b", DATE, 0, 0, CRS, RES, PX, BOUNDS, nan, None, scene, blob,
              int((~np.isnan(arr)).sum()), acq="orbit_1")
    w.close()
    (r,) = stored()
    assert not np.isnan(r["arr"]).any() and r["cloud_pct"] is None
    assert (r["arr"][:, :10] == 1.5).all() and (r["arr"][:, 10:] == 2.5).all()


def test_tiles_without_an_acquisition_keep_the_fuller_one(vault):
    """Sources without acquisitions (static layers, daily grids) are not merged: the tile
    with more valid pixels replaces the other, as before."""
    w = store.Writer("d")
    assert add(w, tile(100, WEST), "file_a", None)
    assert add(w, tile(200, EAST), "file_b", None) is False
    assert add(w, tile(300), "file_c", None) is False
    w.close()
    (r,) = stored()
    assert r["scene_id"] == "file_c" and (r["arr"] == 300).all() and r["acq"] is None


def test_reencode_merges_too(vault):
    w = store.Writer("d")
    add(w, tile(100, WEST), "scene_a", "orbit_1")
    w.flush()
    add(w, tile(200, EAST), "scene_b", "orbit_1")
    w.flush()
    assert len(stored()) == 2
    n, _, _ = store.reencode_month("d", RES, DATE[:7] + "-01")
    (r,) = stored()
    assert n == 1 and r["valid_px"] == PX * PX


def test_close_without_merge_leaves_the_rows_to_the_next_close(vault):
    w = store.Writer("d")
    add(w, tile(100, WEST), "scene_a", "orbit_1")
    add(w, tile(200, EAST), "scene_b", "orbit_1")
    w.close(merge=False)
    assert len(stored()) == 2
    w.close()                                      # nothing new to flush, but the month is owed
    (r,) = stored()
    assert r["valid_px"] == PX * PX


def test_tiles_that_cannot_be_merged_do_not_block_the_month(vault):
    """Two rows under one key with tiles of different size: kept as they are, and the
    other tiles of the month are still merged and compacted."""
    w = store.Writer("d")
    add(w, tile(100, WEST), "scene_a", "orbit_1", x=0)
    big = np.full((2 * PX, 2 * PX), 7, np.uint16)
    blob = encode_blob(big, CRS, Affine(RES, 0, X0, 0, -RES, Y1), 0)
    w.add("b", DATE, 0, 0, CRS, RES, PX, BOUNDS, 0, None, "scene_b", blob, big.size, acq="orbit_1")
    add(w, tile(100, WEST), "scene_a", "orbit_1", x=1)
    add(w, tile(200, EAST), "scene_b", "orbit_1", x=1)
    w.close()
    store.compact("d")
    rows = stored()
    assert [(r["x"], r["scene_id"]) for r in rows] == [
        (0, "scene_a"), (0, "scene_b"), (1, "scene_a|scene_b")]
    assert len(store.month_files("d", RES, DATE[:7] + "-01")) == 1


def test_compact_reports_only_what_it_sealed(vault):
    w = store.Writer("d")
    add(w, tile(100), "scene_a", "orbit_1")
    w.flush()
    assert store.open_months("d") == [(float(RES), DATE[:7] + "-01")]
    lock = store.lock_file("d", RES, DATE)
    held = store._MonthLock(lock, wait=False)
    assert held.acquire()                          # as another compaction holds the month
    monkey = store._MonthLock.__init__.__defaults__
    store._MonthLock.__init__.__defaults__ = (0.2,)
    try:
        assert store.compact("d") == []
        assert store.open_months("d") != []
        lines = []
        from geovault import cli
        cli._report_open("d", lines.append)
        assert "2026-03 r10" in lines[0] and len(lines) == 2
        assert "another geovault process" in lines[1] and str(lock) in lines[1]
        assert lock.read_text() == str(os.getpid())
    finally:
        store._MonthLock.__init__.__defaults__ = monkey
        held.release()
    assert store.compact("d") == [(float(RES), DATE[:7] + "-01")] and store.open_months("d") == []
    lines = []
    cli._report_open("d", lines.append)
    assert lines == []


# --- what a scene still has to fetch ----------------------------------------------------------

from types import SimpleNamespace

from geovault.grid import Grid
from geovault.sources import common, landsat, s1_pc, s2_earthsearch

AREA = (36.30, 37.00, 36.34, 37.03)            # a few 10 m tiles in zone 37
EVERYWHERE = lambda bounds: True


def plan(stored, acq="orbit_1", scene="scene_a", extent=None, grid=None):
    grid = grid or Grid("EPSG:32637", 10, 256)
    return common.plan_scene_tiles(grid, AREA, "EPSG:32637", "b", DATE, stored, acq, scene,
                                   True, EVERYWHERE, extent=extent)


def test_a_scene_plans_what_it_has_not_given_yet():
    tiles = plan({})
    assert len(tiles) >= 4
    full, partial, rest = tiles[0], tiles[1], tiles[2:]
    stored = {("b", DATE, "orbit_1", "EPSG:32637", *full): None,                    # no empty pixel
              ("b", DATE, "orbit_1", "EPSG:32637", *partial): frozenset({"scene_a"})}
    assert plan(stored, scene="scene_a") == rest             # nothing more to give
    assert plan(stored, scene="scene_b") == [partial] + rest  # may hold the missing part
    assert plan(stored, acq="orbit_2") == tiles               # another overpass: all of it


def test_a_scene_plans_only_tiles_inside_its_raster():
    grid = Grid("EPSG:32637", 10, 256)
    tiles = plan({})
    w, s, e, n = grid.tile_bounds(*tiles[0])
    assert plan({}, extent=(w + 10, s + 10, e - 10, n - 10)) == [tiles[0]]
    assert plan({}, extent=(w - 1e6, s, w - 5e5, n)) == []


def test_scene_extent_from_stac_fields():
    on_asset = SimpleNamespace(properties={}, assets={"red": SimpleNamespace(extra_fields={
        "proj:shape": [10980, 10980], "proj:transform": [10, 0, 199980, 0, -10, 4200000]})})
    assert common.scene_extent(on_asset, "red") == (199980, 4090200, 309780, 4200000)
    on_item = SimpleNamespace(assets={"vv": SimpleNamespace(extra_fields={})}, properties={
        "proj:shape": [20780, 28534],
        "proj:transform": [10.0, 0.0, 531740.0, 0.0, -10.0, 4146090.0, 0.0, 0.0, 1.0]})
    assert common.scene_extent(on_item, "vv") == (531740.0, 3938290.0, 817080.0, 4146090.0)
    assert common.scene_extent(SimpleNamespace(assets={}, properties={}), "vv") is None


def test_acquisition_names():
    def item(**props):
        return SimpleNamespace(properties=props)
    # the processing baseline is not part of the overpass
    assert s2_earthsearch.acquisition(item(**{"s2:datatake_id": "GS2C_20260305T081821_007813_N05.12"})) \
        == "GS2C_20260305T081821_007813"
    assert s1_pc.acquisition(item(**{"platform": "sentinel-1a", "sat:absolute_orbit": 63436})) \
        == "sentinel-1a_63436"
    # the catalogue spells the satellite in two ways, also within one orbit
    assert s1_pc.acquisition(item(**{"platform": "SENTINEL-1A", "sat:absolute_orbit": 57253})) \
        == s1_pc.acquisition(item(**{"platform": "sentinel-1a", "sat:absolute_orbit": 57253}))
    assert landsat.acquisition(item(**{"platform": "landsat-9", "landsat:wrs_path": "175"})) \
        == "landsat-9_175"
    assert landsat.acquisition(item(**{"platform": "LANDSAT-9", "landsat:wrs_path": "175"})) \
        == "landsat-9_175"
    with pytest.raises(ValueError, match="s2:datatake_id"):
        s2_earthsearch.acquisition(item(platform="sentinel-2a"))


def stac_item(crs, assets, **props):
    return SimpleNamespace(
        id="scene_a", assets={a: SimpleNamespace(extra_fields={}) for a in assets},
        properties={"datetime": DATE + "T08:00:00Z", "proj:code": crs, **props})


@pytest.mark.parametrize("src,asset,band,props", [
    (s2_earthsearch, "red", "B4", {"s2:datatake_id": "GS2C_20260301T081821_007813_N05.12"}),
    (s1_pc, "vv", "VV", {"platform": "sentinel-1a", "sat:absolute_orbit": 63436}),
    (landsat, "red", "red", {"platform": "landsat-9", "landsat:wrs_path": "175"}),
], ids=["s2", "s1", "landsat"])
def test_plan_scene_skips_by_acquisition_and_scene(src, asset, band, props):
    item = stac_item("EPSG:32637", [asset], **props)
    acq = src.acquisition(item)
    n = src.plan_scene(item, AREA, bands=[band])[band]
    grid = landsat.grid_for("EPSG:32637") if src is landsat else Grid("EPSG:32637", 10, 256)
    tx, ty = common.plan_scene_tiles(grid, AREA, "EPSG:32637", band, DATE, {}, acq, "scene_a",
                                     True, EVERYWHERE)[0]
    key = (band, DATE, acq, "EPSG:32637", tx, ty)
    assert src.plan_scene(item, AREA, bands=[band], skip_keys={key: None}).get(band, 0) == n - 1
    assert src.plan_scene(item, AREA, bands=[band],
                          skip_keys={key: frozenset({"scene_a"})}).get(band, 0) == n - 1
    assert src.plan_scene(item, AREA, bands=[band],
                          skip_keys={key: frozenset({"other"})})[band] == n
    other_day = (band, "2026-03-02", acq, "EPSG:32637", tx, ty)
    assert src.plan_scene(item, AREA, bands=[band], skip_keys={other_day: None})[band] == n


def test_scene_extent_of_a_rotated_raster_is_unknown():
    item = SimpleNamespace(assets={}, properties={
        "proj:shape": [100, 100], "proj:transform": [10, 2, 500000, 2, -10, 4100000]})
    assert common.scene_extent(item) is None


def test_the_cloud_mask_of_a_scene_is_its_own(vault):
    """Stored mask tiles are used for a scene only when they hold that scene; rows not
    merged yet are combined as the merge would, the fullest first."""
    w = store.Writer("d")
    add(w, tile(3, slice(0, 12)), "scene_a", "orbit_1", x=0, band="SCL")
    add(w, tile(9, slice(4, PX)), "scene_b", "orbit_1", x=0, band="SCL")
    add(w, tile(3), "scene_a", "orbit_1", x=1, band="SCL")
    w.close(merge=False)
    for merged in (False, True):
        if merged:
            w.close()
        got = common.stored_tiles("d", RES, "SCL", DATE, CRS, "orbit_1", "scene_b")
        assert list(got) == [(0, 0)]                             # tile 1 holds scene_a only
        assert (got[(0, 0)][:, :12] == 3).all() and (got[(0, 0)][:, 12:] == 9).all()
        assert set(common.stored_tiles("d", RES, "SCL", DATE, CRS, "orbit_1", "scene_a")) == {
            (0, 0), (1, 0)}
        assert common.stored_tiles("d", RES, "SCL", DATE, CRS, "orbit_2", "scene_a") == {}


def test_existing_scenes_tells_full_tiles_from_partial_ones(vault):
    w = store.Writer("d")
    add(w, tile(100), "scene_a", "orbit_1", x=0)
    add(w, tile(100, WEST), "scene_a", "orbit_1", x=1)
    add(w, tile(200, slice(12, PX)), "scene_b", "orbit_1", x=1)
    w.close()
    assert w.existing_scenes(RES, [DATE[:7]]) == {
        ("b", DATE, "orbit_1", CRS, 0, 0): None,
        ("b", DATE, "orbit_1", CRS, 1, 0): frozenset({"scene_a", "scene_b"})}


# --- reading a date that holds two acquisitions ------------------------------------------------

from pyproj import Transformer
from shapely.geometry import box

TO_WGS = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)
SIZE = RES * PX


def wgs(w, s, e, n):
    xs, ys = TO_WGS.transform([w, e, w, e], [s, s, n, n])
    return min(xs), min(ys), max(xs), max(ys)


TILE = wgs(X0 + 1, Y1 - SIZE + 1, X0 + SIZE - 1, Y1 - 1)              # inside tile (0, 0)
EAST_HALF = wgs(X0 + 110, Y1 - SIZE + 1, X0 + SIZE - 1, Y1 - 1)        # its columns 11..15


def geo_store(layers, dataset="g", merge=True):
    """layers: (array, scene, acq, date[, cloud_pct]). All on tile (0, 0), with true
    WGS84 bounds."""
    w = store.Writer(dataset)
    for arr, scene, acq, date, *cloud in layers:
        blob = encode_blob(arr, CRS, Affine(RES, 0, X0, 0, -RES, Y1), 0)
        w.add("b", date, 0, 0, CRS, RES, PX, wgs(X0, Y1 - SIZE, X0 + SIZE, Y1), 0,
              cloud[0] if cloud else 0.0, scene, blob, int((arr != 0).sum()), acq=acq)
    w.close(merge=merge)
    catalog.rebuild(dataset)


@pytest.fixture
def two_passes(vault):
    """A morning pass over the whole tile and an evening pass over its eastern 10 columns.
    The evening pass is the clearer one, so a mosaic that blended the two (cleanest tile
    first) would show it in the east."""
    geo_store([(tile(100), "morning", "orbit_1", DATE, 50.0),
               (tile(200, slice(6, PX)), "evening", "orbit_2", DATE, 0.0)])


def test_acquisitions_lists_the_passes_of_a_date(two_passes):
    assert reader.acquisitions("g", TILE, "b", DATE) == ["orbit_1", "orbit_2"]
    assert reader.acquisitions("g", TILE, "b", "2026-03-02") == []


def test_read_tile_reads_the_acquisition_of_its_row(two_passes):
    rows = reader.tiles_for_bbox("g", TILE, "b", DATE)
    assert [(r["acq"], int(reader.read_tile(r)[0][0, 15])) for r in rows] == [
        ("orbit_1", 100), ("orbit_2", 200)]


def test_read_tile_matches_a_row_whose_scene_is_not_named(vault):
    geo_store([(tile(100), None, "orbit_1", DATE)])
    (row,) = reader.tiles_for_bbox("g", TILE, "b", DATE)
    assert row["scene_id"] is None and (reader.read_tile(row)[0] == 100).all()
    assert (reader.mosaic("g", TILE, "b", DATE)[0] == 100).all()


def test_mosaic_takes_one_acquisition_and_says_so(two_passes):
    with pytest.warns(UserWarning, match="2 acquisitions") as rec:
        arr, _, _ = reader.mosaic("g", TILE, "b", DATE)
    assert (arr == 100).all()                      # the pass covering most of the request
    assert len(rec) == 1 and Path(rec[0].filename).samefile(__file__)     # points at the caller
    assert "orbit_1" in str(rec[0].message) and "orbit_2" in str(rec[0].message)
    with pytest.warns(UserWarning, match="2 acquisitions"):
        arr, _, _ = reader.clip("g", box(*TILE), "b", DATE, masked=True)
    assert set(np.unique(arr.compressed()).tolist()) == {100}


def test_mosaic_of_a_named_acquisition(two_passes, recwarn):
    arr, _, _ = reader.mosaic("g", TILE, "b", DATE, acq="orbit_2")
    assert (arr[:, 6:] == 200).all() and (arr[:, :6] == 0).all()      # not filled from the other pass
    arr, _, _ = reader.clip("g", box(*TILE), "b", DATE, acq="orbit_1")
    assert (arr == 100).all()
    assert reader.mosaic("g", TILE, "b", DATE, acq="orbit_9") is None
    assert len(recwarn) == 0


def test_the_acquisition_is_chosen_by_what_it_covers_of_the_request(vault):
    """Morning holds columns 0..11, evening columns 10..15: the tile as a whole is mostly
    morning, the eastern end of it is all evening."""
    geo_store([(tile(100, slice(0, 12)), "morning", "orbit_1", DATE),
               (tile(200, slice(10, PX)), "evening", "orbit_2", DATE)])
    with pytest.warns(UserWarning, match="2 acquisitions"):
        arr, _, _ = reader.mosaic("g", EAST_HALF, "b", DATE)
    assert (arr[:, 11:] == 200).all()
    with pytest.warns(UserWarning, match="2 acquisitions"):
        arr, _, _ = reader.mosaic("g", TILE, "b", DATE)
    assert (arr[:, :12] == 100).all() and (arr[:, 12:] == 0).all()


def test_equal_coverage_takes_the_first_acquisition_by_name(vault):
    geo_store([(tile(200), "evening", "orbit_2", DATE), (tile(100), "morning", "orbit_1", DATE)])
    with pytest.warns(UserWarning, match="2 acquisitions"):
        arr, _, _ = reader.mosaic("g", TILE, "b", DATE)
    assert (arr == 100).all()


def test_one_acquisition_reads_as_before(vault, recwarn):
    geo_store([(tile(100), "morning", "orbit_1", DATE), (tile(300), "next", "orbit_3", "2026-03-02")])
    arr, _, _ = reader.mosaic("g", TILE, "b", DATE)
    assert (arr == 100).all() and len(recwarn) == 0
    # a date prefix is a composite over dates and acquisitions, as it always was
    arr, _, _ = reader.mosaic("g", TILE, "b", "2026-03")
    assert (arr == 100).all() and len(recwarn) == 0


def test_a_date_prefix_stays_a_composite(two_passes, recwarn):
    arr, _, _ = reader.mosaic("g", TILE, "b", "2026-03")
    assert (arr[:, 6:] == 200).all() and (arr[:, :6] == 100).all()   # the clearer pass first
    arr, _, _ = reader.mosaic("g", TILE, "b", "2026-03", acq="orbit_1")
    assert (arr == 100).all() and len(recwarn) == 0


def test_an_acquisition_without_an_observation_on_the_request_does_not_count(two_passes, recwarn):
    """The evening pass has a tile over the request but no pixel on its western end."""
    west_end = wgs(X0 + 1, Y1 - SIZE + 1, X0 + 50, Y1 - 1)       # columns 0..4
    assert reader.acquisitions("g", west_end, "b", DATE) == ["orbit_1", "orbit_2"]
    arr, _, _ = reader.mosaic("g", west_end, "b", DATE)
    assert (arr == 100).all() and len(recwarn) == 0


def test_scenes_not_merged_yet_are_read_one_by_one(vault, recwarn):
    """After an interrupted run a tile of one acquisition is still two rows in one file."""
    geo_store([(tile(100, WEST), "scene_a", "orbit_1", DATE),
               (tile(200, EAST), "scene_b", "orbit_1", DATE)], merge=False)
    rows = reader.tiles_for_bbox("g", TILE, "b", DATE)
    assert [(r["scene_id"], int(reader.read_tile(r)[0][0, 0]), int(reader.read_tile(r)[0][0, 15]))
            for r in rows] == [("scene_a", 100, 0), ("scene_b", 0, 200)]
    arr, _, _ = reader.mosaic("g", TILE, "b", DATE)
    assert (arr != 0).all() and len(recwarn) == 0
    lon, lat = TO_WGS.transform(X0 + 5, Y1 - 5)                  # column 0: scene_a only
    assert [(r["scene_id"], r["value"]) for r in reader.series("g", lon, lat, "b")] == [
        ("scene_a", 100)]


def test_series_gives_one_record_per_acquisition(two_passes):
    lon, lat = TO_WGS.transform(X0 + 155, Y1 - 5)             # column 15: both passes
    recs = reader.series("g", lon, lat, "b")
    assert [(r["date"], r["acq"], r["value"]) for r in recs] == [
        (DATE, "orbit_1", 100), (DATE, "orbit_2", 200)]              # by date, then acquisition
    lon, lat = TO_WGS.transform(X0 + 5, Y1 - 5)               # column 0: the morning pass only
    assert [(r["acq"], r["value"]) for r in reader.series("g", lon, lat, "b")] == [("orbit_1", 100)]


def test_a_month_another_process_holds_stays_owed_a_merge(vault):
    w = store.Writer("d")
    add(w, tile(100, WEST), "scene_a", "orbit_1")
    add(w, tile(200, EAST), "scene_b", "orbit_1")
    lock = store.lock_file("d", RES, DATE)
    lock.parent.mkdir(parents=True)
    held = store._MonthLock(lock, wait=False)
    assert held.acquire()
    try:
        w.close()                                  # cannot merge now
        assert len(stored()) == 2
    finally:
        held.release()
    w.close()                                      # nothing new, but the merge is still owed
    assert len(stored()) == 1
