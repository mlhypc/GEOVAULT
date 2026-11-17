"""Offline tests for reader.series on edge cases of the catalog: the same tile numbers in two
UTM zones, and a stale catalog."""

import os

import duckdb
import numpy as np
import pytest
from pyproj import Transformer
from rasterio.transform import Affine

from geovault import catalog, reader, store
from geovault.sources.common import encode_blob

POINT = (27.0, 38.1)


@pytest.fixture
def two_zones(tmp_path, monkeypatch):
    """Two dates, each stored as tile (1, 2) in zone 35 (value 35) and zone 36 (value 36),
    both covering POINT, in one month file."""
    monkeypatch.setattr(store, "STORE", tmp_path / "store")
    monkeypatch.setattr(catalog, "STORE", tmp_path / "store")
    monkeypatch.setattr(catalog, "CATALOG", tmp_path / "catalog")
    w = store.Writer("edge")
    for crs, val in (("EPSG:32635", 35), ("EPSG:32636", 36)):
        x, y = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(*POINT)
        blob = encode_blob(np.full((10, 10), val, np.int16), crs, Affine(10, 0, x - 50, 0, -10, y + 50), -1)
        for d in ("2026-01-01", "2026-01-02"):
            w.add("b", d, 1, 2, crs, 10, 10, (26.9, 38.0, 27.1, 38.2), -1, None, "s", blob, 100)
    w.close()
    catalog.rebuild("edge")
    return catalog.path("edge").as_posix()


def _rewrite(path, select):
    con = duckdb.connect()
    con.execute(f"COPY ({select} FROM read_parquet('{path}')) TO '{path}.tmp' (FORMAT PARQUET)")
    con.close()
    os.replace(path + ".tmp", path)


def values(rows):
    return [(r["date"], r["value"]) for r in rows]


def test_series_keeps_both_zones(two_zones):
    assert values(reader.series("edge", *POINT, "b")) == [
        ("2026-01-01", 35), ("2026-01-01", 36), ("2026-01-02", 35), ("2026-01-02", 36)]


def test_series_on_a_stale_catalog_raises(two_zones):
    _rewrite(two_zones, "SELECT * REPLACE (CASE WHEN date = '2026-01-02' THEN '2026-01-03' ELSE date END AS date)")
    with pytest.raises(RuntimeError, match="rebuild the catalog"):
        reader.series("edge", *POINT, "b")
