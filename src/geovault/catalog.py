"""Catalog: one small Parquet per dataset answering "what exists?"

Existence and coverage queries never touch pixel files (the same separation
Google Earth's BulkMetadata provides). Row = one stored tile: band, date, x, y,
WGS84 bbox, cloud_pct, scene_id, size, source file. The store is the source of
truth; the catalog is a derived index, rebuilt after every ingest.
"""

from pathlib import Path

import duckdb

from .store import ROOT, STORE

CATALOG = ROOT / "catalog"


def path(dataset: str) -> Path:
    return CATALOG / f"{dataset}.parquet"


def rebuild(dataset: str) -> int:
    """Regenerate the dataset's catalog by scanning its store partitions."""
    files = list((STORE / f"dataset={dataset}").rglob("*.parquet"))
    CATALOG.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    if not files:
        con.execute(
            "COPY (SELECT NULL band, NULL date, NULL x, NULL y, NULL west, NULL south, "
            "NULL east, NULL north, NULL cloud_pct, NULL scene_id, NULL size_bytes, "
            "NULL file WHERE false) TO '{}' (FORMAT PARQUET)".format(path(dataset).as_posix()))
        con.close()
        return 0
    lst = ", ".join(f"'{f.as_posix()}'" for f in files)
    con.execute(
        f"COPY (SELECT band, date, x, y, west, south, east, north, cloud_pct, "
        f"scene_id, size_bytes, filename AS file "
        f"FROM read_parquet([{lst}], filename=true, union_by_name=true) "
        f"ORDER BY band, date, x, y) "
        f"TO '{path(dataset).as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    n = con.execute(
        f"SELECT count(*) FROM read_parquet('{path(dataset).as_posix()}')").fetchone()[0]
    con.close()
    return n


def coverage(dataset: str, bbox=None, date=None) -> list:
    """(date, band, tile count, mean cloud) summary; bbox = (w, s, e, n) in WGS84."""
    p = path(dataset)
    if not p.exists():
        return []
    con = duckdb.connect()
    where = ["1=1"]
    if bbox:
        w, s, e, n = bbox
        where.append(f"east > {w} AND west < {e} AND north > {s} AND south < {n}")
    if date:
        where.append(f"date LIKE '{date}%'")
    rows = con.execute(
        f"SELECT date, band, count(*) tiles, round(avg(cloud_pct), 1) cloud "
        f"FROM read_parquet('{p.as_posix()}') WHERE {' AND '.join(where)} "
        f"GROUP BY date, band ORDER BY date, band").fetchall()
    con.close()
    return rows
