"""Catalog: one small Parquet per dataset answering "what exists?"

Existence and coverage queries never touch pixel files (the same separation
Google Earth's BulkMetadata provides). Row = one stored tile: band, date, x, y,
WGS84 bbox, cloud_pct, scene_id, size, source file, crs, acq, valid_px. The store is the source of
truth; the catalog is a derived index, rebuilt after every ingest.
"""

import math
from pathlib import Path

import duckdb

from .store import ROOT, STORE, scan

CATALOG = ROOT / "catalog"


def path(dataset: str) -> Path:
    return CATALOG / f"{dataset}.parquet"


def bbox_sql(bbox) -> str:
    """SQL test on catalog rows: does the tile touch the WGS84 bbox (w, s, e, n)? A row
    with west > east is a tile across the antimeridian (see common.wgs84_bounds). The
    bbox itself may not cross it: one with west > east is refused, since it would match
    nothing."""
    w, s, e, n = bbox
    if not (all(math.isfinite(v) for v in bbox) and w <= e and s <= n):
        raise ValueError(f"bbox {tuple(bbox)} is not (west, south, east, north) with west <= east "
                         f"and south <= north. A bbox across the antimeridian is not supported: "
                         f"ask for its two parts")
    return (f"north > {s} AND south < {n} AND CASE WHEN west <= east "
            f"THEN east > {w} AND west < {e} ELSE east > {w} OR west < {e} END")


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


def coverage(dataset: str, bbox=None, date=None) -> list:
    """(date, band, tile count, mean cloud) summary; bbox = (w, s, e, n) in WGS84."""
    p = path(dataset)
    if not p.exists():
        return []
    con = duckdb.connect()
    where = ["1=1"]
    if bbox:
        where.append(bbox_sql(bbox))
    if date:
        where.append(f"date LIKE '{date}%'")
    rows = con.execute(
        f"SELECT date, band, count(*) tiles, round(avg(cloud_pct), 1) cloud "
        f"FROM read_parquet('{p.as_posix()}') WHERE {' AND '.join(where)} "
        f"GROUP BY date, band ORDER BY date, band").fetchall()
    con.close()
    return rows
