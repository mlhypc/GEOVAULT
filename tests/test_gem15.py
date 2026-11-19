"""Offline tests for the gem15 adapter, on files written here as the product lays them out:
one raster per cell in the UTM zone of the cell, padded with NaN, holding values about a
kilometre past the cell, where neighbouring cells do not agree. A tile takes every pixel
from the cell its centre lies in, of the cells of its own zone, and is stored only when
every cell it needs answered."""

import os

import duckdb
import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import Affine

from rasterio.errors import RasterioIOError

from geovault import blob, catalog, cli, store
from geovault.grid import Grid
from geovault.sources import dem_gem15 as gem
from geovault.store import Writer

PAST = 0.012                    # degrees a cell holds values past its own ground
VALUE = {(38, 34): 100.0, (38, 35): 200.0, (38, 36): 300.0, (39, 34): 400.0, (39, 35): 500.0,
         (0, 37): 600.0, (-1, 37): 700.0}
STEP = {"dsm": 1.0, "dtm": 0.0}     # band k of the DSM file holds the cell's value + k


def zone(lat, lon):
    """The UTM zone the product puts a cell in: the southern zones south of the equator."""
    return f"EPSG:{(32600 if lat >= 0 else 32700) + (lon + 180) // 6 + 1}"


def centres(w, n, cols, rows):
    """Coordinates of the pixel centres of a raster with north-west corner (w, n)."""
    return np.meshgrid(w + (np.arange(cols) + 0.5) * 15.0, n - (np.arange(rows) + 0.5) * 15.0)


def lonlat_of_pixels(crs, w, n, cols, rows):
    return Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform(*centres(w, n, cols, rows))


def ramp(w, n, cols, rows):
    """A value that changes from every pixel to the next, by its place on the ground: a
    window read one pixel off holds other numbers. Exact in float32 on top of VALUE."""
    x, y = centres(w, n, cols, rows)
    return np.floor(x / 15.0) % 64 + np.floor(y / 15.0) % 64 / 64


def write_cell(folder, lat, lon, around, tiles=3, shift=0.0, ramped=False):
    """The two files of one cell: rasters of (2 * tiles + 1) tiles a side around the tile
    that holds the point `around` (lon, lat), valid on the cell and PAST degrees past it.
    ramped: every pixel holds the value of its cell plus ramp()."""
    crs = zone(lat, lon)
    grid = Grid(crs, 15.0, gem.TILE_PX)
    x, y = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(*around)
    tx, ty = grid.tile_of_point(x, y)
    w = grid.tile_bounds(tx - tiles, ty)[0] + shift
    n = grid.tile_bounds(tx, ty + tiles)[3]
    side = (2 * tiles + 1) * gem.TILE_PX
    plon, plat = lonlat_of_pixels(crs, w, n, side, side)
    valid = ((plon >= lon - PAST) & (plon < lon + 1 + PAST)
             & (plat >= lat - PAST) & (plat < lat + 1 + PAST))
    extra = ramp(w, n, side, side) if ramped else 0.0
    for asset, names in gem.ASSETS.items():
        path = folder / f"{gem.cell_id(lat, lon)}_{asset}.tif"
        with rasterio.open(path, "w", driver="GTiff", width=side, height=side, count=len(names),
                           dtype="float32", crs=crs, transform=Affine(15, 0, w, 0, -15, n),
                           nodata=float("nan"), tiled=True, blockxsize=512, blockysize=512,
                           compress="deflate") as dst:
            for k in range(len(names)):
                dst.write(np.where(valid, VALUE[(lat, lon)] + k * STEP[asset] + extra, np.nan)
                          .astype("float32"), k + 1)


@pytest.fixture
def product(tmp_path, monkeypatch):
    """A product of local files. `product.answers[cell id]` overrides what the bucket says
    about a cell (None: no answer); `product.asked` lists the files asked for."""
    monkeypatch.setattr(store, "STORE", tmp_path / "store")
    monkeypatch.setattr(store, "ROOT", tmp_path)
    monkeypatch.setattr(catalog, "STORE", tmp_path / "store")
    monkeypatch.setattr(catalog, "CATALOG", tmp_path / "catalog")
    folder = tmp_path / "cells"
    folder.mkdir()

    class Product:
        answers, asked, composed = {}, [], []

        @staticmethod
        def cell(lat, lon, around, **kw):
            write_cell(folder, lat, lon, around, **kw)

    def url(lat, lon, asset):
        return str(folder / f"{gem.cell_id(lat, lon)}_{asset}.tif")

    def exists(path):
        Product.asked.append(os.path.basename(path))
        cell = os.path.basename(path).split("_")[0]
        return Product.answers[cell] if cell in Product.answers else os.path.exists(path)

    compose = gem._compose

    def counted(owners, grid, tx, ty, count, inside):
        Product.composed.append((grid.crs, tx, ty))
        return compose(owners, grid, tx, ty, count, inside)

    monkeypatch.setattr(gem, "asset_url", url)
    monkeypatch.setattr(gem, "_exists", exists)
    monkeypatch.setattr(gem, "_compose", counted)
    return Product


def ingest(bbox, **kw):
    writer = Writer("gem15")
    lines = []
    skip = writer.existing_keys(15.0, [""])
    n = gem.ingest(bbox, writer, skip_keys=skip, log=lines.append, **kw)
    writer.close()
    ingest.left_out = writer.left_out
    return n, lines


def stored(dataset="gem15"):
    """{(band, crs, x, y): (array, scene id, valid_px)} of everything in the store."""
    files = store.month_files(dataset, 15.0, "")
    if not files:
        return {}
    con = duckdb.connect()
    rows = con.execute(f"SELECT band, crs, x, y, scene_id, valid_px, data FROM {store.scan(files)}").fetchall()
    con.close()
    return {(b, c, x, y): (blob.decode(d)[0], s, v) for b, c, x, y, s, v, d in rows}


def expected(band, crs, x, y, cells, ramped=False):
    """What a tile has to hold: per pixel the value of the cell its centre lies in, when
    that cell is one of `cells` and lies in the tile's UTM zone, else NaN."""
    grid = Grid(crs, 15.0, gem.TILE_PX)
    w, _, _, n = grid.tile_bounds(x, y)
    lon, lat = lonlat_of_pixels(crs, w, n, gem.TILE_PX, gem.TILE_PX)
    asset = "dtm" if band == "dtm" else "dsm"
    k = gem.ASSETS[asset].index(band)
    out = np.full(lon.shape, np.nan, np.float32)
    for (lat0, lon0) in cells:
        if zone(lat0, lon0) == crs:
            own = (np.floor(lon) == lon0) & (np.floor(lat) == lat0)
            out[own] = VALUE[(lat0, lon0)] + k * STEP[asset]
    if ramped:
        out += ramp(w, n, gem.TILE_PX, gem.TILE_PX).astype(np.float32)
    return out


def assert_tiles_hold_their_cells(tiles, cells, ramped=False):
    assert tiles
    for (band, crs, x, y), (arr, _, valid) in tiles.items():
        want = expected(band, crs, x, y, cells, ramped)
        assert np.array_equal(arr, want, equal_nan=True), (band, crs, x, y)
        assert valid == int(np.isfinite(want).sum()) > 0


LINE = (35.0, 38.5)                             # on the line between w34s38 and w35s38
ACROSS = (34.96, 38.49, 35.03, 38.51)           # a bbox across that line


def test_names_of_cells_and_folders():
    assert gem.cell_id(38, 35) == "w35s38" and gem.cell_id(-11, -41) == "wm41sm11"
    assert gem.cell_id(0, 0) == "w0s0"
    assert [gem.group_id(*c) for c in ((38, 35), (-11, -41), (5, -3), (-1, 0), (9, 179), (-20, -50))] == [
        "p30_p30", "m20_m50", "p0_m10", "m10_p0", "p0_p170", "m20_m50"]
    assert gem.asset_url(38, 35, "dtm").endswith("/gem-15/p30_p30/w35s38/dtm/zall/mosaic.tif")
    assert gem.cells_of((34.97, 38.5, 35.02, 38.56)) == [(38, 34), (38, 35)]
    assert gem.cells_of((34.2, 38.9, 34.3, 39.0)) == [(38, 34)]
    assert gem.cells_of((179.9, 0.1, -179.95, 0.2)) == [(0, 179), (0, -180)]


def test_a_tile_on_a_cell_line_takes_each_pixel_from_its_own_cell(product):
    product.cell(38, 34, LINE)
    product.cell(38, 35, LINE)
    n, lines = ingest(ACROSS)
    tiles = stored()
    assert n == len(tiles) and not any("left out" in line for line in lines)
    assert {k[0] for k in tiles} == set(gem.DEFAULT_BANDS) and {k[1] for k in tiles} == {"EPSG:32636"}
    assert_tiles_hold_their_cells(tiles, [(38, 34), (38, 35)])
    dtm = {(x, y): (arr, scene) for (band, _, x, y), (arr, scene, _) in tiles.items() if band == "dtm"}
    both = [scene for arr, scene in dtm.values() if {100.0, 200.0} <= set(np.unique(arr).tolist())]
    west = [scene for arr, scene in dtm.values() if set(np.unique(arr).tolist()) == {100.0}]
    assert both and set(both) == {"google-gem-15-v1:w34s38|w35s38"}
    assert west and set(west) == {"google-gem-15-v1:w34s38"}


def test_the_bands_of_the_dsm_file_keep_their_names(product):
    product.cell(38, 34, LINE)
    ingest((34.90, 38.49, 34.93, 38.51))
    tiles = stored()
    x, y = next((x, y) for (band, _, x, y) in tiles if band == "dsm")
    assert [float(tiles[(band, "EPSG:32636", x, y)][0][0, 0]) for band in gem.DEFAULT_BANDS] == [
        100.0, 101.0, 102.0, 103.0, 104.0, 100.0]


def test_a_cell_the_product_does_not_have_leaves_its_ground_empty(product):
    product.cell(38, 34, LINE)
    n, lines = ingest(ACROSS)
    tiles = stored()
    assert n == len(tiles) and not any("left out" in line for line in lines)
    assert_tiles_hold_their_cells(tiles, [(38, 34)])
    assert any(np.isnan(arr).any() for arr, _, _ in tiles.values())


def test_a_tile_waits_for_a_cell_that_did_not_answer(product):
    """Stored without the cell that failed, the tile would be skipped by every later run."""
    product.cell(38, 34, LINE)
    product.cell(38, 35, LINE)
    product.answers["w35s38"] = None
    n, lines = ingest(ACROSS)
    first = stored()
    assert ingest.left_out > 0
    assert any("left out" in line for line in lines) and any("w35s38" in line and "FAILED" in line for line in lines)
    assert_tiles_hold_their_cells(first, [(38, 34)])
    assert all(not np.isnan(arr).any() for arr, _, _ in first.values())     # only tiles wholly in w34s38
    del product.answers["w35s38"]
    more, lines = ingest(ACROSS)
    tiles = stored()
    assert more > 0 and len(tiles) == n + more and not any("left out" in line for line in lines)
    assert ingest.left_out == 0
    assert_tiles_hold_their_cells(tiles, [(38, 34), (38, 35)])


def test_a_tile_waits_for_a_cell_that_did_not_open(product, monkeypatch):
    product.cell(38, 34, LINE)
    product.cell(38, 35, LINE)
    opened = rasterio.open

    def refuse(path, *args, **kw):
        if "w35s38" in str(path):
            raise RasterioIOError("HTTP response code: 503")
        return opened(path, *args, **kw)

    with monkeypatch.context() as broken:
        broken.setattr(gem.rasterio, "open", refuse)
        n, lines = ingest(ACROSS, bands=["dtm"])
    assert ingest.left_out > 0 and any("w35s38/dtm: FAILED to open" in line for line in lines)
    assert_tiles_hold_their_cells(stored(), [(38, 34)])
    assert all(not np.isnan(arr).any() for arr, _, _ in stored().values())
    more, _ = ingest(ACROSS, bands=["dtm"])
    assert more > 0 and ingest.left_out == 0
    assert_tiles_hold_their_cells(stored(), [(38, 34), (38, 35)])


def test_a_tile_whose_read_fails_is_left_out_and_fetched_later(product, monkeypatch):
    product.cell(38, 34, LINE)
    product.cell(38, 35, LINE)
    compose, calls = gem._compose, []

    def fail_once(*args):
        calls.append(args)
        if len(calls) == 2:
            raise RasterioIOError("Read failed")
        return compose(*args)

    with monkeypatch.context() as broken:
        broken.setattr(gem, "_compose", fail_once)
        n, lines = ingest(ACROSS, bands=["dtm"])
    assert len(calls) > 2 and n == len(calls) - 1 == len(stored())
    assert ingest.left_out == 1 and any("FAILED to read" in line for line in lines)
    more, _ = ingest(ACROSS, bands=["dtm"])
    assert more == 1 and ingest.left_out == 0
    assert_tiles_hold_their_cells(stored(), [(38, 34), (38, 35)])


def test_the_command_says_by_its_exit_code_that_tiles_were_left_out(product):
    import logging
    product.cell(38, 34, LINE)
    product.cell(38, 35, LINE)
    command = ["ingest", "--dataset", "gem15", "--bands", "dtm", "--bbox", *map(str, ACROSS)]
    try:
        product.answers["w35s38"] = None
        assert cli.main(command) == 2
        n = len(stored())
        del product.answers["w35s38"]
        assert cli.main(command) == 0
        assert len(stored()) > n > 0
        assert cli.main(command) == 0
    finally:
        product.answers.clear()
        for handler in list(logging.getLogger("geovault").handlers):     # the run log, in tmp_path
            logging.getLogger("geovault").removeHandler(handler)
            handler.close()


def test_a_second_run_reads_no_tile(product):
    product.cell(38, 34, LINE)
    product.cell(38, 35, LINE)
    n, _ = ingest(ACROSS)
    read = len(product.composed)
    again, _ = ingest(ACROSS)
    assert n > 0 and read > 0 and again == 0 and len(stored()) == n
    assert len(product.composed) == read


def test_every_pixel_keeps_its_place(product):
    """The cells hold a value that differs from pixel to pixel, so a tile cut one pixel
    off, in a cell or on the line between two, does not pass."""
    product.cell(38, 34, LINE, ramped=True)
    product.cell(38, 35, LINE, ramped=True)
    ingest(ACROSS)
    tiles = stored()
    assert {k[0] for k in tiles} == set(gem.DEFAULT_BANDS)
    assert_tiles_hold_their_cells(tiles, [(38, 34), (38, 35)], ramped=True)


def test_a_tile_reaching_past_the_edge_of_a_file(product):
    """The files are on the 15 m lattice, not on the tile lattice: where a file ends inside
    a tile the tile holds the file's pixels in their place and NaN past them."""
    product.cell(38, 34, (34.5, 38.5), tiles=1, shift=15.0 * 100, ramped=True)
    grid = Grid("EPSG:32636", 15.0, gem.TILE_PX)
    with rasterio.open(gem.asset_url(38, 34, "dtm")) as ds:
        tx, ty = grid.tile_of_point(ds.bounds.left + 1.0, ds.bounds.top - 1.0)
        arr, used = gem._compose([(38, 34, ds)], grid, tx, ty, 1, True)
        whole, _ = gem._compose([(38, 34, ds)], grid, tx + 1, ty, 1, True)
    want = expected("dtm", "EPSG:32636", tx, ty, [(38, 34)], ramped=True)
    assert used == ["w34s38"] and arr.shape == (1, gem.TILE_PX, gem.TILE_PX)
    assert np.isnan(arr[0, :, :100]).all()
    assert np.array_equal(arr[0, :, 100:], want[:, 100:])
    assert np.array_equal(whole[0], expected("dtm", "EPSG:32636", tx + 1, ty, [(38, 34)], ramped=True))


def test_south_of_the_equator_is_stored_in_the_southern_zone(product):
    """The cell north of the equator is in zone 37 N, the one south of it in 37 S: each
    zone's tiles hold their own cell and end on the equator."""
    for cell in ((0, 37), (-1, 37)):
        product.cell(*cell, (37.5, 0.0), ramped=True)
    ingest((37.49, -0.01, 37.51, 0.01), bands=["dtm"])
    tiles = stored()
    assert {k[1] for k in tiles} == {"EPSG:32637", "EPSG:32737"}
    assert_tiles_hold_their_cells(tiles, [(0, 37), (-1, 37)], ramped=True)
    for (_, crs, _, _), (arr, scene, _) in tiles.items():
        assert scene == f"google-gem-15-v1:{'w37s0' if crs == 'EPSG:32637' else 'w37sm1'}"
    assert any(np.isnan(arr).any() for arr, _, _ in tiles.values())     # a tile across the equator


def test_only_the_file_of_the_wanted_bands_is_opened(product):
    product.cell(38, 34, LINE)
    n, _ = ingest((34.90, 38.49, 34.93, 38.51), bands=["dtm"])
    assert n > 0 and {k[0] for k in stored()} == {"dtm"}
    assert product.asked and all(name.endswith("_dtm.tif") for name in product.asked)
    with pytest.raises(ValueError, match="no band"):
        gem.ingest(ACROSS, Writer("gem15"), bands=["elevation"])


def test_a_tile_holds_the_cells_of_its_own_utm_zone(product):
    """36 E is a zone line: the ground east of it is stored in the tiles of zone 37, the
    ground west of it in those of zone 36, and none of it twice."""
    line = (36.0, 38.5)
    product.cell(38, 35, line)
    product.cell(38, 36, line)
    ingest((35.97, 38.49, 36.03, 38.51), bands=["dtm"])
    tiles = stored()
    assert {k[1] for k in tiles} == {"EPSG:32636", "EPSG:32637"}
    assert_tiles_hold_their_cells(tiles, [(38, 35), (38, 36)])
    assert {float(v) for (b, crs, x, y), (arr, _, _) in tiles.items() if crs == "EPSG:32636"
            for v in np.unique(arr[np.isfinite(arr)])} == {200.0}
    assert {float(v) for (b, crs, x, y), (arr, _, _) in tiles.items() if crs == "EPSG:32637"
            for v in np.unique(arr[np.isfinite(arr)])} == {300.0}


def test_four_cells_meet_in_one_tile(product):
    corner = (35.0, 39.0)
    for cell in ((38, 34), (38, 35), (39, 34), (39, 35)):
        product.cell(*cell, corner)
    ingest((34.99, 38.99, 35.01, 39.01), bands=["dtm"])
    tiles = stored()
    assert_tiles_hold_their_cells(tiles, [(38, 34), (38, 35), (39, 34), (39, 35)])
    assert any({100.0, 200.0, 400.0, 500.0} <= set(np.unique(arr).tolist()) for arr, _, _ in tiles.values())
    assert any(scene == "google-gem-15-v1:w34s38|w34s39|w35s38|w35s39" for _, scene, _ in tiles.values())


def test_a_file_off_the_lattice_is_refused(product):
    product.cell(38, 34, LINE, shift=7.5)
    with pytest.raises(ValueError, match="not a GEM-15 version 1 file"):
        ingest((34.90, 38.49, 34.93, 38.51))
    assert stored() == {}


def test_polygon_narrows_the_tiles(product):
    from shapely.geometry import box
    product.cell(38, 34, LINE)
    product.cell(38, 35, LINE)
    n_all, _ = ingest(ACROSS, bands=["dtm"])
    store_all = set(stored())
    small = box(34.965, 38.495, 34.970, 38.500)
    kept = stored()
    writer = Writer("gem15b")
    n = gem.ingest(small.bounds, writer, bands=["dtm"], aoi=small, log=lambda line: None)
    writer.close()
    narrow = stored("gem15b")
    assert 0 < n == len(narrow) < n_all == len(store_all)
    for key, (arr, scene, valid) in narrow.items():         # the same tiles, only fewer of them
        assert np.array_equal(arr, kept[key][0], equal_nan=True) and (scene, valid) == kept[key][1:]
