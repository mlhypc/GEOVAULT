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
import threading
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
        self.seen = {}      # (band, date, x, y) -> (row index, valid_px, scene_id) buffered this session
        self._lock = threading.Lock()   # add() is called from concurrent scene workers

    def add(self, band, date, x, y, crs, res, tile_px, wgs84_bounds, nodata,
            cloud_pct, scene_id, blob: bytes, valid_px: int | None = None) -> bool:
        """Buffer one tile. The same logical key can arrive twice in a run from two
        scenes of the same day (adjacent MGRS granules overlap at their edges, and
        the edge granule holds a partially empty tile): the tile with MORE valid
        pixels wins, whatever order the scenes were fetched in, so concurrent
        ingests are deterministic. Returns True when the buffer gained a new key."""
        key = (band, date, x, y)
        w, s, e, n = wgs84_bounds
        row = dict(
            band=band, date=date, x=x, y=y, crs=crs, res=float(res), tile_px=tile_px,
            west=w, south=s, east=e, north=n, nodata=nodata, cloud_pct=cloud_pct,
            scene_id=scene_id, data=blob, size_bytes=len(blob),
            fetched_at=int(time.time() * 1000),
        )
        with self._lock:
            prev = self.seen.get(key)
            if prev is not None:
                idx, prev_valid, prev_scene = prev
                better = (valid_px is not None and prev_valid is not None and valid_px > prev_valid)
                # equal coverage: the lexically smaller scene id wins, so the outcome never depends
                # on which worker finished first
                tie = (valid_px == prev_valid and scene_id is not None and prev_scene is not None
                       and str(scene_id) < str(prev_scene))
                if better or tie:
                    self.rows[idx] = row
                    self.seen[key] = (idx, valid_px, scene_id)
                return False
            self.seen[key] = (len(self.rows), valid_px, scene_id)
            self.rows.append(row)
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
        with self._lock:
            rows, self.rows, self.seen = self.rows, [], {}
        if not rows:
            return {}
        by_group = {}          # one part file per (res, MONTH): the store is partitioned by month,
        for r in rows:         # a part per date would mean dozens of tiny files per flush
            by_group.setdefault((r["res"], r["date"][:7] + "-01" if r["date"] else ""), []).append(r)

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
            # Small row groups (256 tiles, ~12 MB of blobs) over a band/x/y sort: a reader asking for one
            # band and a bbox touches a few row groups via their min/max stats instead of decompressing
            # the whole file. With one row group per file a 900 MB static layer cost 0.5 s per map tile.
            con.execute(
                f"COPY (SELECT * FROM buf ORDER BY band, date, x, y) "
                f"TO '{f.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 256)")
            written[f] = len(rows)
            touched.add((res, date))
        con.close()

        for res, date in touched:
            if len(month_files(self.dataset, res, date)) > AUTO_COMPACT_PARTS:
                # Skipped when another process is compacting the same month: two concurrent merges
                # each delete the parts the other is still reading (seen with 6 parallel static
                # ingests: one crashed in unlink, the store was left with an orphan .tmp). Parts
                # left behind are merged by the next close() or by `geovault compact`.
                compact_month(self.dataset, res, date, wait=False)
        return written


class _MonthLock:
    """Exclusive lock file next to the month's files. wait=False -> acquire() returns False at once
    when another process holds it; wait=True blocks (polling) until it is free or `timeout` passes."""

    def __init__(self, path, wait: bool, timeout: float = 600.0):
        self.path, self.wait, self.timeout, self.fd = path, wait, timeout, None

    def acquire(self) -> bool:
        import os, time
        t0 = time.time()
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return True
            except FileExistsError:
                if not self.wait or time.time() - t0 > self.timeout:
                    return False
                time.sleep(0.5)

    def release(self):
        import os
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass


def compact_month(dataset: str, res: float, date: str, wait: bool = True):
    """Merge a month's sealed file and parts into one file.
    Dedup: newest fetched_at wins. Safe to run any time, including as repair.
    One compaction per month at a time (lock file); wait=False returns None when it is busy."""
    target = month_dir(dataset, date) / f"r{int(res)}.parquet"
    lock = _MonthLock(target.with_suffix(".parquet.lock"), wait)
    if not lock.acquire():
        return None
    try:
        return _compact_month_locked(dataset, res, date, target)
    finally:
        lock.release()


def _compact_month_locked(dataset: str, res: float, date: str, target):
    files = month_files(dataset, res, date)
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
        f"TO '{tmp.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 256)")
    con.close()
    for f in files:
        f.unlink(missing_ok=True)
    tmp.replace(target)
    return target


def reencode_month(dataset: str, res: float, date: str, workers: int = 8, wait: bool = True):
    """Rewrite one (res, month) with every blob re-encoded by the current encoder
    (sources.common.reencode_blob): pixels, georeferencing and nodata stay bit-for-bit,
    only the compression choice changes. Merges open parts like compaction (newest
    fetched_at wins) and seals the month. Returns (rows, bytes_before, bytes_after), or
    None when the month is empty or locked by another process."""
    from concurrent.futures import ThreadPoolExecutor
    from .sources.common import reencode_blob
    target = month_dir(dataset, date) / f"r{int(res)}.parquet"
    lock = _MonthLock(target.with_suffix(".parquet.lock"), wait)
    if not lock.acquire():
        return None
    try:
        files = month_files(dataset, res, date)
        if not files:
            return None
        lst = ", ".join(f"'{f.as_posix()}'" for f in files)
        cols = ", ".join(COLS)
        con = duckdb.connect()
        rows = con.execute(
            f"SELECT {cols} FROM ("
            f"  SELECT {cols}, row_number() OVER "
            f"    (PARTITION BY band, date, x, y ORDER BY fetched_at DESC) rn"
            f"  FROM read_parquet([{lst}], union_by_name=true)) WHERE rn = 1 "
            f"ORDER BY band, date, x, y").fetchall()
        i_data, i_size = COLS.index("data"), COLS.index("size_bytes")
        before = sum(len(r[i_data]) for r in rows)
        with ThreadPoolExecutor(max_workers=workers) as ex:   # GDAL releases the GIL in codecs
            blobs = list(ex.map(lambda r: reencode_blob(bytes(r[i_data])), rows))
        out = []
        for r, b in zip(rows, blobs):
            r = list(r)
            r[i_data], r[i_size] = b, len(b)
            out.append(r)
        after = sum(len(b) for b in blobs)
        tmp = target.with_suffix(".parquet.tmp")
        con.execute(f"CREATE OR REPLACE TABLE buf ({SCHEMA})")
        con.executemany(f"INSERT INTO buf VALUES ({', '.join('?' for _ in COLS)})", out)
        con.execute(
            f"COPY (SELECT * FROM buf ORDER BY band, date, x, y) "
            f"TO '{tmp.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 256)")
        con.close()
        for f in files:
            f.unlink(missing_ok=True)
        tmp.replace(target)
        return len(rows), before, after
    finally:
        lock.release()


def months(dataset: str) -> list:
    """Every (res, date) group of a dataset, static as (res, '')."""
    import re
    out = set()
    for f in (STORE / f"dataset={dataset}").rglob("r*.parquet"):
        m = re.match(r"r(\d+)\.(?:part-[0-9a-f]+\.)?parquet$", f.name)
        if not m:
            continue
        rel = f.parent.relative_to(STORE / f"dataset={dataset}").as_posix()
        date = "" if rel == "static" else rel.replace("/", "-") + "-01"
        out.add((float(m.group(1)), date))
    return sorted(out, key=lambda k: (k[1], k[0]))


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
