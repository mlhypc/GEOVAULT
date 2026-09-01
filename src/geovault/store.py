"""Tile store: month-granular Hive-partitioned Parquet.

One Parquet file per (dataset, year, month, resolution group); each row is one
tile with its pixels as a DEFLATE GeoTIFF blob. Ingest appends cheap immutable
part files; a month is sealed into a single file by compaction. Deduplication is
on the logical key (band, date, x, y), newest fetched_at wins. Rows are sorted
(band, date, x, y) so DuckDB row-group statistics prune reads.

Layout:
  data/store/dataset=<d>/<YYYY>/<MM>/r<res>.parquet          (sealed month)
  data/store/dataset=<d>/<YYYY>/<MM>/r<res>.part-<id>.parquet (open parts)
  data/store/dataset=<d>/static/r<res>.parquet               (static layers)

Design notes (from reverse-engineering Google Earth's tile system):
  few big immutable files instead of thousands of small ones, and a separate
  catalog (catalog.py) so readers never glob the store to ask what exists.
"""

import os
import time
import uuid
from pathlib import Path

import duckdb

ROOT = Path(os.environ.get("GEOVAULT_DATA", Path(__file__).resolve().parents[2] / "data"))
STORE = ROOT / "store"

SCHEMA = """
    band        VARCHAR,
    date        VARCHAR,
    x           BIGINT,
    y           BIGINT,
    crs         VARCHAR,
    res         DOUBLE,
    tile_px     INTEGER,
    west        DOUBLE, south DOUBLE, east DOUBLE, north DOUBLE,
    nodata      DOUBLE,
    cloud_pct   DOUBLE,
    scene_id    VARCHAR,
    data        BLOB,
    size_bytes  BIGINT,
    fetched_at  BIGINT
"""
COLS = ["band", "date", "x", "y", "crs", "res", "tile_px",
        "west", "south", "east", "north", "nodata", "cloud_pct",
        "scene_id", "data", "size_bytes", "fetched_at"]

# A month is auto-compacted at close() once it spans more than this many files.
# Ingest stays a cheap append (new part file); compaction is maintenance.
AUTO_COMPACT_PARTS = 3


def month_dir(dataset: str, date: str) -> Path:
    part = "static" if not date else f"{date[:4]}/{date[5:7]}"
    return STORE / f"dataset={dataset}" / part


def month_files(dataset: str, res: float, date: str) -> list:
    """All files holding a (dataset, res, month): sealed file plus open parts."""
    d = month_dir(dataset, date)
    return sorted(d.glob(f"r{int(res)}.parquet")) + sorted(d.glob(f"r{int(res)}.part-*.parquet"))


class Writer:
    """Buffers rows in memory; close() appends them as new part files."""

    def __init__(self, dataset: str):
        self.dataset = dataset
        self.rows = []
        self.seen = set()   # (band, date, x, y) buffered this session

    def add(self, band, date, x, y, crs, res, tile_px, wgs84_bounds, nodata,
            cloud_pct, scene_id, blob: bytes) -> bool:
        key = (band, date, x, y)
        if key in self.seen:
            return False
        self.seen.add(key)
        w, s, e, n = wgs84_bounds
        self.rows.append(dict(
            band=band, date=date, x=x, y=y, crs=crs, res=float(res), tile_px=tile_px,
            west=w, south=s, east=e, north=n, nodata=nodata, cloud_pct=cloud_pct,
            scene_id=scene_id, data=blob, size_bytes=len(blob),
            fetched_at=int(time.time() * 1000),
        ))
        return True

    def existing_keys(self, res: float, months: list) -> set:
        """(band, date, x, y) already stored for this dataset/res in the given months."""
        files = []
        for m in months:
            files += month_files(self.dataset, res, (m + "-01") if m else "")
        if not files:
            return set()
        con = duckdb.connect()
        lst = ", ".join(f"'{f.as_posix()}'" for f in files)
        rows = con.execute(
            f"SELECT band, date, x, y FROM read_parquet([{lst}], union_by_name=true)").fetchall()
        con.close()
        return set(rows)

    def close(self) -> dict:
        """Append buffered rows as new part files, then auto-compact any
        (res, month) that has grown past AUTO_COMPACT_PARTS."""
        if not self.rows:
            return {}
        by_group = {}
        for r in self.rows:
            by_group.setdefault((r["res"], r["date"]), []).append(r)

        written = {}
        con = duckdb.connect()
        touched = set()
        for (res, date), rows in by_group.items():
            d = month_dir(self.dataset, date)
            d.mkdir(parents=True, exist_ok=True)
            f = d / f"r{int(res)}.part-{uuid.uuid4().hex[:8]}.parquet"
            con.execute(f"CREATE OR REPLACE TABLE buf ({SCHEMA})")
            con.executemany(
                f"INSERT INTO buf VALUES ({', '.join('?' for _ in COLS)})",
                [[r[c] for c in COLS] for r in rows],
            )
            con.execute(
                f"COPY (SELECT * FROM buf ORDER BY band, date, x, y) "
                f"TO '{f.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)")
            written[f] = len(rows)
            touched.add((res, date))
        con.close()
        self.rows = []

        for res, date in touched:
            if len(month_files(self.dataset, res, date)) > AUTO_COMPACT_PARTS:
                compact_month(self.dataset, res, date)
        return written


def compact_month(dataset: str, res: float, date: str):
    """Merge a month's sealed file and parts into one file.
    Dedup: newest fetched_at wins. Safe to run any time, including as repair."""
    files = month_files(dataset, res, date)
    target = month_dir(dataset, date) / f"r{int(res)}.parquet"
    if not files:
        return None
    if len(files) == 1:
        # A lone part file (typical for static layers after their first ingest)
        # needs no merge; sealing it is just a rename.
        if files[0] != target:
            files[0].replace(target)
            return target
        return None
    tmp = target.with_suffix(".parquet.tmp")
    lst = ", ".join(f"'{f.as_posix()}'" for f in files)
    cols = ", ".join(COLS)
    con = duckdb.connect()
    # Columns are selected explicitly so stray helper columns in old files or
    # columns added in future versions never break the merge.
    con.execute(
        f"COPY (SELECT {cols} FROM ("
        f"  SELECT {cols}, row_number() OVER "
        f"    (PARTITION BY band, date, x, y ORDER BY fetched_at DESC) rn"
        f"  FROM read_parquet([{lst}], union_by_name=true)) WHERE rn = 1 "
        f"ORDER BY band, date, x, y) "
        f"TO '{tmp.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    con.close()
    for f in files:
        f.unlink()
    tmp.replace(target)
    return target


def compact(dataset: str) -> list:
    """Compact every (res, month) of a dataset that has open part files."""
    import re
    done = []
    for f in (STORE / f"dataset={dataset}").rglob("r*.part-*.parquet"):
        m = re.match(r"r(\d+)\.part-", f.name)
        rel = f.parent.relative_to(STORE / f"dataset={dataset}").as_posix()
        date = "" if rel == "static" else rel.replace("/", "-") + "-01"
        key = (float(m.group(1)), date)
        if key not in done:
            compact_month(dataset, *key)
            done.append(key)
    return done
