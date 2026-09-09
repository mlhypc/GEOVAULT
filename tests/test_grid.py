"""Offline unit tests for the grid and AOI math (no network, no data dir)."""

from shapely.geometry import Polygon

from geovault.aoi import tile_filter
from geovault.grid import Grid
from geovault.sources.common import canonical_epsg


def test_grid_roundtrip():
    g = Grid("EPSG:32636", 10, 256)
    tx, ty = g.tile_of_point(527531.0, 4063217.0)
    w, s, e, n = g.tile_bounds(tx, ty)
    assert w <= 527531.0 < e
    assert s <= 4063217.0 < n
    assert (e - w) == 2560.0


def test_grid_deterministic():
    g = Grid("EPSG:32636", 10, 256)
    assert g.tile_of_point(0.0, 0.0) == (0, 0)
    assert g.tile_of_point(-0.1, -0.1) == (-1, -1)
    assert g.tile_of_point(2560.0, 2560.0) == (1, 1)


def test_tiles_for_bounds_covers_rect():
    g = Grid("EPSG:32636", 20, 256)
    tiles = list(g.tiles_for_bounds(0.0, 0.0, 10240.0, 5120.0))
    assert (0, 0) in tiles and (1, 0) in tiles
    assert len(tiles) == 2 * 1


def test_canonical_epsg():
    assert canonical_epsg(35.3, 36.7) == "EPSG:32636"
    assert canonical_epsg(36.5, 36.7) == "EPSG:32637"
    assert canonical_epsg(35.3, -20.0) == "EPSG:32736"


def test_tile_filter_polygon():
    tri = Polygon([(0, 0), (10, 0), (0, 10)])
    f = tile_filter(tri)
    assert f((1, 1, 2, 2)) is True or f((1, 1, 2, 2)) == True
    assert not f((9, 9, 10, 10))
    assert tile_filter(None)((999, 999, 1000, 1000))



def test_static_grid_is_global_across_files():
    """Two 1-degree GLO-30 files (pixel-centre registered: origin at k - 0.5 px) must give the same
    tile index and bounds for the same ground, and their edges must fall on tile edges."""
    from rasterio.transform import Affine
    from geovault.grid import Grid
    res = 1.0 / 3600.0
    f35 = Affine(res, 0, 35 - res / 2, 0, -res, 38 - res / 2)
    f36 = Affine(res, 0, 36 - res / 2, 0, -res, 38 - res / 2)
    g35 = Grid.from_transform("EPSG:4326", f35, 240)
    g36 = Grid.from_transform("EPSG:4326", f36, 240)
    assert abs(g35.ox - g36.ox) < 1e-12 and abs(g35.oy - g36.oy) < 1e-12
    pt = (35.9999, 37.5)
    assert g35.tile_of_point(*pt) == g36.tile_of_point(*pt)
    assert g35.aligned_with_raster(f35, 3600, 3600) and g36.aligned_with_raster(f36, 3600, 3600)
    assert not Grid.from_transform("EPSG:4326", f35, 256).aligned_with_raster(f35, 3600, 3600)
    # WorldCover: 3-degree files at 1/12000 deg, origin exactly on the degree line
    wres = 1.0 / 12000.0
    w33 = Grid.from_transform("EPSG:4326", Affine(wres, 0, 33.0, 0, -wres, 39.0), 240)
    w36 = Grid.from_transform("EPSG:4326", Affine(wres, 0, 36.0, 0, -wres, 39.0), 240)
    assert abs(w33.ox) < 1e-9 and abs(w33.ox - w36.ox) < 1e-12
    assert w33.aligned_with_raster(Affine(wres, 0, 33.0, 0, -wres, 39.0), 36000, 36000)


def test_landsat_grid_is_global_across_scenes():
    """Landsat C2 scene origins end in ...85/...15 (pixel edges 15 m off the UTM origin) for
    every generation. The fixed (15, 15) anchor must put each scene pixel edge exactly on a
    tile pixel edge, and the same ground must map to the same tile from any scene."""
    from geovault.sources.landsat import grid_for
    g = grid_for("EPSG:32636")
    # real origins: L9 2026, L7 2010, L5 1995 (path 175 row 34)
    for ox, oy in ((606285.0, 4265715.0), (599085.0, 4256115.0), (596985.0, 4257015.0)):
        tx, ty = g.tile_of_point(ox, oy - 1)
        w, s, e, n = g.tile_bounds(tx, ty)
        assert abs(((ox - w) / g.res) - round((ox - w) / g.res)) < 1e-9
        assert abs(((n - oy) / g.res) - round((n - oy) / g.res)) < 1e-9
    assert g.tile_size == 7680.0
    assert g.tile_of_point(15.0, 15.0) == (0, 0)
    assert g.tile_of_point(14.9, 14.9) == (-1, -1)


def test_landsat_cloud_pct_from_qa():
    import numpy as np
    from geovault.sources.landsat import cloud_pct_from_qa
    fill, clear, cloud, shadow = 1, 1 << 6, 1 << 3, 1 << 4
    qa = np.array([[fill, clear], [cloud, shadow]], dtype=np.uint16)
    assert cloud_pct_from_qa(qa) == round(2 / 3 * 100, 2)
    assert cloud_pct_from_qa(np.full((2, 2), fill, dtype=np.uint16)) is None


def test_encode_geotiff_scale_offset_roundtrip():
    import io
    import numpy as np
    import rasterio
    from geovault.grid import Grid
    from geovault.sources.common import encode_geotiff
    g = Grid("EPSG:32636", 30, 16, 15.0, 15.0)
    arr = np.arange(256, dtype=np.uint16).reshape(16, 16)
    blob = encode_geotiff(arr, g, 3, 4, "EPSG:32636", 0, 2.75e-05, -0.2)
    with rasterio.MemoryFile(io.BytesIO(blob)) as mem, mem.open() as ds:
        assert ds.scales == (2.75e-05,) and ds.offsets == (-0.2,)
        assert (ds.read(1) == arr).all()
        assert ds.transform.c == 15.0 + 3 * 480 and ds.nodata == 0
