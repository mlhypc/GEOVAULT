"""Offline tests for catalog.rebuild: the catalog is put in place whole, and a rebuild that
fails leaves the catalog of before."""

import duckdb
import pytest

from geovault import catalog, store
from geovault.store import Writer


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE", tmp_path / "store")
    monkeypatch.setattr(catalog, "STORE", tmp_path / "store")
    monkeypatch.setattr(catalog, "CATALOG", tmp_path / "catalog")
    return tmp_path


def add(writer, x, date="2026-01-01"):
    writer.add("B", date, x, 0, "EPSG:4326", 0.01, 10, (0, 0, 1, 1), 0, None, "s", b"x" * 100, 100)


def rows(dataset):
    con = duckdb.connect()
    out = con.execute(f"SELECT x FROM read_parquet('{catalog.path(dataset).as_posix()}') "
                      f"ORDER BY x").fetchall()
    con.close()
    return [r[0] for r in out]


def test_rebuild_leaves_one_file(vault):
    w = Writer("t")
    add(w, 1)
    add(w, 2)
    w.close()
    assert catalog.rebuild("t") == 2 and rows("t") == [1, 2]
    assert [f.name for f in (vault / "catalog").iterdir()] == ["t.parquet"]


def test_rebuild_of_an_empty_dataset(vault):
    assert catalog.rebuild("none") == 0 and rows("none") == []
    assert [f.name for f in (vault / "catalog").iterdir()] == ["none.parquet"]


def test_a_failed_rebuild_keeps_the_catalog_of_before(vault):
    w = Writer("t")
    add(w, 1)
    w.close()
    catalog.rebuild("t")
    (vault / "store" / "dataset=t" / "2026" / "02").mkdir(parents=True)
    (vault / "store" / "dataset=t" / "2026" / "02" / "r0.parquet").write_bytes(b"not parquet")
    with pytest.raises(duckdb.Error):
        catalog.rebuild("t")
    assert rows("t") == [1]
    assert [f.name for f in (vault / "catalog").iterdir()] == ["t.parquet"]


def test_a_rebuild_leaves_the_file_of_another_rebuild_alone(vault, monkeypatch):
    """Two ingests of one dataset can end at the same moment, each rebuilding the catalog:
    every rebuild writes under a name of its own."""
    w = Writer("t")
    add(w, 1)
    w.close()
    names = []
    connect = duckdb.connect

    class Watched:
        def __init__(self):
            self.con = connect()

        def execute(self, sql, *args):
            if sql.startswith("COPY"):
                names.append(sql.split(" TO '")[1].split("'")[0].rsplit("/", 1)[-1])
            return self.con.execute(sql, *args)

        def close(self):
            self.con.close()

    other = vault / "catalog" / "t.parquet.0a1b2c3d.tmp"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"half of the catalog another rebuild is writing")
    with monkeypatch.context() as watching:
        watching.setattr(catalog.duckdb, "connect", Watched)
        assert catalog.rebuild("t") == 1 and catalog.rebuild("t") == 1
    assert len(set(names)) == 2 and all(n.startswith("t.parquet.") and n.endswith(".tmp") for n in names)
    assert sorted(f.name for f in (vault / "catalog").iterdir()) == ["t.parquet", other.name]
    assert other.read_bytes().startswith(b"half")


def test_a_band_or_date_is_a_value_not_a_piece_of_the_query(vault):
    from geovault import reader
    w = Writer("t")
    w.add("it's", "2026-01-01", 1, 0, "EPSG:4326", 0.01, 10, (0, 0, 1, 1), 0, None, "s", b"x" * 100, 100)
    add(w, 2)
    w.close()
    catalog.rebuild("t")
    assert [r["x"] for r in reader.tiles_for_bbox("t", (0, 0, 1, 1), band="it's")] == [1]
    assert reader.tiles_for_bbox("t", (0, 0, 1, 1), band="B", date="2026' OR '1'='1") == []
    assert [r["x"] for r in reader.tiles_for_bbox("t", (0, 0, 1, 1), date="2026-01")] == [2, 1]
    assert catalog.coverage("t", date="2026' OR '1'='1") == []
    assert len(catalog.coverage("t", date="2026")) == 2


def test_rebuild_waits_for_a_reader_that_holds_the_catalog(vault, monkeypatch):
    """Windows does not rename over a file a reader has open: the rename is tried again."""
    w = Writer("t")
    add(w, 1)
    w.close()
    catalog.rebuild("t")
    add(w, 2)
    w.close()
    held = [3]
    replace = type(catalog.path("t")).replace

    def busy(self, target):
        if held[0]:
            held[0] -= 1
            raise PermissionError("held by a reader")
        return replace(self, target)

    monkeypatch.setattr(type(catalog.path("t")), "replace", busy)
    monkeypatch.setattr(catalog.time, "sleep", lambda s: None)
    assert catalog.rebuild("t") == 2 and rows("t") == [1, 2]


def test_rebuild_gives_up_on_a_catalog_that_stays_held(vault, monkeypatch):
    w = Writer("t")
    add(w, 1)
    w.close()
    catalog.rebuild("t")
    add(w, 2)
    w.close()

    def busy(self, target):
        raise PermissionError("held by a reader")

    monkeypatch.setattr(type(catalog.path("t")), "replace", busy)
    monkeypatch.setattr(catalog.time, "sleep", lambda s: None)
    with pytest.raises(PermissionError):
        catalog.rebuild("t")
    monkeypatch.undo()
    monkeypatch.setattr(catalog, "CATALOG", vault / "catalog")
    assert rows("t") == [1]
    assert [f.name for f in (vault / "catalog").iterdir()] == ["t.parquet"]
