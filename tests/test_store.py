"""Offline tests for the store Writer: auto-flush, flushed-key replacement, compaction."""

import duckdb
import pytest

from geovault import store
from geovault.store import Writer, compact_month, month_files


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE", tmp_path / "store")
    return tmp_path / "store"


def _add(w, x, blob=b"x" * 1000, valid=None, scene="s1", date=""):
    return w.add("B", date, x, 0, "EPSG:4326", 0.01, 10, (0, 0, 1, 1), 0, None, scene, blob, valid)


def _rows(files):
    con = duckdb.connect()
    lst = ", ".join(f"'{f.as_posix()}'" for f in files)
    out = con.execute(f"SELECT x, scene_id, fetched_at FROM read_parquet([{lst}]) ORDER BY x").fetchall()
    con.close()
    return out


def test_auto_flush_past_buffer_limit(tmp_store):
    lines = []
    w = Writer("t", max_buffer_mb=0.0035, log=lines.append)   # 3.5 KB: every 4th 1 KB tile flushes
    for x in range(10):
        assert _add(w, x)
    parts = month_files("t", 0.01, "")
    assert len(parts) == 2 and sum(w.written.values()) == 8
    assert len(w.rows) == 2 and w.buffered_bytes == 2000
    assert len(lines) == 2 and "flushed 4 rows" in lines[0]
    w.close()
    assert sum(w.written.values()) == 10 and w.rows == [] and w.buffered_bytes == 0
    assert len(_rows(month_files("t", 0.01, ""))) == 10


def test_duplicate_key_after_flush_is_resolved_by_compaction(tmp_store):
    w = Writer("t", max_buffer_mb=1000)
    assert _add(w, 1, valid=10, scene="edge")
    w.flush()
    assert _add(w, 1, valid=10, scene="edge") is False       # same tile again: dropped
    assert _add(w, 1, valid=5, scene="worse") is False        # fewer valid pixels: dropped
    assert len(w.rows) == 0
    assert _add(w, 1, valid=50, scene="full") is False        # better: appended, newer fetched_at
    assert len(w.rows) == 1
    w.close()
    assert len(month_files("t", 0.01, "")) == 2
    compact_month("t", 0.01, "")
    rows = _rows(month_files("t", 0.01, ""))
    assert rows == [(1, "full", rows[0][2])]


def test_fetched_at_strictly_increasing(tmp_store):
    w = Writer("t")
    for x in range(50):
        _add(w, x)
    stamps = [r["fetched_at"] for r in w.rows]
    assert all(b > a for a, b in zip(stamps, stamps[1:]))


def test_parts_split_by_month_and_close_keeps_writer_usable(tmp_store):
    w = Writer("t", max_buffer_mb=1000)
    _add(w, 1, date="2024-06-03")
    _add(w, 2, date="2024-06-20")
    _add(w, 3, date="2024-07-01")
    first = w.close()
    assert len(first) == 2
    assert len(month_files("t", 0.01, "2024-06-01")) == 1 and len(month_files("t", 0.01, "2024-07-01")) == 1
    _add(w, 4, date="2024-06-21")
    w.close()
    assert len(month_files("t", 0.01, "2024-06-01")) == 2 and sum(w.written.values()) == 4
