"""Offline checks for the budget NDVI adapters' lattices and band routing (no network)."""

import numpy as np

from geovault.sources import modis_vi, modis_vi_1km, s3_syn_v10, cgls_ndvi300


def test_modis_1km_shares_tile_footprint_with_250m():
    g1, g4 = modis_vi.grid_for(), modis_vi_1km.grid_for()
    assert abs(g1.tile_size - g4.tile_size) < 1e-6
    assert g4.tile_px * modis_vi_1km.FACTOR == g1.tile_px
    scene = 1200 * modis_vi_1km.RES
    assert abs(scene / g4.tile_size - 20) < 1e-9          # 20 x 20 tiles per scene, none straddling
    assert g1.tile_of_point(3000000.0, 4500000.0) == g4.tile_of_point(3000000.0, 4500000.0)


def test_modis_1km_good_share():
    rel = np.full((8, 8), 3, dtype=np.uint8)              # cloud everywhere ...
    rel[:4, :4] = 0                                       # ... except the first 1 km pixel: all good
    rel[4:, 4:] = 255                                     # last pixel entirely fill
    rel[0:4, 4:8][::2] = 1                                # half marginal -> 50 %
    out = modis_vi_1km.good_share(rel, np.uint8(255))
    assert out.shape == (2, 2)
    assert out[0, 0] == 100 and out[0, 1] == 50 and out[1, 0] == 0 and out[1, 1] == 255


def test_modis_1km_band_routing():
    class Item:
        id = "MOD13Q1.A2025177.h20v05.061.x"
        properties = {"platform": "terra"}
        assets = {"250m_16_days_NDVI": 1, "250m_16_days_pixel_reliability": 1}
    pairs = modis_vi_1km._bands_of(Item(), modis_vi_1km.DEFAULT_BANDS)
    assert [p[0] for p in pairs] == ["terra_ndvi", "terra_good"]


def test_syn_v10_lattice_matches_file_edges():
    g = s3_syn_v10.grid_for()
    # the EUROPE file's top-left pixel edge is (-11 + 1/224, 75 + 1/224): a tile corner
    w, s, e, n = g.tile_bounds(*g.tile_of_point(-11 + 1 / 224 + 1e-9, 75 + 1 / 224 - 1e-9))
    assert abs(w - (-11 + 1 / 224)) < 1e-9 and abs(n - (75 + 1 / 224)) < 1e-9
    assert abs(g.tile_size - 0.5) < 1e-12
    tiles = list(g.tiles_for_bounds(25.6, 35.8, 44.9, 42.2))
    assert 500 <= len(tiles) <= 560                       # about 39 x 13 half-degree tiles


def test_syn_v10_window_from_file_lattice():
    class H5:
        def __init__(self):
            self.d = {"latitude": np.linspace(75, 25, 5601), "longitude": np.linspace(-11, 62, 8177)[:-1]}
        def __getitem__(self, k):
            return self.d[k]
    g = s3_syn_v10.grid_for()
    tx, ty = g.tile_of_point(35.0, 39.0)
    r0, c0 = s3_syn_v10._window(H5(), g, tx, ty)
    w, s, e, n = g.tile_bounds(tx, ty)
    assert abs((75 + 1 / 224 - r0 / 112) - n) < 1e-9
    assert abs((-11 - 1 / 224 + c0 / 112) - w) < 1e-9


def test_cgls_lattice_matches_global_cog():
    import rasterio
    g = cgls_ndvi300.grid_for()
    t = rasterio.Affine(0.002976190476204, 0.0, -180.0014880952381, 0.0, -0.0029761904761898, 80.00148809523809)
    assert g.aligned_with_raster(t, 120960, 47040)
    tiles = list(g.tiles_for_bounds(25.6, 35.8, 44.9, 42.2))
    assert 500 <= len(tiles) <= 560
