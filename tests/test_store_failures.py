"""Offline tests for what the store keeps when a write or a compaction fails: a file
another program holds open, a part that cannot be written. The file tests hold real
handles; what a held file refuses differs between Windows and POSIX, so they assert
what must be true on both: no row is lost and none is stored twice."""

import hashlib
import os
from pathlib import Path

import duckdb
import pytest

from geovault import store
from geovault.store import Writer, compact_month, month_files, reencode_month

RES, MONTH = 10.0, "2024-06-01"
WINDOWS = os.name == "nt"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE", tmp_path / "store")
    return tmp_path / "store"


def add(w, x, blob=None, valid=None, date="2024-06-03", scene="s"):
    return w.add("B", date, x, 0, "EPSG:32636", RES, 10, (0, 0, 1, 1), 0, None, scene,
                 blob if blob is not None else bytes([x % 251]) * 1000, valid)


def rows(dataset="t"):
    """Every stored row of the dataset: (date, x, scene, hash of the tile bytes), sorted."""
    files = sorted((store.STORE / f"dataset={dataset}").rglob("r*.parquet"))
    if not files:
        return []
    con = duckdb.connect()
    out = con.execute(f"SELECT date, x, scene_id, data FROM {store.scan(files)}").fetchall()
    con.close()
    return sorted((d, x, s, hashlib.md5(bytes(b)).hexdigest()) for d, x, s, b in out)


def month(xs_sealed, xs_part):
    """A month with a sealed file and one open part. Returns (sealed, part, expected rows)."""
    w = Writer("t")
    for x in xs_sealed:
        add(w, x)
    w.flush()
    sealed = compact_month("t", RES, MONTH)
    for x in xs_part:
        add(w, x)
    w.flush()
    (part,) = [f for f in month_files("t", RES, MONTH) if f != sealed]
    return sealed, part, rows()


def leftovers(vault):
    return sorted(p.name for p in vault.rglob("*") if p.suffix in (".tmp", ".old", ".lock"))


def hold_shared(path):
    """Hold a file the way a reader does on Windows: others may read, write and delete
    it. Returns the function that lets go."""
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.restype = wintypes.HANDLE
    k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                              wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k.CreateFileW(str(path), 0x80000000, 0x7, None, 3, 0x80, None)   # read; share all; open existing
    assert handle and handle != wintypes.HANDLE(-1).value, ctypes.get_last_error()
    return lambda: k.CloseHandle(handle)


def spy_moves(monkeypatch):
    """Record every rename as (from name, to name)."""
    moves, real = [], Path.replace

    def replace(self, target):
        moves.append((self.name, Path(target).name))
        return real(self, target)
    monkeypatch.setattr(Path, "replace", replace)
    return moves


# --- compaction and files held open -----------------------------------------------------------

def test_a_part_another_program_holds_open_costs_no_rows(vault):
    sealed, part, want = month([0, 1, 2], [5])
    with open(part, "rb"):
        assert compact_month("t", RES, MONTH) == sealed
        con = duckdb.connect()
        in_sealed = con.execute(f"SELECT x FROM read_parquet('{sealed.as_posix()}') ORDER BY x").fetchall()
        con.close()
        assert in_sealed == [(0,), (1,), (2,), (5,)]          # the sealed file is whole
        if WINDOWS:                                            # the part could not be deleted
            assert part.exists() and store.open_months("t") == [(RES, MONTH)]
            assert store.compact("t") == []                    # and the month is not counted as sealed
    assert store.compact("t") == ([(RES, MONTH)] if WINDOWS else [])
    assert rows() == want and month_files("t", RES, MONTH) == [sealed]
    assert leftovers(vault) == []


@pytest.mark.skipif(not WINDOWS, reason="what a held file refuses is Windows behaviour")
def test_a_reader_holding_the_sealed_file_does_not_stop_a_compaction(vault, monkeypatch):
    sealed, part, want = month([0, 1, 2], [5])
    moves = spy_moves(monkeypatch)
    release = hold_shared(sealed)
    try:
        assert compact_month("t", RES, MONTH) == sealed
        assert rows() == want                                  # while the old file is still held
    finally:
        release()
    assert ("r10.parquet", "r10.parquet.old") in moves         # it had to be moved aside
    assert month_files("t", RES, MONTH) == [sealed] and leftovers(vault) == []


def test_a_sealed_file_another_program_holds_open_is_left_whole(vault):
    sealed, part, want = month([0, 1, 2], [5])
    with open(sealed, "rb"):
        if WINDOWS:
            with pytest.raises(PermissionError):
                compact_month("t", RES, MONTH)
            assert month_files("t", RES, MONTH) == [sealed, part]
        else:
            compact_month("t", RES, MONTH)
        assert rows() == want
    assert leftovers(vault) == []                              # no stray file, no lock
    compact_month("t", RES, MONTH)
    assert rows() == want and month_files("t", RES, MONTH) == [sealed]


def test_a_new_file_that_cannot_take_its_place_leaves_the_old_one(vault, monkeypatch):
    """The new file is refused its place after the sealed file was moved aside for it:
    the sealed file has to come back."""
    sealed, part, want = month([0, 1, 2], [5])
    before = sealed.read_bytes()
    real = Path.replace

    def held(self, target):
        if self.name.endswith(".parquet.tmp"):
            raise PermissionError(32, "held by another program", str(self))
        return real(self, target)
    monkeypatch.setattr(Path, "replace", held)
    with pytest.raises(PermissionError):
        compact_month("t", RES, MONTH)
    monkeypatch.undo()
    monkeypatch.setattr(store, "STORE", vault)
    assert sealed.read_bytes() == before and part.exists() and rows() == want
    assert leftovers(vault) == []
    compact_month("t", RES, MONTH)
    assert rows() == want and month_files("t", RES, MONTH) == [sealed]


@pytest.mark.skipif(not WINDOWS, reason="the error code is what Windows reports")
def test_a_held_new_file_is_told_from_a_held_sealed_file(vault, monkeypatch):
    """Error 32 on the first rename means the new file itself is held: moving the sealed
    file aside could not help, so it is not touched."""
    sealed, part, want = month([0, 1, 2], [5])
    moves, real = [], Path.replace

    def held(self, target):
        moves.append((self.name, Path(target).name))
        if self.name.endswith(".parquet.tmp"):
            raise PermissionError(13, "held by another program", str(self), 32)
        return real(self, target)
    monkeypatch.setattr(Path, "replace", held)
    with pytest.raises(PermissionError):
        compact_month("t", RES, MONTH)
    assert moves == [("r10.parquet.tmp", "r10.parquet")]
    monkeypatch.undo()
    monkeypatch.setattr(store, "STORE", vault)
    assert rows() == want and leftovers(vault) == []


def test_a_sealed_file_that_cannot_be_moved_back_is_found_by_the_next_compact(vault, monkeypatch):
    """The worst case: aside, the new file refused, and the way back refused too. The
    month is then listed as open and `compact` puts the sealed file back."""
    sealed, part, want = month([0, 1, 2], [5])
    real = Path.replace

    def held(self, target):
        if self.name.endswith(".parquet.tmp") or self.name.endswith(".parquet.old"):
            raise PermissionError(13, "held by another program", str(self))
        return real(self, target)
    monkeypatch.setattr(Path, "replace", held)
    with pytest.raises(PermissionError):
        compact_month("t", RES, MONTH)
    monkeypatch.undo()
    monkeypatch.setattr(store, "STORE", vault)
    assert not sealed.exists() and sealed.with_suffix(".parquet.old").exists()
    assert store.open_months("t") == [(RES, MONTH)]
    assert store.compact("t") == [(RES, MONTH)]
    assert rows() == want and month_files("t", RES, MONTH) == [sealed] and leftovers(vault) == []


def test_a_sealed_file_left_aside_by_a_killed_run_is_put_back(vault):
    sealed, part, want = month([0, 1, 2], [5])
    sealed.replace(sealed.with_suffix(".parquet.old"))         # killed between the two renames
    assert compact_month("t", RES, MONTH) == sealed
    assert rows() == want and leftovers(vault) == []


def test_a_month_left_with_nothing_but_its_aside_file_is_found_by_compact(vault):
    """A re-encode of a month without parts, killed between the two renames."""
    w = Writer("t")
    for x in range(3):
        add(w, x)
    w.flush()
    sealed = compact_month("t", RES, MONTH)
    want = rows()
    sealed.replace(sealed.with_suffix(".parquet.old"))
    assert rows() == [] and store.open_months("t") == [(RES, MONTH)]
    assert store.compact("t") == [(RES, MONTH)]
    assert rows() == want and store.open_months("t") == [] and leftovers(vault) == []


def two_copies(sealed, part, n=40):
    """A month whose sealed file and whose part hold the same n keys with the same
    fetched_at. sealed, part: (scene, valid_px, blob size) of each copy."""
    d = store.month_dir("t", MONTH)
    d.mkdir(parents=True)
    con = duckdb.connect()
    for name, (scene, valid, size) in (("r10.parquet", sealed), ("r10.part-aaaaaaaa.parquet", part)):
        con.execute(f"CREATE OR REPLACE TABLE buf ({store.SCHEMA})")
        con.executemany(f"INSERT INTO buf VALUES ({', '.join('?' for _ in store.COLS)})",
                        [["B", "2024-06-03", x, 0, "EPSG:32636", RES, 10, 0.0, 0.0, 1.0, 1.0, 0.0, None,
                          scene, b"z" * size, size, 7, None, valid] for x in range(n)])
        store._write_sorted(con, "SELECT * FROM buf", d / name)
    kept = compact_month("t", RES, MONTH)
    out = con.execute(f"SELECT DISTINCT scene_id, valid_px, size_bytes "
                      f"FROM read_parquet('{kept.as_posix()}')").fetchall()
    con.close()
    return out


def test_of_two_copies_of_one_age_the_fuller_is_kept(vault):
    """Two processes storing one tile in the same millisecond: as in one Writer, the
    fuller tile wins, though the emptier one is the smaller blob."""
    assert two_copies(("sparse", 64, 300), ("full", 100, 900)) == [("full", 100, 900)]
    assert len(rows()) == 40


def test_of_two_equal_copies_the_smaller_scene_id_is_kept(vault):
    assert two_copies(("scene_b", 100, 300), ("scene_a", 100, 900)) == [("scene_a", 100, 900)]


def test_of_two_encodings_of_one_tile_the_smaller_is_kept(vault):
    """A re-encoded month next to a part the re-encode could not delete."""
    assert two_copies(("s", 100, 900), ("s", 100, 300)) == [("s", 100, 300)]


def test_a_stale_aside_file_is_dropped_when_the_sealed_file_is_there(vault):
    sealed, part, want = month([0, 1, 2], [5])
    sealed.with_suffix(".parquet.old").write_bytes(b"not a month file")
    assert compact_month("t", RES, MONTH) == sealed
    assert rows() == want and leftovers(vault) == []


def test_reencode_with_a_part_held_open_costs_no_rows(vault):
    import numpy as np
    from rasterio.transform import Affine
    from geovault.sources.common import encode_blob
    w = Writer("t")
    for x in (0, 1, 2):
        blob = encode_blob(np.full((16, 16), x + 1, np.int16), "EPSG:32636", Affine(10, 0, 0, 0, -10, 0), -1)
        add(w, x, blob=blob)
        w.flush()
    parts = month_files("t", RES, MONTH)
    with open(parts[1], "rb"):
        assert reencode_month("t", RES, MONTH)[0] == 3
        xs = [r[1] for r in rows()]
        assert sorted(set(xs)) == [0, 1, 2] and len(xs) == (4 if WINDOWS else 3)   # the held part is a second copy
    compact_month("t", RES, MONTH)
    assert [r[1] for r in rows()] == [0, 1, 2] and leftovers(vault) == []


# --- a flush that fails -----------------------------------------------------------------------

def failing(monkeypatch, on_call, before=None):
    """Make the n-th file write fail; `before` runs first, inside the failing write."""
    real, calls = store._write_sorted, []

    def write(con, rows_sql, path):
        calls.append(path)
        if len(calls) == on_call:
            if before:
                before()
            raise OSError(28, "No space left on device")
        return real(con, rows_sql, path)
    monkeypatch.setattr(store, "_write_sorted", write)


def test_a_failed_flush_keeps_its_rows_for_the_next_one(vault, monkeypatch):
    w = Writer("t")
    for x in range(3):
        add(w, x)
    failing(monkeypatch, on_call=1)
    with pytest.raises(OSError):
        w.flush()
    assert len(w.rows) == 3 and w.buffered_bytes == 3000 and rows() == [] and w.written == {}
    assert sum(w.flush().values()) == 3
    assert [r[1] for r in rows()] == [0, 1, 2] and w.rows == [] and w.buffered_bytes == 0


def test_a_month_that_cannot_be_written_does_not_hold_back_the_others(vault, monkeypatch):
    w = Writer("t")
    for x in range(3):
        add(w, x, date="2024-06-03")
    for x in range(3, 5):
        add(w, x, date="2024-07-01")
    add(w, 5, date="2024-08-01")
    failing(monkeypatch, on_call=2)
    with pytest.raises(OSError):
        w.flush()
    assert [r[1] for r in rows()] == [0, 1, 2, 5] and sum(w.written.values()) == 4
    assert sorted(r["x"] for r in w.rows) == [3, 4] and w.buffered_bytes == 2000
    assert sum(w.flush().values()) == 2                         # the written months are not written again
    assert [r[1] for r in rows()] == [0, 1, 2, 3, 4, 5] and sum(w.written.values()) == 6


def test_a_flush_that_cannot_start_keeps_its_rows(vault, monkeypatch):
    w = Writer("t")
    for x in range(3):
        add(w, x)
    real, calls = duckdb.connect, []

    def connect(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("no connection")
        return real(*a, **k)
    monkeypatch.setattr(store.duckdb, "connect", connect)
    with pytest.raises(RuntimeError):
        w.flush()
    assert len(w.rows) == 3 and w.buffered_bytes == 3000
    assert sum(w.flush().values()) == 3 and [r[1] for r in rows()] == [0, 1, 2]


def test_a_part_that_cannot_be_renamed_into_place_leaves_no_file(vault, monkeypatch):
    w = Writer("t")
    for x in range(3):
        add(w, x)
    real = Path.replace

    def held(self, target):
        if self.name.endswith(".parquet.tmp"):
            raise PermissionError(13, "held by another program", str(self))
        return real(self, target)
    monkeypatch.setattr(Path, "replace", held)
    with pytest.raises(PermissionError):
        w.flush()
    monkeypatch.undo()
    monkeypatch.setattr(store, "STORE", vault)
    assert list(vault.rglob("*.*")) == [] and len(w.rows) == 3
    assert sum(w.flush().values()) == 3 and [r[1] for r in rows()] == [0, 1, 2]


def test_a_tile_added_again_after_a_failed_flush_is_stored_once(vault, monkeypatch):
    w = Writer("t")
    add(w, 0, blob=b"a" * 1000, valid=10)
    add(w, 1, blob=b"a" * 1000, valid=10)
    failing(monkeypatch, on_call=1)
    with pytest.raises(OSError):
        w.flush()
    assert add(w, 0, blob=b"a" * 1000, valid=10) is False       # the same tile: it is still buffered
    assert add(w, 1, blob=b"b" * 1000, valid=50) is False       # a fuller one takes its place
    assert sum(w.flush().values()) == 2
    assert rows() == [("2024-06-03", 0, "s", hashlib.md5(b"a" * 1000).hexdigest()),
                      ("2024-06-03", 1, "s", hashlib.md5(b"b" * 1000).hexdigest())]


def test_a_fuller_tile_arriving_during_a_failed_flush_stands(vault, monkeypatch):
    w = Writer("t")
    add(w, 0, blob=b"a" * 1000, valid=10)
    add(w, 1, blob=b"c" * 1000, valid=10)
    failing(monkeypatch, on_call=1, before=lambda: add(w, 0, blob=b"b" * 1000, valid=50))
    with pytest.raises(OSError):
        w.flush()
    assert len(w.rows) == 2 and w.buffered_bytes == 2000
    w.flush()
    assert rows() == [("2024-06-03", 0, "s", hashlib.md5(b"b" * 1000).hexdigest()),
                      ("2024-06-03", 1, "s", hashlib.md5(b"c" * 1000).hexdigest())]
