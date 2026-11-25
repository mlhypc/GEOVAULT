"""Offline tests for data folders with an unusual path. The path of every file goes into
a query as text, where a quote ends the text, `[`, `*` and `?` make it a pattern that
can match the files of another folder, and a folder named `key=value` turns into a
column. The files named have to be the files read."""

import datetime
import os

import numpy as np
import pyarrow.parquet as pq
import pytest
from rasterio.transform import Affine

from geovault import catalog, reader, store
from geovault.sources import chirps, era5, era5land
from geovault.sources.common import encode_blob

CRS, RES, PX = "EPSG:32636", 10, 16
X0, Y1 = 300000.0, 4100000.0
BOUNDS = (32.7, 37.0, 32.8, 37.1)

# (the data folder, a folder beside it that its path matches when read as a pattern)
FOLDERS = [("o'brien's data", None), ("v[1]", "v1"), ("band=other", None)]
if os.name != "nt":                         # Windows allows neither character in a name
    FOLDERS += [("v*", "v12"), ("v?", "v1")]


def use(root, monkeypatch):
    monkeypatch.setattr(store, "STORE", root / "store")
    monkeypatch.setattr(catalog, "STORE", root / "store")
    monkeypatch.setattr(catalog, "CATALOG", root / "catalog")


def add(w, value, date, x=0, scene="s"):
    arr = np.full((PX, PX), value, np.uint16)
    blob = encode_blob(arr, CRS, Affine(RES, 0, X0 + x * RES * PX, 0, -RES, Y1), 0)
    w.add("b", date, x, 0, CRS, RES, PX, BOUNDS, 0, None, scene, blob, arr.size)


def on_disk(root, dataset="q"):
    """(date, x, scene) of every stored row, read without DuckDB: what the files hold."""
    rows = []
    for f in sorted((root / "store" / f"dataset={dataset}").rglob("*.parquet")):
        t = pq.ParquetFile(f).read(columns=["date", "x", "scene_id"]).to_pylist()
        rows += [(r["date"], r["x"], r["scene_id"]) for r in t]
    return sorted(rows)


@pytest.mark.parametrize("name, neighbour", FOLDERS)
def test_the_files_of_the_data_folder_are_the_files_read(tmp_path, monkeypatch, name, neighbour):
    if neighbour:                           # a sealed month and a catalog of other tiles
        use(tmp_path / neighbour, monkeypatch)
        w = store.Writer("q")
        add(w, 99, "2026-01-01", x=5, scene="theirs")
        w.close()
        store.compact("q")
        catalog.rebuild("q")
        theirs = on_disk(tmp_path / neighbour)

    root = tmp_path / name
    use(root, monkeypatch)
    w = store.Writer("q")
    add(w, 7, "2026-01-01")
    w.flush()
    assert store.compact("q") == [(float(RES), "2026-01-01")]       # the sealed file
    catalog.rebuild("q")
    add(w, 8, "2026-01-02")
    w.close()
    mine = [("2026-01-01", 0, "s"), ("2026-01-02", 0, "s")]
    assert w.existing_keys(RES, ["2026-01"]) == {("b", d, CRS, x, 0) for d, x, _ in mine}
    assert store.compact("q") == [(float(RES), "2026-01-01")]
    assert on_disk(root) == mine                                    # nothing lost, nothing gained
    assert catalog.rebuild("q") == 2
    assert [r[:3] for r in catalog.coverage("q")] == [("2026-01-01", "b", 1), ("2026-01-02", "b", 1)]

    rows = reader.tiles_for_bbox("q", BOUNDS, band="b")
    assert [(r["band"], r["date"], r["scene_id"]) for r in rows] == [
        ("b", "2026-01-01", "s"), ("b", "2026-01-02", "s")]
    assert all(name in r["file"].replace("\\", "/").split("/") for r in rows)
    assert reader.read_tile(rows[1])[0][0, 0] == 8
    arr, _, crs = reader.mosaic("q", BOUNDS, "b", "2026-01-01")
    assert arr[0, 0] == 7 and crs == CRS
    assert store.reencode_month("q", RES, "2026-01-01")[0] == 2
    assert on_disk(root) == mine
    if neighbour:
        assert on_disk(tmp_path / neighbour) == theirs


@pytest.mark.parametrize("source", [chirps, era5, era5land])
def test_an_adapter_reads_its_catalog_in_such_a_folder(tmp_path, monkeypatch, source):
    use(tmp_path / "o'brien's v[1]", monkeypatch)
    w = store.Writer("m")
    w.add("b", "2026-01-01", 3, 4, source.CRS, 0.1, 10, (0, 0, 1, 1), 0, None,
          source.SCENE_PRELIM, b"x" * 100, 100)
    w.add("b", "2026-01-02", 3, 4, source.CRS, 0.1, 10, (0, 0, 1, 1), 0, None, "final", b"x" * 100, 100)
    w.close()
    catalog.rebuild("m")
    assert source._prelim_keys_in_store("m", datetime.date(2026, 1, 1)) == {
        ("b", "2026-01-01", source.CRS, 3, 4)}
    assert source._prelim_keys_in_store("m", datetime.date(2026, 1, 2)) == set()
