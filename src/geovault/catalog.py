"""Catalog: one small Parquet per dataset answering "what exists?"

Existence and coverage queries never touch pixel files (the same separation
Google Earth's BulkMetadata provides). Row = one stored tile: band, date, x, y,
WGS84 bbox, cloud_pct, scene_id, size, source file, crs, acq, valid_px. The store is the source of
truth; the catalog is a derived index, rebuilt after every ingest.
"""

import math
import time
import uuid
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
    """Regenerate the dataset's catalog by scanning its store partitions. The catalog is
    written under a temporary name and put in place whole: a reader sees the catalog of
    before or the new one, and a rebuild that fails leaves the one of before. The name
    is this rebuild's own: two ingests of one dataset end at the same time more often
    than it seems (one process per band is how a slow source is fetched), and with a
    shared name each deleted or renamed the file the other was writing."""
    files = list((STORE / f"dataset={dataset}").rglob("*.parquet"))
    CATALOG.mkdir(parents=True, exist_ok=True)
    target = path(dataset)
    tmp = target.with_name(f"{target.name}.{uuid.uuid4().hex[:8]}.tmp")
    if files:
        rows = (f"SELECT band, date, x, y, west, south, east, north, cloud_pct, "
                f"scene_id, size_bytes, filename AS file, crs, acq, valid_px "
                f"FROM {scan(files, filename=True)} ORDER BY band, date, acq, crs, x, y")
    else:
        rows = ("SELECT NULL band, NULL date, NULL x, NULL y, NULL west, NULL south, "
                "NULL east, NULL north, NULL cloud_pct, NULL scene_id, NULL size_bytes, "
                "NULL file, NULL crs, NULL acq, NULL valid_px WHERE false")
    con = duckdb.connect()
    try:
        con.execute(f"COPY ({rows}) TO '{tmp.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        n = con.execute(f"SELECT count(*) FROM read_parquet('{tmp.as_posix()}')").fetchone()[0]
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        con.close()
    _put_in_place(tmp, target)
    return n


def _put_in_place(tmp, target, tries: int = 25) -> None:
    """Rename the finished catalog over the one in place. Windows refuses that while a
    reader has the catalog open; a query holds it for milliseconds, so the rename is
    tried again for a few seconds before it is given up."""
    for left in range(tries - 1, -1, -1):
        try:
            tmp.replace(target)
            return
        except PermissionError:
            if not left:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.2)


def coverage(dataset: str, bbox=None, date=None) -> list:
    """(date, band, tile count, mean cloud) summary; bbox = (w, s, e, n) in WGS84."""
    p = path(dataset)
    if not p.exists():
        return []
    con = duckdb.connect()
    where, args = ["1=1"], []
    if bbox:
        where.append(bbox_sql(bbox))
    if date:
        where.append("date LIKE ?")
        args.append(f"{date}%")
    rows = con.execute(
        f"SELECT date, band, count(*) tiles, round(avg(cloud_pct), 1) cloud "
        f"FROM read_parquet('{p.as_posix()}') WHERE {' AND '.join(where)} "
        f"GROUP BY date, band ORDER BY date, band", args).fetchall()
    con.close()
    return rows
