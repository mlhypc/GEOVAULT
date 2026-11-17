"""Offline tests for the Parquet layout of store files: row groups sized by the bytes
of the tiles they hold, on every path that writes a file."""

import duckdb
import numpy as np
import pytest

from geovault import catalog, store
from geovault.grid import Grid
from geovault.sources.common import encode_geotiff
from geovault.store import Writer, compact_month, month_files, reencode_month

RES = 0.01


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE", tmp_path / "store")
    monkeypatch.setattr(catalog, "STORE", tmp_path / "store")
    monkeypatch.setattr(catalog, "CATALOG", tmp_path / "catalog")
    return tmp_path / "store"


@pytest.fixture
def small_groups(monkeypatch):
    """Groups of 4000 bytes of tiles, so a handful of 1000-byte tiles spans several."""
    monkeypatch.setattr(store, "GROUP_BYTES", 4000)


def add(w, x, blob=b"x" * 1000, date=""):
    return w.add("B", date, x, 0, "EPSG:4326", RES, 10, (0, 0, 1, 1), 0, None, "s", blob, None)


def group_rows(f):
    """Rows in each row group of a file."""
    con = duckdb.connect()
    out = con.execute(f"SELECT row_group_num_rows FROM parquet_metadata('{f.as_posix()}') "
                      f"WHERE path_in_schema = 'data' ORDER BY row_group_id").fetchall()
    con.close()
    return [r[0] for r in out]


def rows_of(files):
    con = duckdb.connect()
    lst = ", ".join(f"'{f.as_posix()}'" for f in files)
    out = con.execute(f"SELECT * FROM read_parquet([{lst}]) ORDER BY band, date, x, y").fetchall()
    con.close()
    return out


def test_a_part_file_is_cut_into_groups_by_tile_bytes(vault, small_groups):
    w = Writer("t")
    for x in range(10):
        add(w, x)
    w.flush()
    (part,) = month_files("t", RES, "")
    assert group_rows(part) == [4, 4, 2]
    assert [r[2] for r in rows_of([part])] == list(range(10))


def test_default_group_is_about_four_megabytes_of_tiles(vault):
    w = Writer("t")
    for x in range(30):
        add(w, x, blob=bytes(300_000))
    w.flush()
    (part,) = month_files("t", RES, "")
    assert group_rows(part) == [14, 14, 2]            # 14 x 300 KB is the first size past 4 MB


def test_a_group_holds_at_most_the_row_limit(vault, monkeypatch):
    monkeypatch.setattr(store, "GROUP_ROWS", 3)
    w = Writer("t")
    for x in range(10):
        add(w, x, blob=b"x")
    w.flush()
    assert group_rows(month_files("t", RES, "")[0]) == [3, 3, 3, 1]


def test_a_tile_larger_than_the_target_is_a_group_of_its_own(vault, small_groups):
    w = Writer("t")
    for x in range(3):
        add(w, x, blob=b"x" * 9000)
    w.flush()
    assert group_rows(month_files("t", RES, "")[0]) == [1, 1, 1]


def test_groups_are_cut_evenly_across_many_rows(vault, monkeypatch):
    """More rows than one read batch: the cut does not follow the batches."""
    monkeypatch.setattr(store, "GROUP_BYTES", 7000)
    w = Writer("t")
    for x in range(600):
        add(w, x)
    w.flush()
    (part,) = month_files("t", RES, "")
    assert group_rows(part) == [7] * 85 + [5]
    assert [r[2] for r in rows_of([part])] == list(range(600))


def test_compaction_writes_sized_groups_and_keeps_every_row(vault, small_groups):
    w = Writer("t", max_buffer_mb=1000)
    for x in range(6):
        add(w, x, date="2024-06-03")
    w.flush()
    for x in range(6, 11):
        add(w, x, date="2024-06-20")
    w.flush()
    before = rows_of(month_files("t", RES, "2024-06-01"))
    sealed = compact_month("t", RES, "2024-06-01")
    assert month_files("t", RES, "2024-06-01") == [sealed]
    assert group_rows(sealed) == [4, 4, 3]
    assert rows_of([sealed]) == before


def test_written_files_keep_the_schema_types(vault):
    w = Writer("t")
    add(w, 1)
    w.flush()
    (part,) = month_files("t", RES, "")
    con = duckdb.connect()
    got = [(c[0], c[1]) for c in con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{part.as_posix()}', hive_partitioning=false)").fetchall()]
    con.execute(f"CREATE TABLE want ({store.SCHEMA})")
    want = [(c[0], c[1]) for c in con.execute("DESCRIBE want").fetchall()]
    con.close()
    assert got == want


def test_key_columns_have_statistics_and_the_tile_bytes_do_not(vault, small_groups):
    w = Writer("t")
    for x in range(10):
        add(w, x)
    w.flush()
    (part,) = month_files("t", RES, "")
    con = duckdb.connect()
    stats = con.execute(
        f"SELECT path_in_schema, stats_min_value, stats_max_value FROM parquet_metadata('{part.as_posix()}') "
        f"WHERE row_group_id = 1").fetchall()
    con.close()
    by_col = {c: (lo, hi) for c, lo, hi in stats}
    assert by_col["x"] == ("4", "7") and by_col["band"] == ("B", "B")
    assert by_col["data"] == (None, None) and by_col["nodata"] == (None, None)
    assert {c for c, (lo, _) in by_col.items() if lo is not None} <= set(store.STAT_COLS)


def test_a_nan_nodata_value_is_found_by_a_filter(vault):
    """NaN is left out of min/max statistics; a group must not be skipped because of it."""
    w = Writer("t")
    for x, nodata in enumerate((0.0, float("nan"), 0.5)):
        w.add("B", "", x, 0, "EPSG:4326", RES, 10, (0, 0, 1, 1), nodata, None, "s", b"x", None)
    w.flush()
    (part,) = month_files("t", RES, "")
    con = duckdb.connect()
    src = f"read_parquet('{part.as_posix()}')"
    assert con.execute(f"SELECT x FROM {src} WHERE isnan(nodata)").fetchall() == [(1,)]
    assert con.execute(f"SELECT x FROM {src} WHERE nodata > 1").fetchall() == [(1,)]
    con.close()


def test_a_part_write_that_fails_leaves_no_part_file(vault, small_groups, monkeypatch):
    def fail_after_one(batches, groups=store._row_groups):
        yield next(groups(batches))
        raise OSError("disk full")
    monkeypatch.setattr(store, "_row_groups", fail_after_one)
    w = Writer("t")
    for x in range(10):
        add(w, x)
    with pytest.raises(OSError):
        w.flush()
    assert month_files("t", RES, "") == []
    assert list((vault / "dataset=t").rglob("*")) == [vault / "dataset=t" / "static"]


def test_reencode_writes_sized_groups(vault, small_groups):
    grid = Grid("EPSG:4326", RES, 16)
    rng = np.random.default_rng(0)
    w = Writer("t")
    for x in range(6):
        arr = rng.integers(0, 30000, (16, 16), dtype=np.int16)
        blob = encode_geotiff(arr, grid, x, 0, "EPSG:4326", -1)
        w.add("B", "", x, 0, "EPSG:4326", RES, 16, (0, 0, 1, 1), -1, None, "s", blob, 256)
    w.close()
    n, _, after = reencode_month("t", RES, "")
    (sealed,) = month_files("t", RES, "")
    sizes = group_rows(sealed)
    assert n == 6 and sum(sizes) == 6 and len(sizes) > 1
    assert after == sum(r[store.COLS.index("size_bytes")] for r in rows_of([sealed]))
