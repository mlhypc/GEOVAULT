"""Offline tests for verify: a whole dataset has no finding, and each way a store or its
catalog can be wrong is found and named, never raised. The deep check reads tiles without
GDAL, so a damaged tile never reaches it."""

import shutil
import struct
import threading

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import rasterio
from rasterio.transform import Affine

from geovault import blob, catalog, cli, store
from geovault import verify as verify_module
from geovault.sources.common import encode_blob, observed
from geovault.verify import verify

CRS, RES, PX = "EPSG:32636", 10, 16
X0, Y1 = 300000.0, 4100000.0
BOUNDS = (32.7, 37.0, 32.8, 37.1)
JANUARY = "2026/01/r10.parquet"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE", tmp_path / "store")
    monkeypatch.setattr(catalog, "STORE", tmp_path / "store")
    monkeypatch.setattr(catalog, "CATALOG", tmp_path / "catalog")
    return tmp_path


def tile(value=7, dtype=np.uint16, nodata=0, empty_cols=0):
    arr = np.full((PX, PX), value, dtype)
    arr[:, :empty_cols] = nodata
    return arr


def encoded(arr, x=0, nodata=0, crs=CRS, res=RES):
    """The blob of a tile where tile number x of the test lattice lies."""
    return encode_blob(arr, crs, Affine(res, 0, X0 + x * RES * PX, 0, -res, Y1), nodata)


def add(w, x=0, date="2026-01-05", arr=None, nodata=0, blob_=None, valid=None, px=PX, band="b",
        acq=None, scene="s"):
    arr = tile() if arr is None else arr
    data = blob_ if blob_ is not None else encoded(arr, x, nodata)
    seen = int(observed(arr, nodata).sum()) if valid is None else valid
    w.add(band, date, x, 0, CRS, RES, px, BOUNDS, nodata, None, scene, data, seen, acq=acq)


def seal(dataset="d"):
    store.compact(dataset)
    catalog.rebuild(dataset)


def whole(dataset="d", months=("2026-01-05", "2026-02-05")):
    """A sealed dataset with a catalog: two tiles in each month."""
    w = store.Writer(dataset)
    for date in months:
        add(w, 0, date)
        add(w, 1, date, arr=tile(9, empty_cols=3))
    w.close()
    seal(dataset)


def path(rel, dataset="d"):
    return store.STORE / f"dataset={dataset}" / rel


def rewrite(rel, change, dataset="d"):
    """Rewrite a store file with its table passed through `change`."""
    f = path(rel, dataset)
    pq.write_table(change(pq.read_table(f)), f)


def set_column(table, name, values):
    i = table.schema.get_field_index(name)
    return table.set_column(i, table.schema.field(i), pa.array(values, table.schema.field(i).type))


def patch_tag(data, tag, value):
    """A tile blob with the value of one TIFF tag replaced (a LONG or SHORT held in its
    entry), as a flipped bit in the header leaves it."""
    data = bytearray(data)
    (at,) = struct.unpack_from("<I", data, 4)
    (n,) = struct.unpack_from("<H", data, at)
    for k in range(n):
        entry = at + 2 + 12 * k
        if struct.unpack_from("<H", data, entry)[0] == tag:
            kind = struct.unpack_from("<H", data, entry + 2)[0]
            struct.pack_into("<I" if kind == 4 else "<H", data, entry + 8, value)
            return bytes(data)
    raise KeyError(tag)


def problems(report):
    return " | ".join(report.problems)


# --- a store that is whole ---------------------------------------------------------------

def test_a_whole_dataset_has_no_finding(vault):
    whole()
    report = verify("d")
    assert report.ok and report.notes == [] and (report.files, report.tiles, report.read) == (2, 4, 0)
    report = verify("d", deep=True)
    assert report.ok and report.notes == [] and report.read == 4


def test_every_kind_of_tile_passes(vault):
    w = store.Writer("d")
    add(w, 0, band="nan", arr=tile(1.5, np.float32, np.nan, empty_cols=2), nodata=np.nan)
    add(w, 1, band="none", arr=tile(0, np.int32), nodata=None, valid=PX * PX)
    add(w, 2, band="f8", arr=tile(2.5, np.float64, -1.0), nodata=-1.0)
    add(w, 3, band="i1", arr=tile(-3, np.int8, -128), nodata=-128)
    for value, acq in ((5, "pass_1"), (6, "pass_2")):               # two overpasses on one day
        add(w, 4, band="a", arr=tile(value), acq=acq, scene=acq)
    add(w, 5, band="a", arr=tile(5, empty_cols=8), acq="pass_1", scene="east")     # one overpass
    half = tile(5)
    half[:, 8:] = 0
    add(w, 5, band="a", arr=half, acq="pass_1", scene="west")                      # in two scenes
    w.flush()
    report = verify("d")                            # before the scenes are merged
    assert len(report.problems) == 3 and "geovault compact d" in problems(report)
    w.close()
    seal()
    report = verify("d", deep=True)
    assert report.ok and report.notes == [] and (report.tiles, report.read) == (7, 7)


def test_nothing_stored_is_said_and_is_not_a_problem(vault):
    report = verify("d")
    assert report.ok and report.notes == ["nothing is stored"]
    whole()
    report = verify("d", date="2031")
    assert report.ok and report.notes == ["nothing is stored for 2031"]
    shutil.rmtree(store.STORE / "dataset=d")
    catalog.rebuild("d")                            # an empty catalog and no store
    report = verify("d")
    assert report.ok and report.notes == ["nothing is stored"]


def test_a_date_narrows_the_check_to_its_months(vault):
    whole()
    for date, tiles, read in (("2026-02", 2, 2), ("2026", 4, 4), ("2026-01-05", 2, 2),
                              ("2026-01-06", 2, 0)):
        report = verify("d", deep=True, date=date)
        assert report.ok and (report.tiles, report.read) == (tiles, read), date
    month = path("2026/02")
    (month / "r10.parquet.tmp").write_bytes(b"x")
    assert verify("d", date="2026-01").notes == [] and len(verify("d", date="2026-02").notes) == 1


def test_many_runs_of_rows_read_every_tile_once(vault, monkeypatch):
    monkeypatch.setattr(store, "GROUP_ROWS", 3)
    monkeypatch.setattr(verify_module, "_RUN_BYTES", 2500)
    w = store.Writer("d")
    good = encoded(tile(), 40)
    for x in range(41):
        add(w, x, blob_=good[:-30] + bytes(30) if x == 40 else None)       # the last tile is damaged
    w.close()
    seal()
    f = path(JANUARY)
    assert pq.ParquetFile(f).metadata.num_row_groups == 14 and f.stat().st_size > 10 * 2500
    for workers in (1, 4):
        report = verify("d", deep=True, workers=workers)
        assert report.read == 40 and "1 tiles are not what their rows say: band b" in problems(report)
        assert "x 40 y 0" in problems(report)


# --- files -------------------------------------------------------------------------------

def test_open_parts_and_tiles_stored_twice_are_found(vault):
    whole()
    w = store.Writer("d")
    add(w, 0, arr=tile(8))                          # the tile again, in a part
    w.flush()
    report = verify("d")
    assert "1 open part files" in problems(report) and "geovault compact d" in problems(report)
    assert ("1 tiles are stored in more than one row (1 rows too many) in months with open part "
            "files") in problems(report)
    assert "the catalog is stale: the store holds 1 tiles it does not list" in problems(report)
    seal()
    assert verify("d", deep=True).ok


def test_a_tile_twice_in_a_sealed_month_is_found_and_not_sent_to_compact(vault):
    whole()
    rewrite(JANUARY, lambda t: pa.concat_tables([t, t.slice(0, 1)]))
    catalog.rebuild("d")
    assert store.compact("d") == []                 # compaction has nothing to do here
    text = problems(verify("d"))
    assert "1 tiles are stored in more than one row of a sealed month (1 rows too many)" in text
    assert "No command resolves them" in text and "geovault compact" not in text


def test_a_file_in_the_folder_of_another_month_or_resolution_is_found(vault):
    whole()
    march = path("2026/03/r10.parquet")
    march.parent.mkdir()
    path(JANUARY).rename(march)
    catalog.rebuild("d")
    assert problems(verify("d")) == "2026/03/r10.parquet holds 2 rows of another month or resolution"
    assert problems(verify("d", date="2026-03")) == problems(verify("d"))       # also when narrowed
    march.rename(path("2026/01/r20.parquet"))
    catalog.rebuild("d")
    assert problems(verify("d")) == "2026/01/r20.parquet holds 2 rows of another month or resolution"


def test_a_parquet_file_that_is_not_of_the_store_is_found(vault):
    whole()
    shutil.copy(path(JANUARY), path("2026/01/r10.copy.parquet"))
    catalog.rebuild("d")                            # which lists its rows as well
    text = problems(verify("d"))
    assert "2026/01/r10.copy.parquet is not a file of the store" in text
    assert "2 tiles are stored in more than one row" in text and "the catalog is stale" not in text


def test_a_file_that_is_cut_short_is_found(vault):
    whole()
    f = path(JANUARY)
    f.write_bytes(f.read_bytes()[:200])
    report = verify("d", deep=True)
    assert "2026/01/r10.parquet cannot be opened" in problems(report)
    assert "geovault rebuild-catalog d" in problems(report)
    assert report.files == 2 and report.read == 2   # the other month is still checked


def test_a_file_with_a_damaged_page_is_found_and_the_others_still_checked(vault):
    whole()
    f = path(JANUARY)
    column = pq.ParquetFile(f).metadata.row_group(0).column(store.COLS.index("x"))
    data = bytearray(f.read_bytes())
    at = column.data_page_offset + column.total_compressed_size // 2
    data[at:at + 16] = bytes(b ^ 0xFF for b in data[at:at + 16])
    f.write_bytes(bytes(data))
    for deep in (False, True):
        report = verify("d", deep=deep)             # reports, does not raise
        assert "2026/01/r10.parquet cannot be read" in problems(report)
        assert (report.files, report.tiles, report.read) == (2, 2, 2 if deep else 0)


def test_a_file_of_another_version_is_found(vault):
    whole()
    rewrite(JANUARY, lambda t: t.drop_columns(["acq"]))
    report = verify("d", deep=True)
    assert "2026/01/r10.parquet does not have the columns of a store file of this version" in problems(report)
    assert report.read == 2


def test_a_file_that_goes_away_while_it_is_read_is_said_and_not_called_damaged(vault, monkeypatch):
    whole()
    real = verify_module._rows

    def compaction_runs_beside(run, con, files):
        path(JANUARY).unlink(missing_ok=True)
        return real(run, con, files)

    monkeypatch.setattr(verify_module, "_rows", compaction_runs_beside)
    report = verify("d", deep=True)
    text = problems(report)
    assert "1 files went away while they were read" in text and "Run verify again" in text
    assert "cannot be" not in text and report.read == 2


def test_a_compaction_beside_a_deep_check_is_not_held_up(vault, monkeypatch):
    """The deep check holds a store file open while it reads its tiles. A compaction of
    that month has to be able to replace the file meanwhile (on Windows a file held
    without sharing cannot be moved), and the check then reads on in the file it has."""
    whole()
    w = store.Writer("d")
    add(w, 5)
    w.flush()                                       # a part for the compaction to merge
    reading, go, real, result = threading.Event(), threading.Event(), verify_module.check, {}

    def slow(data, tile_px=None):
        reading.set()
        assert go.wait(60)
        return real(data, tile_px)

    monkeypatch.setattr(verify_module, "check", slow)
    checker = threading.Thread(target=lambda: result.update(report=verify("d", deep=True, workers=1)))
    checker.start()
    try:
        assert reading.wait(60)                     # a tile of January is in its hands
        sealed = store.compact_month("d", float(RES), "2026-01-01")
        assert store.month_files("d", float(RES), "2026-01-01") == [sealed]
    finally:
        go.set()
        checker.join(120)
    report = result["report"]
    # the sealed file it held was read to its end; the part the compaction merged is gone
    assert report.read == 4 and "1 files went away while they were read" in problems(report)
    assert "cannot be" not in problems(report)
    assert not [f for f in path("2026/01").iterdir() if f.name != "r10.parquet"]
    catalog.rebuild("d")
    monkeypatch.setattr(verify_module, "check", real)
    assert verify("d", deep=True).ok


def test_what_an_interrupted_run_leaves_is_named(vault):
    whole()
    month = path("2026/01")
    (month / "r10.parquet.tmp").write_bytes(b"x")
    (month / "r10.parquet.lock").write_text("999999")
    (month / "notes.txt").write_text("x")
    report = verify("d")
    assert report.ok and len(report.notes) == 3
    (month / "r10.parquet").rename(month / "r10.parquet.old")
    report = verify("d")
    assert "2026/01/r10.parquet.old is a sealed month left aside" in problems(report)


# --- rows --------------------------------------------------------------------------------

def test_rows_that_cannot_be_true_are_found_and_do_not_stop_the_deep_check(vault):
    whole()
    rewrite(JANUARY, lambda t: set_column(set_column(set_column(
        t, "band", [None, "b"]), "valid_px", [PX * PX + 1, 208]), "res", [10.0, None]))
    catalog.rebuild("d")
    report = verify("d", deep=True)
    text = problems(report)
    assert "1 rows with a count of observed pixels no tile of their size can have" in text
    assert "2 rows without a band, date, CRS, tile number or resolution" in text
    assert "its row has no size, tile number or resolution" in text
    rewrite("2026/02/r10.parquet", lambda t: set_column(t, "size_bytes", [0, 5]))
    catalog.rebuild("d")
    assert "1 rows without a size" in problems(verify("d"))


# --- the catalog -------------------------------------------------------------------------

def recatalog(select, dataset="d"):
    """Rewrite the catalog through a SELECT clause, as a catalog of another state."""
    p = catalog.path(dataset).as_posix()
    con = duckdb.connect()
    con.execute(f"COPY ({select} FROM read_parquet('{p}')) TO '{p}.new' (FORMAT PARQUET)")
    con.close()
    shutil.move(p + ".new", p)


def test_a_catalog_that_does_not_list_the_store_is_found(vault):
    whole()
    catalog.path("d").unlink()
    assert problems(verify("d")) == "no catalog: run `geovault rebuild-catalog d`"
    catalog.rebuild("d")
    shutil.rmtree(path("2026/02"))
    assert "it lists 2 the store does not hold" in problems(verify("d"))
    assert "it lists 2 the store does not hold" in problems(verify("d", date="2026-02"))
    assert verify("d", date="2026-01").ok
    shutil.rmtree(store.STORE / "dataset=d")
    assert "it lists 4 the store does not hold" in problems(verify("d"))


def test_a_catalog_whose_values_are_not_those_of_the_store_is_found(vault):
    whole()
    recatalog("SELECT * REPLACE (west + 50 AS west)")
    assert "the store holds 4 tiles it does not list as they are" in problems(verify("d"))
    catalog.rebuild("d")
    recatalog("SELECT * EXCLUDE (west)")
    assert problems(verify("d")) == ("the catalog does not have the columns of this version: "
                                     "run `geovault rebuild-catalog d`")
    catalog.path("d").write_bytes(b"not a parquet file")
    assert "the catalog cannot be read" in problems(verify("d"))


def test_a_catalog_that_names_the_files_of_another_folder_is_found(vault, monkeypatch):
    whole()
    assert verify("d").ok
    copy = vault / "moved"
    shutil.copytree(vault / "store", copy / "store")
    shutil.copytree(vault / "catalog", copy / "catalog")
    monkeypatch.setattr(store, "STORE", copy / "store")
    monkeypatch.setattr(catalog, "STORE", copy / "store")
    monkeypatch.setattr(catalog, "CATALOG", copy / "catalog")
    text = problems(verify("d"))                    # the rows agree, the files are the old ones
    assert "the catalog names 2 files that are not the files of this data folder" in text
    catalog.rebuild("d")
    assert verify("d", deep=True).ok


# --- tiles -------------------------------------------------------------------------------

def test_a_damaged_tile_is_found_without_gdal(vault, monkeypatch):
    whole()
    good = encoded(tile(), 2)
    w = store.Writer("d")
    add(w, 2, blob_=good[:-30] + bytes(30))         # strips that no longer inflate
    add(w, 3, blob_=good[:40])                      # cut short
    add(w, 4, blob_=b"not a tile at all")
    add(w, 5, blob_=patch_tag(encoded(tile(), 5), blob.COMPRESSION, 5))       # a header that changed
    add(w, 6, blob_=patch_tag(encoded(tile(), 6), blob.WIDTH, 0xFFFF))        # and one that asks for
    add(w, 7, blob_=patch_tag(patch_tag(encoded(tile(), 7), blob.WIDTH, 0xFFFF),       # gigabytes
                              blob.HEIGHT, 0xFFFF), px=0xFFFF, valid=1)
    w.close()
    seal()

    def gdal(*args, **kwargs):
        raise AssertionError("a damaged tile was handed to GDAL")

    monkeypatch.setattr(rasterio, "MemoryFile", gdal)
    monkeypatch.setattr(blob, "_decode_gdal", gdal)
    assert verify("d").ok                           # the rows themselves are in order
    monkeypatch.setattr(verify_module, "_NAMED", 10)
    report = verify("d", deep=True)
    text = problems(report)
    assert len(report.problems) == 1 and text.startswith("2026/01/r10.parquet: 6 tiles are not")
    assert "x 2 y 0 EPSG:32636: its strips do not hold the rows its size calls for" in text
    assert "x 3 y 0 EPSG:32636: cut short" in text
    assert "x 4 y 0 EPSG:32636: not a TIFF" in text
    assert "x 5 y 0 EPSG:32636: not laid out as the encoder writes a tile" in text
    assert f"x 6 y 0 EPSG:32636: 65535 x {PX} pixels, not {PX} x {PX}" in text
    assert "x 7 y 0 EPSG:32636: not laid out as the encoder writes a tile" in text
    assert report.read == 4                         # the whole tiles were still read


def test_a_tile_that_is_not_what_its_row_says_is_found(vault, monkeypatch):
    monkeypatch.setattr(verify_module, "_NAMED", 10)
    w = store.Writer("d")
    add(w, 0, arr=tile(empty_cols=4), valid=PX * PX)            # 64 pixels are empty
    add(w, 1, px=PX * 2, valid=PX * PX)                         # a tile of 16 in a row of 32
    add(w, 2)                                                   # as its row says
    add(w, 3, blob_=encoded(tile(5.0, np.float32), 3, np.nan), nodata=-9999.0, valid=PX * PX)
    add(w, 4, blob_=encoded(tile(), 4, crs="EPSG:32635"))       # in the neighbouring zone
    add(w, 5, blob_=encoded(tile(), 9))                         # four tiles east of its number
    add(w, 6)
    w.close()
    seal()
    # pixels of 20 m under a row of 10 m, and a blob one byte longer than its row says
    wide = encode_blob(tile(), CRS, Affine(20, 0, X0 + 6 * RES * PX, 0, -20, Y1), 0)
    rewrite(JANUARY, lambda t: set_column(set_column(t, "data", [
        wide if x == 6 else b for x, b in zip(t["x"].to_pylist(), t["data"].to_pylist())]),
        "size_bytes", [len(wide) if x == 6 else n + (x == 2) for x, n in
                       zip(t["x"].to_pylist(), t["size_bytes"].to_pylist())]))
    catalog.rebuild("d")
    assert verify("d").ok
    report = verify("d", deep=True)
    text = problems(report)
    assert len(report.problems) == 1 and report.read == 5
    assert text.startswith("2026/01/r10.parquet: 7 tiles are not what their rows say: ")
    assert f"x 0 y 0 EPSG:32636: {PX * PX - 4 * PX} observed pixels, its row says {PX * PX}" in text
    assert "x 1 y 0 EPSG:32636: 16 x 16 pixels, not 32 x 32" in text
    assert "x 2 y 0 EPSG:32636: " in text and " bytes, its row says " in text
    assert "x 3 y 0 EPSG:32636: nodata value nan, its row says -9999.0" in text
    assert "x 4 y 0 EPSG:32636: its pixels are in EPSG:32635" in text
    assert "x 5 y 0 EPSG:32636: it does not lie where its tile number says" in text
    assert "x 6 y 0 EPSG:32636: pixels of 20.0 by 20.0, its row says 10.0" in text


def test_a_geotiff_something_else_wrote_is_found(vault):
    arr = np.arange(64 * 64, dtype=np.uint16).reshape(64, 64)
    with rasterio.MemoryFile() as mem:
        with mem.open(driver="GTiff", width=64, height=64, count=1, dtype="uint16", crs=CRS,
                      transform=Affine(RES, 0, X0, 0, -RES, Y1), tiled=True, blockxsize=32,
                      blockysize=32, compress="lzw") as dst:
            dst.write(arr, 1)
        other = mem.read()
    w = store.Writer("d")
    add(w, 0, blob_=other, px=64, valid=64 * 64 - 1)
    add(w, 1)
    w.close()
    seal()
    report = verify("d", deep=True)
    assert report.read == 1 and report.notes == []
    assert "x 0 y 0 EPSG:32636: not laid out as the encoder writes a tile" in problems(report)


def test_check_reads_what_decode_reads(vault):
    for dtype, nodata in ((np.uint8, None), (np.uint16, 0), (np.int16, -32768), (np.int32, -1),
                          (np.float32, np.nan), (np.float64, -9999.0)):
        arr = (np.arange(PX * PX).reshape(PX, PX) % 251).astype(dtype)
        data = encoded(arr, 3, nodata)
        got, transform, tagged, epsg = blob.check(data, PX)
        want, profile = blob.decode(data)
        assert got.dtype == want.dtype and (got == want).all() and transform == profile["transform"]
        assert epsg == 32636 and (tagged is None) == (nodata is None)
    assert blob.check(encoded(tile(), 0, crs="EPSG:4326"))[3] == 4326
    with pytest.raises(blob.BadTile, match="16 x 16 pixels, not 8 x 8"):
        blob.check(encoded(tile()), 8)


def test_the_command_says_what_it_found_and_exits_with_it(vault, capsys):
    whole()
    assert cli.main(["verify", "d", "--deep"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("d: 2 files, 4 tiles, 4 read") and out[-1] == "nothing wrong found"
    catalog.path("d").unlink()
    assert cli.main(["verify", "d"]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[1] == "  PROBLEM: no catalog: run `geovault rebuild-catalog d`" and out[-1] == "1 problems"
