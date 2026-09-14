"""Offline checks for the meteorology adapters' lattices (no network)."""

from geovault.sources import chirps, modis_lst


def test_chirps_lattice_matches_file_origin():
    g = chirps.grid_for()
    # the global file starts at -180 / 50: both must be tile corners
    assert g.tile_bounds(*g.tile_of_point(-180.0, 49.9))[0] == -180.0
    assert g.tile_bounds(*g.tile_of_point(-179.9, 49.9))[3] == 50.0
    # Turkey bbox -> about 10 tiles of 5 degrees
    tiles = list(g.tiles_for_bounds(25.6, 35.8, 44.9, 42.2))
    assert 8 <= len(tiles) <= 12


def test_modis_scene_is_whole_tiles():
    g = modis_lst.grid_for()
    scene = 1200 * modis_lst.RES
    origin = -20015109.354
    k = origin / g.tile_size
    assert abs(k - round(k)) < 1e-6          # global origin on a tile corner
    assert abs(scene / g.tile_size - 5) < 1e-9  # 5 x 5 tiles per scene, none straddling


def test_modis_band_routing():
    class Item:
        id = "MYD11A1.A2026154.h20v05.061.x"
        assets = {"LST_Day_1km": 1, "QC_Day": 1}
    pairs = modis_lst._bands_of(Item(), modis_lst.DEFAULT_BANDS)
    assert pairs == [("aqua_lst_day", "LST_Day_1km"), ("aqua_qc_day", "QC_Day")]
