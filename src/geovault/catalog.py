"""Catalog: one small Parquet per dataset answering "what exists?"

Existence and coverage queries never touch pixel files (the same separation
Google Earth's BulkMetadata provides). Row = one stored tile: band, date, x, y,
WGS84 bbox, cloud_pct, scene_id, size, source file, crs, acq, valid_px. The store is the source of
truth; the catalog is a derived index, rebuilt after every ingest.
"""

from pathlib import Path

import duckdb

from .store import ROOT, STORE, scan

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
            "NULL file, NULL crs, NULL acq, NULL valid_px WHERE false) TO '{}' (FORMAT PARQUET)"
            .format(path(dataset).as_posix()))
        con.close()
        return 0
    con.execute(
        f"COPY (SELECT band, date, x, y, west, south, east, north, cloud_pct, "
        f"scene_id, size_bytes, filename AS file, crs, acq, valid_px "
        f"FROM {scan(files, filename=True)} "
        f"ORDER BY band, date, acq, crs, x, y) "
        f"TO '{path(dataset).as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    n = con.execute(
        f"SELECT count(*) FROM read_parquet('{path(dataset).as_posix()}')").fetchone()[0]
    con.close()
    return n


def lacks_acquisitions(dataset: str) -> bool:
    """True when the dataset holds tiles that do not name their acquisition: for a source
    whose tiles do, a store written before 0.9.0. Answered from the catalog; when there
    is none (a first ingest, or a crash before the first rebuild) from the store."""
    p = path(dataset)
    if not p.exists():
        from .store import has_rows_without_acq
        return has_rows_without_acq(dataset)
    con = duckdb.connect()
    src = f"read_parquet('{p.as_posix()}')"
    cols = {c[0] for c in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()}
    where = "WHERE acq IS NULL" if "acq" in cols else ""
    hit = con.execute(f"SELECT 1 FROM {src} {where} LIMIT 1").fetchone()
    con.close()
    return hit is not None


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
