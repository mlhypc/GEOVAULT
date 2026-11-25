"""Offline tests for export: a GeoTIFF holds the stored pixels of the area asked for, on
the grid they are stored on, with what marks a pixel without a value; a CSV holds the
records of a series."""

import io
from types import SimpleNamespace

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import Affine
from shapely.geometry import box

from geovault import catalog, cli, store
from geovault import export
from geovault.export import CSV_FIELDS, _window, geotiff, series_csv
from geovault.sources.common import encode_blob

CRS = "EPSG:32635"
RES, PX = 10, 10
X0, Y1 = 500000.0, 4200000.0            # upper-left corner of tile (0, 0)
SIZE = RES * PX
DATE = "2026-03-01"
TO_WGS = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)


def wgs_box(w, s, e, n):
    xs, ys = TO_WGS.transform([w, e, w, e], [s, s, n, n])
    return min(xs), min(ys), max(xs), max(ys)


def add(writer, band, tx, ty, arr, nodata, date=DATE, acq=None, scale=None, offset=None):
    """Tile (tx, ty) of the test grid; ty grows southwards."""
    w, n = X0 + tx * SIZE, Y1 - ty * SIZE
    blob = encode_blob(arr, CRS, Affine(RES, 0, w, 0, -RES, n), nodata, scale, offset)
    writer.add(band, date, tx, ty, CRS, RES, PX, wgs_box(w, n - SIZE, w + SIZE, n), nodata,
               None, f"scene{acq or ''}", blob, int(arr.size), acq=acq)


def ramp(tx, ty, dtype):
    """A tile whose pixel values name their own place: 1000 * row + column of the block."""
    rows, cols = np.mgrid[ty * PX:(ty + 1) * PX, tx * PX:(tx + 1) * PX]
    return (1000 * rows + cols).astype(dtype)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A 2 x 2 block of tiles. Bands: `u` uint16 with nodata 0 (never a value of the ramp
    but in pixel (0, 0), which is empty), `f` float32 with nodata NaN, `n` int32 without a
    nodata value and without tile (1, 1), `s` uint16 with a scale and an offset, `a` one
    tile from two overpasses, `h` one tile without a date."""
    monkeypatch.setattr(store, "STORE", tmp_path / "store")
    monkeypatch.setattr(catalog, "STORE", tmp_path / "store")
    monkeypatch.setattr(catalog, "CATALOG", tmp_path / "catalog")
    w = store.Writer("d")
    for tx in (0, 1):
        for ty in (0, 1):
            add(w, "u", tx, ty, ramp(tx, ty, np.uint16), 0)
            add(w, "f", tx, ty, ramp(tx, ty, np.float32) + np.float32(0.1), np.nan)
            add(w, "s", tx, ty, ramp(tx, ty, np.uint16), 0, scale=2.75e-05, offset=-0.2)
            if (tx, ty) != (1, 1):
                add(w, "n", tx, ty, ramp(tx, ty, np.int32), None)
    for value, acq in ((5, "pass_1"), (6, "pass_2")):
        add(w, "a", 0, 0, np.full((PX, PX), value, np.uint16), 0, acq=acq)
    add(w, "h", 0, 0, ramp(0, 0, np.int16), -1, date="")           # a static layer
    w.close()
    store.compact("d")
    catalog.rebuild("d")
    return tmp_path


def area(col0, row0, col1, row1, inset=2.0):
    """The WGS84 bbox of the block's pixels [row0, row1) x [col0, col1), drawn a little
    inside their outer edges so that it touches no pixel beyond them."""
    return wgs_box(X0 + col0 * RES + inset, Y1 - row1 * RES + inset,
                   X0 + col1 * RES - inset, Y1 - row0 * RES - inset)


def read(path):
    """(masked array, transform, what the file says about itself)."""
    with rasterio.open(path) as ds:
        said = SimpleNamespace(crs=ds.crs.to_string(), nodata=ds.nodata, dtype=ds.dtypes[0],
                               descriptions=ds.descriptions, tags=ds.tags(), scales=ds.scales,
                               offsets=ds.offsets)
        return ds.read(1, masked=True), ds.transform, said


def test_the_file_holds_the_pixels_of_the_area_on_their_own_grid(vault):
    out = vault / "u.tif"
    assert geotiff("d", "u", DATE, out, bbox=area(7, 4, 13, 9)) == (5, 6, CRS)
    arr, transform, ds = read(out)
    assert (ds.crs, ds.nodata, ds.dtype) == (CRS, 0, "uint16")
    assert transform == Affine(RES, 0, X0 + 7 * RES, 0, -RES, Y1 - 4 * RES)      # across two tiles
    rows, cols = np.mgrid[4:9, 7:13]
    assert (arr == 1000 * rows + cols).all() and not arr.mask.any()
    assert ds.descriptions == ("u",)
    assert ds.tags["dataset"] == "d" and ds.tags["date"] == DATE


def test_a_pixel_without_a_value_holds_the_nodata_value(vault):
    out = vault / "u.tif"
    geotiff("d", "u", DATE, out, bbox=area(0, 0, 3, 2))
    arr, _, _ = read(out)
    assert arr.mask.tolist() == [[True, False, False], [False, False, False]]   # the empty pixel
    out = vault / "f.tif"
    geotiff("d", "f", DATE, out, bbox=area(18, 18, 20, 20))
    arr, _, ds = read(out)
    assert np.isnan(ds.nodata) and ds.dtype == "float32"
    assert arr.tolist() == [[np.float32(18018.1), np.float32(18019.1)],
                            [np.float32(19018.1), np.float32(19019.1)]]


def test_outside_a_polygon_there_is_no_value(vault):
    out = vault / "u.tif"
    left = box(*area(2, 2, 4, 6))
    right = box(*area(7, 2, 9, 6))
    assert geotiff("d", "u", DATE, out, aoi=left.union(right)) == (4, 7, CRS)
    arr, transform, _ = read(out)
    assert transform.c == X0 + 2 * RES and transform.f == Y1 - 2 * RES
    assert (~arr.mask).sum(axis=0).tolist() == [4, 4, 0, 0, 0, 4, 4]


def test_a_band_without_a_nodata_value_gets_a_mask_in_the_file(vault):
    out = vault / "n.tif"
    assert geotiff("d", "n", DATE, out, bbox=area(8, 8, 12, 12)) == (4, 4, CRS)
    arr, _, ds = read(out)
    assert ds.nodata is None and not (vault / "n.tif.msk").exists()
    assert arr.mask.tolist() == [[False] * 4, [False] * 4, [False, False, True, True],
                                 [False, False, True, True]]           # tile (1, 1) is not stored
    assert arr[0].tolist() == [8008, 8009, 8010, 8011]


def test_the_scale_and_offset_of_a_source_are_carried_over(vault):
    out = vault / "s.tif"
    geotiff("d", "s", DATE, out, bbox=area(1, 1, 3, 3))
    arr, _, ds = read(out)
    assert (ds.scales, ds.offsets) == ((2.75e-05,), (-0.2,))
    assert arr.tolist() == [[1001, 1002], [2001, 2002]]                 # the stored numbers


def test_nothing_stored_there_writes_no_file(vault):
    out = vault / "none.tif"
    assert geotiff("d", "u", DATE, out, bbox=(10.0, 10.0, 10.1, 10.1)) is None
    assert geotiff("d", "u", "2031-01-01", out, bbox=area(0, 0, 3, 3)) is None
    assert geotiff("d", "n", DATE, out, bbox=area(12, 12, 18, 18)) is None      # the missing tile
    assert geotiff("d", "u", DATE, out, bbox=area(0, 0, 1, 1)) is None          # the empty pixel
    assert not out.exists() and list(vault.glob("*.part")) == []
    with pytest.raises(ValueError):
        geotiff("d", "u", DATE, out)
    with pytest.raises(ValueError):
        geotiff("d", "u", DATE, out, bbox=area(0, 0, 3, 3), aoi=box(*area(0, 0, 3, 3)))


def test_a_name_that_is_not_there_is_an_error_not_an_empty_area(vault):
    out, here = vault / "x.tif", area(1, 1, 3, 3)
    with pytest.raises(ValueError, match="no dataset 'dd' is stored"):
        geotiff("dd", "u", DATE, out, bbox=here)
    with pytest.raises(ValueError, match="d has no band 'B4'; it has a, f, h, n, s, u"):
        geotiff("d", "B4", DATE, out, bbox=here)
    with pytest.raises(ValueError, match="has no acquisition 'pass_9' there; it has pass_1, pass_2"):
        geotiff("d", "a", DATE, out, bbox=here, acq="pass_9")
    with pytest.raises(ValueError, match="d has no band 'B4'"):
        series_csv("d", *point(3, 2), ["u", "B4"], io.StringIO())
    assert not out.exists()


def test_a_dated_layer_needs_a_date_and_a_static_one_none(vault):
    out = vault / "h.tif"
    with pytest.raises(ValueError, match="d is stored by date: give the date to export .2026-03-01."):
        geotiff("d", "u", "", out, bbox=area(1, 1, 3, 3))
    assert not out.exists()
    assert geotiff("d", "h", "", out, bbox=area(1, 1, 3, 3)) == (2, 2, CRS)
    arr, _, said = read(out)
    assert arr.tolist() == [[1001, 1002], [2001, 2002]] and said.tags["date"] == "static"


def test_a_failure_leaves_the_file_of_before_and_nothing_half_written(vault, monkeypatch):
    out = vault / "u.tif"
    geotiff("d", "u", DATE, out, bbox=area(1, 1, 3, 3))
    before = out.read_bytes()

    def full(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(export.os, "replace", full)
    with pytest.raises(OSError):
        geotiff("d", "u", DATE, out, bbox=area(4, 4, 9, 9))
    assert out.read_bytes() == before and list(vault.glob("*.part")) == []


def test_which_pixels_touch_a_rectangle():
    grid = Affine(0.01, 0, 35.0, 0, -0.01, 37.0)            # 100 x 100 pixels of 0.01 degrees
    # drawn along pixel edges, where 35.1 / 0.01 is not a whole number to a computer
    assert _window(grid, (100, 100), (35.10, 36.50, 35.20, 36.60)) == (40, 50, 10, 20)
    assert _window(grid, (100, 100), (35.29, 36.71, 35.57, 36.93)) == (7, 29, 29, 57)
    # a thousandth of a pixel past an edge takes the pixel beyond it
    assert _window(grid, (100, 100), (35.10 - 1e-5, 36.50 - 1e-5, 35.20 + 1e-5, 36.60 + 1e-5)) == (
        39, 51, 9, 21)
    assert _window(grid, (100, 100), (35.105, 36.505, 35.106, 36.506)) == (49, 50, 10, 11)
    assert _window(grid, (100, 100), (34.0, 36.95, 35.03, 38.0)) == (0, 5, 0, 3)     # kept inside
    row0, row1, col0, col1 = _window(grid, (100, 100), (36.5, 36.5, 36.6, 36.6))    # beside it
    assert col0 >= col1


def test_an_acquisition_is_named_or_warned_of(vault):
    out = vault / "a.tif"
    with pytest.warns(UserWarning, match="2 acquisitions on this date"):
        geotiff("d", "a", DATE, out, bbox=area(1, 1, 3, 3))
    geotiff("d", "a", DATE, out, bbox=area(1, 1, 3, 3), acq="pass_2")
    arr, _, ds = read(out)
    assert (arr == 6).all() and ds.tags["acq"] == "pass_2"


def point(col, row):
    return TO_WGS.transform(X0 + col * RES + 5, Y1 - row * RES - 5)


def test_a_series_as_csv(vault):
    w = store.Writer("d")
    add(w, "f", 0, 0, ramp(0, 0, np.float32) + np.float32(0.1), np.nan, date="2026-03-02")
    w.close()
    store.compact("d")
    catalog.rebuild("d")
    out = io.StringIO()
    assert series_csv("d", *point(3, 2), ["u", "f", "a"], out) == 5
    lines = out.getvalue().splitlines()
    assert lines == [
        ",".join(CSV_FIELDS),
        f"u,{DATE},2003,,,{CRS},scene",
        f"f,{DATE},2003.1,,,{CRS},scene",                  # a float32 as itself, not 2003.0999755859375
        f"f,2026-03-02,2003.1,,,{CRS},scene",
        f"a,{DATE},5,,pass_1,{CRS},scenepass_1",
        f"a,{DATE},6,,pass_2,{CRS},scenepass_2",
    ]
    out = io.StringIO()
    assert series_csv("d", *point(3, 2), ["f"], out, date="2026-03-02") == 1
    out = io.StringIO()
    assert series_csv("d", 10.0, 10.0, ["n", "u"], out) == 0
    assert out.getvalue() == ",".join(CSV_FIELDS) + chr(10)
    out = io.StringIO()                             # a band with nothing there, then one with
    assert series_csv("d", *point(13, 13), ["n", "u"], out) == 1


def test_the_commands(vault, capsys):
    lon, lat = point(3, 2)
    out = vault / "u.tif"
    w, s, e, n = area(7, 4, 13, 9)
    assert cli.main(["export", "--dataset", "d", "--band", "u", "--date", DATE, "--bbox",
                     str(w), str(s), str(e), str(n), "--out", str(out)]) == 0
    assert capsys.readouterr().out.strip() == f"{out}: 6 x 5 pixels, {CRS}"
    assert read(out)[0].shape == (5, 6)

    assert cli.main(["export", "--dataset", "d", "--band", "a", "--date", DATE, "--point",
                     str(lat), str(lon), "--buffer", "20", "--out", str(out)]) == 0
    said = capsys.readouterr()
    assert "warning: d a 2026-03-01: 2 acquisitions on this date" in said.err
    assert read(out)[0].shape == (5, 5)             # 20 m each way of a pixel centre

    assert cli.main(["export", "--dataset", "d", "--band", "u", "--date", DATE, "--bbox",
                     "10", "10", "10.1", "10.1", "--out", str(vault / "none.tif")]) == 1
    assert "nothing written" in capsys.readouterr().err and not (vault / "none.tif").exists()
    with pytest.raises(SystemExit):                 # an area has to be given, and only one
        cli.main(["export", "--dataset", "d", "--band", "u", "--out", str(out)])
    capsys.readouterr()

    # what is not there, and what cannot be written, is said in one line and is not "nothing stored"
    here = ["--bbox", str(w), str(s), str(e), str(n)]
    for wrong, said in ((["--dataset", "dd", "--band", "u", "--date", DATE], "no dataset 'dd' is stored"),
                        (["--dataset", "d", "--band", "B4", "--date", DATE], "d has no band 'B4'"),
                        (["--dataset", "d", "--band", "u"], "d is stored by date")):
        with pytest.raises(SystemExit) as stop:
            cli.main(["export", *wrong, *here, "--out", str(out)])
        assert stop.value.code == 2 and said in capsys.readouterr().err
    assert cli.main(["export", "--dataset", "d", "--band", "u", "--date", DATE, *here,
                     "--out", str(vault / "no such folder" / "u.tif")]) == 2
    err = capsys.readouterr().err
    assert err.startswith("geovault: error: ") and err.count(chr(10)) == 1
    assert cli.main(["export", "--dataset", "d", "--band", "u", "--date", DATE, "--geojson",
                     str(vault / "none.geojson"), "--out", str(out)]) == 2
    assert capsys.readouterr().err.startswith("geovault: error: ")
    assert cli.main(["series", "--dataset", "d", "--bands", "u", "--point", str(lat), str(lon),
                     "--out", str(vault / "no such folder" / "p.csv")]) == 2
    assert capsys.readouterr().err.startswith("geovault: error: ")

    assert cli.main(["series", "--dataset", "d", "--bands", "u", "--point", str(lat), str(lon)]) == 0
    said = capsys.readouterr()
    assert said.out.splitlines() == [",".join(CSV_FIELDS), f"u,{DATE},2003,,,{CRS},scene"]
    csv = vault / "p.csv"
    assert cli.main(["series", "--dataset", "d", "--bands", "u", "f", "--point", str(lat), str(lon),
                     "--date", "2026", "--out", str(csv)]) == 0
    assert "2 records written" in capsys.readouterr().err
    assert csv.read_text(encoding="utf-8").count("\n") == 3
    assert cli.main(["series", "--dataset", "d", "--bands", "u", "--point", "10", "10"]) == 1
    assert "nothing is stored at this point" in capsys.readouterr().err
