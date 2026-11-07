"""Offline checks for the DEM adapters: owned windows, lattices and file naming (no network)."""

import numpy as np
from rasterio.transform import Affine

from geovault.grid import Grid
from geovault.sources import dem_3dep_1m, dem_fabdem, dem_nasadem
from geovault.sources.static_cog import degree_cells, owned_window

PX = 1.0 / 3600.0


def test_owned_window_without_footprint_is_identity():
    tf = Affine(1.0, 0, 100.0, 0, -1.0, 200.0)
    assert owned_window(tf, 10, 20) == (tf, 10, 20)


def test_srtm_style_files_share_one_lattice_and_partition_pixels():
    """3601 px point-registered files: each owns 3600 px, neighbours tile seamlessly."""
    tiles = {}
    for lat, lon in [(36, -120), (36, -119), (35, -120)]:
        tf = Affine(PX, 0, lon - PX / 2, 0, -PX, lat + 1 + PX / 2)
        (_, fp), = list(dem_nasadem.assets((lon + 0.5, lat + 0.5, lon + 0.6, lat + 0.6)))
        own_tf, w, h = owned_window(tf, 3601, 3601, fp)
        assert (w, h) == (3600, 3600)
        assert own_tf.c == tf.c and own_tf.f == tf.f      # keeps the north row, drops the south one
        g = Grid.from_transform("EPSG:4326", own_tf, dem_nasadem.TILE_PX)
        assert g.aligned_with_raster(own_tf, w, h)
        west, north = own_tf.c + PX / 4, own_tf.f - PX / 4     # a quarter pixel inside the edges
        east, south = own_tf.c + w * PX - PX / 4, own_tf.f - h * PX + PX / 4
        tiles[(lat, lon)] = (g, set(g.tiles_for_bounds(west, south, east, north)))
    grids = {(g.ox, g.oy, g.res) for g, _ in tiles.values()}
    assert len(grids) == 1                                  # one global lattice
    a, b, c = (t for _, t in tiles.values())
    assert len(a) == len(b) == len(c) == 225 and not (a & b) and not (a & c)


def test_usgs_1m_buffer_is_cut_to_the_10km_cell():
    url = ("https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1m/Projects/"
           "CA_SanJoaquin_2021_A21/TIFF/USGS_1M_11_x28y406_CA_SanJoaquin_2021_A21.tif")
    zone, x, y, project = dem_3dep_1m.parse_name(url)
    assert (zone, x, y, project) == (11, 28, 406, "CA_SanJoaquin_2021_A21")
    fp = dem_3dep_1m.footprint(x, y)
    assert fp == (280000.0, 4050000.0, 290000.0, 4060000.0)
    tf = Affine(1.0, 0, 279993.99972324574, 0, -1.0, 4060006.000053974)   # real file header
    own_tf, w, h = owned_window(tf, 10012, 10012, fp)
    assert (w, h) == (10000, 10000)
    assert abs(own_tf.c - 280000.0) < 1e-3 and abs(own_tf.f - 4060000.0) < 1e-3
    g = Grid.from_transform("EPSG:26911", own_tf, dem_3dep_1m.TILE_PX)
    assert g.aligned_with_raster(own_tf, w, h)
    assert g.tile_of_point(280000.5, 4059999.5) == (560, 8119)


def test_fabdem_archive_names():
    assert dem_fabdem.zip_name(36, -120) == "N30W120-N40W110_FABDEM_V1-2.zip"
    assert dem_fabdem.zip_name(39, 35) == "N30E030-N40E040_FABDEM_V1-2.zip"
    (href,) = list(dem_fabdem.assets((35.2, 39.1, 35.3, 39.2), cells={(39, 35)}))
    assert list(dem_fabdem.assets((35.2, 39.1, 35.3, 39.2), cells=set())) == []
    assert href.endswith("N30E030-N40E040_FABDEM_V1-2.zip/N39E035_FABDEM_V1-2.tif")


def test_degree_cells():
    assert list(degree_cells((-119.5, 36.55, -119.3, 36.7))) == [(36, -120)]
    assert len(list(degree_cells((26.0, 36.0, 45.0, 42.0)))) == 6 * 19
    assert list(degree_cells((31.0, 36.0, 39.0, 39.9), step=10)) == [(30, 30)]


def test_usgs_tile_choice_prefers_newest_complete():
    nd = dem_3dep_1m.NODATA
    full_old = np.ones((4, 4), dtype=np.float32)
    part_new = full_old.copy()
    part_new[0] = nd
    full_new = full_old * 2
    calls = []

    def reader(arr, tag):
        def read():
            calls.append(tag)
            return arr
        return read

    i, arr = dem_3dep_1m.pick([reader(part_new, "new"), reader(full_old, "old")], nd)
    assert i == 1 and arr is full_old                          # newest is incomplete
    calls.clear()
    i, _ = dem_3dep_1m.pick([reader(full_new, "new"), reader(full_old, "old")], nd)
    assert i == 0 and calls == ["new"]                          # complete newest: older not read
    assert dem_3dep_1m.pick([reader(None, "a"), reader(part_new, "b")], nd)[0] == 1
    assert dem_3dep_1m.pick([reader(None, "a"), reader(None, "b")], nd) == (None, None)
    assert dem_3dep_1m.touches_zone((-119.5, 36.5, -119.4, 36.6), 11)
    assert not dem_3dep_1m.touches_zone((-119.5, 36.5, -119.4, 36.6), 10)
    # a tile straddling the 10/11 line at -120 is kept by both zones: no uncovered strip
    assert dem_3dep_1m.touches_zone((-120.003, 36.5, -119.997, 36.6), 10)
    assert dem_3dep_1m.touches_zone((-120.003, 36.5, -119.997, 36.6), 11)


def test_usgs_legacy_names():
    assert dem_3dep_1m.parse_name("x/USGS_one_meter_x62y335_TX_Central_B1_2017.tif") == (
        None, 62, 335, "TX_Central_B1_2017")
    assert dem_3dep_1m.parse_name("x/USGS_1m_x31y469_MA_NE_CMGP_Sandy_Z19_2013.tif") == (
        None, 31, 469, "MA_NE_CMGP_Sandy_Z19_2013")
    assert dem_3dep_1m.parse_name("x/readme.txt") is None


def test_fabdem_index_tile_names():
    assert dem_fabdem.parse_tile_name("N079W106") == (79, -106)
    assert dem_fabdem.parse_tile_name("S037E174") == (-37, 174)


def test_fabdem_antimeridian_archive_name():
    assert dem_fabdem.zip_name(-37, 174) == "S40E170-S30W180_FABDEM_V1-2.zip"
    assert dem_fabdem.zip_name(-37, -175) == "S40W180-S30W170_FABDEM_V1-2.zip"


def test_pixel_centred_cells_include_the_half_pixel_strip():
    # a bbox starting just north of 37 N still needs the 37-38 file AND the 36-37 file,
    # which owns ground up to half a pixel north of 37 N
    cells = list(degree_cells((-119.5, 37.00002, -119.49, 37.01), pad=PX / 2))
    assert (36, -120) in cells and (37, -120) in cells


def test_usgs_cell_edge_stores_full_tiles_only(tmp_path, monkeypatch):
    """Two USGS-like cells with a 6 m buffer and a float-noisy origin: the tile at the shared
    edge must come whole from the cell that owns it, not as a 6-column sliver of the other."""
    import duckdb
    import rasterio
    from pyproj import Transformer
    from rasterio.io import MemoryFile
    from geovault import store
    from geovault.aoi import tile_filter
    monkeypatch.setattr(store, "STORE", tmp_path / "store")
    nd = dem_3dep_1m.NODATA
    lon, lat = Transformer.from_crs("EPSG:32611", "EPSG:4326", always_xy=True).transform(
        [280000.0, 300000.0], [4050000.0, 4060000.0])
    bbox = (lon[0] - 0.01, lat[0] - 0.01, lon[1] + 0.01, lat[1] + 0.01)
    w = store.Writer("edge", max_buffer_mb=1000)
    for x, value in ((28, 100.0), (29, 101.0)):
        tf = Affine(1.0, 0, x * 10000 - 6.0003, 0, -1.0, 4060006.0001)
        with MemoryFile() as mem:
            with mem.open(driver="GTiff", width=10012, height=10012, count=1, dtype="float32",
                          crs="EPSG:26911", transform=tf, nodata=nd) as ds:
                ds.write(np.full((10012, 10012), value, dtype=np.float32), 1)
            with mem.open() as ds:
                dem_3dep_1m._cell_tiles([(ds, f"P{x}")], 11, dem_3dep_1m.footprint(x, 406),
                                        bbox, None, tile_filter(None), set(), w)
    w.close()
    rows = duckdb.sql(f"SELECT x, data FROM read_parquet('{(tmp_path / 'store').as_posix()}"
                      f"/dataset=edge/static/*.parquet') ORDER BY x, y").fetchall()
    assert len(rows) == 2 * 20 * 20
    for x, blob in rows:
        with MemoryFile(bytes(blob)) as m, m.open() as ds:
            a = ds.read(1)
        assert (a != nd).all(), f"tile x={x} is not complete"
        assert a[0, 0] == (100.0 if x < 580 else 101.0)
