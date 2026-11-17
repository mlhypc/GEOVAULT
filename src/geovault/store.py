"""Tile store: month-granular Hive-partitioned Parquet.

One Parquet file per (dataset, year, month, resolution group); each row is one
tile with its pixels as a DEFLATE GeoTIFF blob. Ingest appends cheap immutable
part files; a month is sealed into a single file by compaction. The logical key is
(band, date, acq, crs, x, y). `acq` names the acquisition (one overpass of one
satellite, within the date) for sources that can image the same ground twice a day,
and is NULL for all others. Compaction resolves rows that share a key: scenes of one acquisition
that each hold part of a tile are merged into one tile; anything else is a re-fetch
and the newest fetched_at wins. Rows are sorted (band, date, x, y) and cut into
small row groups (about GROUP_BYTES of tiles each), so a read that names a band, a
date or a tile fetches the few groups their min/max statistics leave.

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
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

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
    fetched_at  BIGINT,
    acq         VARCHAR,
    valid_px    INTEGER
"""
COLS = ["band", "date", "x", "y", "crs", "res", "tile_px",
        "west", "south", "east", "north", "nodata", "cloud_pct",
        "scene_id", "data", "size_bytes", "fetched_at", "acq", "valid_px"]
KEY = "band, date, acq, crs, x, y"

# A row group ends once it holds this many bytes of tiles, or this many tiles. A reader
# fetches whole groups: with one tile of interest in a group of 2048 (the smallest
# group DuckDB's writer makes) a 211 KB tile cost 112 ms to read, in a 4 MB group 5 ms.
# Smaller groups cost more than they save: the footer, read on every query, grows.
GROUP_BYTES = 4_000_000
GROUP_ROWS = 2048

# The columns rows are found by carry min/max statistics per row group. Not the tile
# bytes, and not nodata or cloud_pct: pyarrow leaves NaN out of a column's min/max and
# DuckDB then skips a group whose only match is a NaN.
STAT_COLS = ["band", "date", "x", "y", "crs", "acq", "scene_id", "west", "south", "east", "north"]

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


def scan(files, filename: bool = False) -> str:
    """SQL source over a list of store files; filename: add the path of each row's file."""
    lst = ", ".join(f"'{Path(f).as_posix()}'" for f in files)
    return f"read_parquet([{lst}]{', filename=true' if filename else ''})"


def _row_groups(batches):
    """Cut a stream of record batches into tables of about GROUP_BYTES of tiles, at
    most GROUP_ROWS rows each: the row groups of a store file."""
    held, rows, size = [], 0, 0
    for batch in batches:
        start = 0
        for end, nbytes in enumerate(pc.binary_length(batch.column("data")).to_pylist(), 1):
            rows += 1
            size += nbytes or 0
            if size >= GROUP_BYTES or rows >= GROUP_ROWS:
                yield pa.Table.from_batches(held + [batch.slice(start, end - start)])
                held, rows, size, start = [], 0, 0, end
        if start < batch.num_rows:
            held.append(batch.slice(start))
    if held:
        yield pa.Table.from_batches(held)


def _write_sorted(con, rows_sql: str, path) -> None:
    """Write the rows of a query as one store file: sorted (band, date, x, y), ZSTD,
    in row groups sized by _row_groups. pyarrow writes the file because DuckDB's COPY
    cannot make a row group smaller than 2048 rows. Statistics go on STAT_COLS only and
    the tile bytes are not dictionary encoded. A write that fails leaves a file holding
    the groups written so far, so callers write to a temporary name and rename."""
    reader = con.execute(
        f"SELECT * FROM ({rows_sql}) ORDER BY band, date, x, y").to_arrow_reader(256)
    with pq.ParquetWriter(path, reader.schema, compression="zstd", compression_level=3,
                          write_statistics=STAT_COLS,
                          use_dictionary=[c for c in COLS if c != "data"]) as out:
        for group in _row_groups(reader):
            out.write_table(group, row_group_size=group.num_rows)


def _slot(row) -> tuple:
    """Buffer slot of a row: the tile key, plus the scene for a tile of an acquisition."""
    key = (row["band"], row["date"], row["acq"], row["crs"], row["x"], row["y"])
    return key if row["acq"] is None else key + (row["scene_id"],)


class Writer:
    """Buffers rows in memory and appends them to the store as new part files.

    The buffer is flushed to disk on its own once it holds more than `max_buffer_mb` of tile
    blobs (a Turkey-wide WorldCover ingest once sat on 900 MB of RAM for two hours and would
    have lost all of it to a crash), at every explicit flush(), and at close(). close() also
    auto-compacts the (res, month) groups that have grown past AUTO_COMPACT_PARTS."""

    def __init__(self, dataset: str, max_buffer_mb: float = 256.0, log=None):
        self.dataset = dataset
        self.max_buffer_bytes = int(max_buffer_mb * 1e6)
        self.log = log                      # callable taking one line, e.g. logger.info
        self.rows = []
        self.buffered_bytes = 0             # blob bytes in self.rows
        # buffer slot -> (row index or None when flushed, valid_px, scene_id); a slot is the
        # tile key, plus the scene for a tile of an acquisition
        self.seen = {}
        self.written = {}   # part file -> rows, everything this Writer wrote to disk
        self.touched = set()                # (res, month date) groups with a part written by us
        self.acq_months = set()             # those of them that got tiles of an acquisition
        self.unmerged = set()               # acq months closed with merge=False, still to merge
        self._last_fetched_at = 0
        self._lock = threading.Lock()       # add() is called from concurrent scene workers
        self._flush_lock = threading.Lock() # one flush at a time; others keep buffering meanwhile

    def add(self, band, date, x, y, crs, res, tile_px, wgs84_bounds, nodata,
            cloud_pct, scene_id, blob: bytes, valid_px: int | None = None,
            acq: str | None = None) -> bool:
        """Buffer one tile. valid_px is the number of pixels that hold an observation.

        acq names the acquisition for sources that can image the same ground twice a day.
        Tiles of one acquisition from different scenes (adjacent granules or slices that
        each hold part of the tile) are all kept, one row per scene, and merged into one
        tile when the month is compacted.

        Without acq, and for the same scene arriving twice, the same key is the same
        tile: the one with MORE valid pixels wins, whatever order they were fetched in,
        so concurrent ingests are deterministic. A better tile for a key that was
        already flushed is appended again; compaction keeps the newest fetched_at,
        which is this one. Returns True when the buffer gained a new row."""
        w, s, e, n = wgs84_bounds
        row = dict(
            band=band, date=date, x=x, y=y, crs=crs, res=float(res), tile_px=tile_px,
            west=w, south=s, east=e, north=n, nodata=nodata, cloud_pct=cloud_pct,
            scene_id=scene_id, data=blob, size_bytes=len(blob), fetched_at=0,
            acq=acq, valid_px=valid_px,
        )
        key = _slot(row)
        with self._lock:
            # strictly increasing within the process, so "newest fetched_at wins" at compaction
            # means "last added wins" even for tiles added in the same millisecond
            self._last_fetched_at = max(int(time.time() * 1000), self._last_fetched_at + 1)
            row["fetched_at"] = self._last_fetched_at
            prev = self.seen.get(key)
            if prev is not None:
                idx, prev_valid, prev_scene = prev
                better = (valid_px is not None and prev_valid is not None and valid_px > prev_valid)
                # equal coverage: the lexically smaller scene id wins, so the outcome never depends
                # on which worker finished first
                tie = (valid_px == prev_valid and scene_id is not None and prev_scene is not None
                       and str(scene_id) < str(prev_scene))
                if better or tie:
                    if idx is None:                 # the loser is already on disk
                        self.seen[key] = (len(self.rows), valid_px, scene_id)
                        self.rows.append(row)
                        self.buffered_bytes += len(blob)
                    else:
                        self.buffered_bytes += len(blob) - self.rows[idx]["size_bytes"]
                        self.rows[idx] = row
                        self.seen[key] = (idx, valid_px, scene_id)
                return False
            self.seen[key] = (len(self.rows), valid_px, scene_id)
            self.rows.append(row)
            self.buffered_bytes += len(blob)
            full = self.buffered_bytes >= self.max_buffer_bytes
        if full:
            self.flush()
        return True

    def flush(self) -> dict:
        """Write the buffered rows as new part files (one per (res, month)). Returns
        {part file: rows} for this flush; nothing is compacted here. When a part cannot
        be written the other months are still written, the rows that are not on disk go
        back into the buffer for the next flush, and the first error is raised."""
        with self._flush_lock:
            with self._lock:
                rows, self.rows = self.rows, []
                nbytes, self.buffered_bytes = self.buffered_bytes, 0
                for key, (idx, valid_px, scene_id) in self.seen.items():
                    if idx is not None:
                        self.seen[key] = (None, valid_px, scene_id)
            if not rows:
                return {}
            groups = {}        # one part file per (res, MONTH): the store is partitioned by month,
            for r in rows:     # a part per date would mean dozens of tiny files per flush
                groups.setdefault((r["res"], r["date"][:7] + "-01" if r["date"] else ""), []).append(r)
            written, failure, con = {}, None, None
            try:
                con = duckdb.connect()
                for res, date in list(groups):
                    try:
                        f = self._write_part(con, res, date, groups[(res, date)])
                    except Exception as e:
                        failure = failure or e
                        continue
                    written[f] = len(groups.pop((res, date)))
                    self.written[f] = written[f]
                    if self.log:
                        self.log(f"  flushed {written[f]} rows ({nbytes / 1e6:.0f} MB) to {f.name}")
            finally:
                if con is not None:
                    con.close()
                self._put_back([r for group in groups.values() for r in group])
            if failure is not None:
                raise failure
        return written

    def _write_part(self, con, res, date, group) -> Path:
        """One new part file holding `group`, the rows of one (res, month)."""
        d = month_dir(self.dataset, date)
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"r{int(res)}.part-{uuid.uuid4().hex[:8]}.parquet"
        con.execute(f"CREATE OR REPLACE TABLE buf ({SCHEMA})")
        con.executemany(
            f"INSERT INTO buf VALUES ({', '.join('?' for _ in COLS)})",
            [[r[c] for c in COLS] for r in group],
        )
        # Parts get the sealed layout too: a lone part is sealed by a rename. Written
        # under a temporary name, so a part exists whole or not at all.
        tmp = f.with_suffix(".parquet.tmp")
        try:
            _write_sorted(con, "SELECT * FROM buf", tmp)
            tmp.replace(f)
        except BaseException:
            _discard(tmp)
            raise
        self.touched.add((res, date))
        if any(r["acq"] is not None for r in group):
            self.acq_months.add((res, date))
        return f

    def _put_back(self, rows) -> None:
        """Rows a failed flush did not write go back into the buffer. A slot that got a
        fuller tile while the flush ran keeps that one."""
        with self._lock:
            for row in rows:
                key = _slot(row)
                idx, valid_px, scene_id = self.seen[key]
                if idx is None:
                    self.seen[key] = (len(self.rows), valid_px, scene_id)
                    self.rows.append(row)
                    self.buffered_bytes += row["size_bytes"]

    def existing_keys(self, res: float, months: list) -> set:
        """(band, date, crs, x, y) already stored for this dataset/res in the given months.
        The CRS is part of the key: UTM tile numbers restart in every zone, so (x, y)
        alone names different ground in different zones."""
        files = []
        for m in months:
            files += month_files(self.dataset, res, (m + "-01") if m else "")
        if not files:
            return set()
        con = duckdb.connect()
        rows = con.execute(f"SELECT band, date, crs, x, y FROM {scan(files)}").fetchall()
        con.close()
        return set(rows)

    def existing_scenes(self, res: float, months: list) -> dict:
        """The stored tiles of a source with acquisitions, for this dataset/res in the
        given months: (band, date, acq, crs, x, y) -> the scenes the tile already holds,
        or None for a tile without an empty pixel, which no further scene can add to."""
        files = []
        for m in months:
            files += month_files(self.dataset, res, (m + "-01") if m else "")
        if not files:
            return {}
        con = duckdb.connect()
        rows = con.execute(
            f"SELECT band, date, acq, crs, x, y, scene_id, valid_px, tile_px FROM {scan(files)}"
        ).fetchall()
        con.close()
        out = {}
        for *key, scenes, valid_px, tile_px in rows:
            key = tuple(key)
            if valid_px is not None and valid_px >= tile_px * tile_px:
                out[key] = None
            elif key not in out or out[key] is not None:    # several rows until compacted
                held = frozenset(s for s in (scenes or "").split("|") if s)
                out[key] = out.get(key, frozenset()) | held
        return out

    def close(self, merge: bool = True) -> dict:
        """Flush, then compact any touched (res, month) that has grown past
        AUTO_COMPACT_PARTS or, with `merge`, holds a tile of an acquisition in several
        rows. merge=False leaves those rows for a later compaction (the next close()
        with merge, or store.compact); a caller that closes after every chunk of a long
        run passes it and compacts once at the end, since a compaction rewrites the
        month. A month another process is compacting right now is skipped; its rows are
        merged by the next compaction. Returns {part file: rows} of the final flush
        only; the whole session's output is in self.written. The Writer stays usable
        after close() (the dated ingest path closes it after every chunk of scenes)."""
        written = self.flush()
        with self._lock:
            touched, self.touched = self.touched, set()
            self.unmerged |= self.acq_months
            self.acq_months = set()
            split = set()
            if merge:
                split, self.unmerged = self.unmerged, set()
        for res, date in touched | split:
            # Skipped when another process is compacting the same month: two concurrent merges
            # each delete the parts the other is still reading (seen with 6 parallel static
            # ingests: one crashed in unlink, the store was left with an orphan .tmp). Parts
            # left behind are merged by a later compaction (`geovault compact`), and a month
            # owed a merge stays owed to the next close().
            if len(month_files(self.dataset, res, date)) > AUTO_COMPACT_PARTS:
                done = compact_month(self.dataset, res, date, wait=False)
            elif (res, date) in split:
                # the check for split tiles reads the month's files, so it runs under the
                # month lock: another process's compaction can delete them meanwhile
                done = compact_month(self.dataset, res, date, wait=False, only_split=True)
            else:
                continue
            if done is False and (res, date) in split:
                with self._lock:
                    self.unmerged.add((res, date))
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


def _has_split_tiles(files) -> bool:
    """True when a tile of an acquisition is stored in more than one row."""
    if not files:
        return False
    con = duckdb.connect()
    hit = con.execute(f"SELECT 1 FROM {scan(files)} WHERE acq IS NOT NULL "
                      f"GROUP BY {KEY} HAVING count(*) > 1 LIMIT 1").fetchone()
    con.close()
    return hit is not None


def _resolved(src: str) -> str:
    """SQL over store rows with re-fetched tiles resolved: one row per tile, or per
    (tile, scene) for a tile of an acquisition, the newest fetched_at each. Copies of
    one age (two processes writing in the same millisecond, a part left beside the month
    that already holds it) are told apart as Writer.add does it: the fuller one, then
    the smaller scene id; then the smaller blob, which is a re-encoded tile next to its
    old encoding. Columns are selected by name, so the hive column DuckDB adds for
    `dataset=` stays out."""
    cols = ", ".join(COLS)
    return (f"(SELECT {cols} FROM (SELECT {cols}, row_number() OVER (PARTITION BY {KEY}, "
            f"CASE WHEN acq IS NULL THEN NULL ELSE scene_id END "
            f"ORDER BY fetched_at DESC, valid_px DESC NULLS LAST, scene_id, size_bytes) rn "
            f"FROM {src}) WHERE rn = 1)")


def _merge_split_tiles(con, src: str, workers: int = 8) -> None:
    """Fill the table `merged` with the tiles of an acquisition that `src` holds in
    several rows: the scenes that each hold part of the tile, merged into one row
    (common.merge_tiles). Rows that cannot be merged (a blob that does not decode, or
    tiles of different size or type under one key) are passed through as they are, so
    one bad tile does not make the month impossible to compact; they stay split, and
    reading that tile fails, until the bad row is removed. The rows are read and
    merged in batches, a few threads at a time: a month can hold thousands of them."""
    from concurrent.futures import ThreadPoolExecutor
    from itertools import groupby
    from rasterio.errors import RasterioError
    from .sources.common import merge_tiles
    cols = ", ".join(COLS)
    i = {c: k for k, c in enumerate(COLS)}
    key = lambda r: tuple(r[i[c]] for c in ("band", "date", "acq", "crs", "x", "y"))

    def merge(group):
        try:
            blob, valid_px, scenes, cloud = merge_tiles(
                [(r[i["scene_id"]], r[i["data"]], r[i["cloud_pct"]]) for r in group])
        except (ValueError, RasterioError):
            return group
        row = list(group[0])
        row[i["data"]], row[i["size_bytes"]], row[i["valid_px"]] = blob, len(blob), valid_px
        row[i["scene_id"]], row[i["cloud_pct"]] = scenes, cloud
        row[i["fetched_at"]] = max(r[i["fetched_at"]] for r in group)
        return [row]

    split = (f"(SELECT * FROM {src} WHERE (band, date, acq, crs, x, y) IN "
             f"(SELECT (band, date, acq, crs, x, y) FROM {src} WHERE acq IS NOT NULL "
             f"GROUP BY {KEY} HAVING count(*) > 1))")
    con.execute(f"CREATE OR REPLACE TABLE merged ({SCHEMA})")
    cur = con.cursor()
    cur.execute(f"SELECT {cols} FROM {_resolved(split)} ORDER BY {KEY}, scene_id")

    def rows():
        while batch := cur.fetchmany(256):
            yield from batch

    insert = f"INSERT INTO merged VALUES ({', '.join('?' for _ in COLS)})"
    with ThreadPoolExecutor(max_workers=workers) as ex:      # GDAL releases the GIL in codecs
        batch = []
        for _, group in groupby(rows(), key=key):
            batch.append(list(group))
            if len(batch) == 64:
                con.executemany(insert, [r for out in ex.map(merge, batch) for r in out])
                batch = []
        if batch:
            con.executemany(insert, [r for out in ex.map(merge, batch) for r in out])
    cur.close()


def _month_rows(con, files, split: bool) -> str:
    """SQL for the rows a month holds once it is compacted: re-fetched tiles resolved
    and, when the month holds split tiles, the scenes of one acquisition merged into
    one tile."""
    src = scan(files)
    if not split:
        return f"SELECT * FROM {_resolved(src)}"
    _merge_split_tiles(con, src)
    rest = (f"(SELECT * FROM {src} WHERE acq IS NULL OR (band, date, acq, crs, x, y) NOT IN "
            f"(SELECT (band, date, acq, crs, x, y) FROM merged))")
    return f"SELECT * FROM {_resolved(rest)} UNION ALL SELECT {', '.join(COLS)} FROM merged"


def _discard(path) -> None:
    """Delete a file if that is possible now; one another program holds open stays."""
    try:
        path.unlink(missing_ok=True)
    except PermissionError:
        pass


def _restore_sealed(target) -> None:
    """Finish what _seal left when it was killed, or could not move the sealed file
    back: a sealed file moved aside with nothing in its place is moved back, one whose
    place was taken is deleted. Runs under the month lock, before the month's files are
    listed. Until the month is compacted or re-encoded again its sealed file is
    missing; open_months lists such a month, so `compact` gets to it."""
    old = target.with_suffix(".parquet.old")
    if old.exists():
        if target.exists():
            _discard(old)
        else:
            old.replace(target)


def _seal(tmp, target, files) -> None:
    """Put the finished month file `tmp` in place of the sealed file and delete the
    parts it replaces. The sealed name never holds part of a month, and no part is
    deleted before the new file is there, so a file another program holds open costs no
    rows:
    - Windows does not rename over a file a reader has open but lets that file be
      renamed, so the old sealed file is moved aside for the new one (for that instant
      the month has no sealed file) and moved back when the new one cannot take its
      place;
    - a part that cannot be deleted stays an open part. Its rows are in the sealed file
      too; the next compaction resolves the two copies.
    Raises when the new file cannot be put in place. The month is then as it was, unless
    the sealed file could not be moved back either: it stays aside as `.parquet.old`
    until the next compaction of the month (_restore_sealed)."""
    try:
        tmp.replace(target)
    except PermissionError as e:
        old = target.with_suffix(".parquet.old")
        try:
            # Nothing to gain from moving the sealed file aside when there is none, or
            # when it is `tmp` that is held: Windows reports that as error 32 (sharing
            # violation on the source), a held target as error 5.
            if not target.exists() or getattr(e, "winerror", None) == 32:
                raise
            target.replace(old)             # refused when a program holds it without sharing
        except PermissionError:
            _discard(tmp)
            raise
        try:
            tmp.replace(target)
        except BaseException:
            try:
                old.replace(target)
            except OSError:
                pass
            _discard(tmp)
            raise
        _discard(old)
    for f in files:
        if f != target:
            _discard(f)


def compact_month(dataset: str, res: float, date: str, wait: bool = True,
                  only_split: bool = False):
    """Merge a month's sealed file and parts into one file. Rows sharing a key are
    resolved: scenes of one acquisition holding parts of a tile are merged into one
    tile, a re-fetched tile keeps the newest fetched_at. Safe to run any time, including
    as repair. only_split: do nothing unless the month holds a tile of an acquisition in
    several rows. Returns the sealed file, or None when there was nothing to do. One
    compaction per month at a time (lock file): returns False when another process holds
    the month (at once with wait=False, else after the lock timeout). A part another
    program holds open is not deleted: the month then still has open parts (see _seal)."""
    target = month_dir(dataset, date) / f"r{int(res)}.parquet"
    lock = _MonthLock(target.with_suffix(".parquet.lock"), wait)
    if not lock.acquire():
        return False
    try:
        return _compact_month_locked(dataset, res, date, target, only_split)
    finally:
        lock.release()


def _compact_month_locked(dataset: str, res: float, date: str, target, only_split: bool = False):
    _restore_sealed(target)
    files = month_files(dataset, res, date)
    if not files:
        return None
    split = _has_split_tiles(files)
    if only_split and not split:
        return None
    if len(files) == 1 and not split:
        # A lone part file (typical for static layers after their first ingest)
        # needs no merge; sealing it is just a rename.
        if files[0] != target:
            files[0].replace(target)
            return target
        return None
    tmp = target.with_suffix(".parquet.tmp")
    con = duckdb.connect()
    try:
        _write_sorted(con, _month_rows(con, files, split), tmp)
    except BaseException:
        _discard(tmp)
        raise
    finally:
        con.close()
    _seal(tmp, target, files)
    return target


def reencode_month(dataset: str, res: float, date: str, workers: int = 8, wait: bool = True):
    """Rewrite one (res, month) with every blob re-encoded by the current encoder
    (sources.common.reencode_blob): pixels, georeferencing and nodata stay bit-for-bit,
    only the compression choice changes. Resolves rows sharing a key like compaction
    and seals the month. Returns (rows, bytes_before, bytes_after), or
    None when the month is empty or locked by another process."""
    from concurrent.futures import ThreadPoolExecutor
    from .sources.common import reencode_blob
    target = month_dir(dataset, date) / f"r{int(res)}.parquet"
    lock = _MonthLock(target.with_suffix(".parquet.lock"), wait)
    if not lock.acquire():
        return None
    try:
        _restore_sealed(target)
        files = month_files(dataset, res, date)
        if not files:
            return None
        con = duckdb.connect()
        rows = con.execute(
            f"SELECT * FROM ({_month_rows(con, files, _has_split_tiles(files))}) "
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
        try:
            _write_sorted(con, "SELECT * FROM buf", tmp)
        except BaseException:
            _discard(tmp)
            raise
        finally:
            con.close()
        _seal(tmp, target, files)
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


def lock_file(dataset: str, res: float, date: str) -> Path:
    """The lock a compaction holds on a (res, month). One left behind by a crash keeps
    the month from being compacted until it is deleted."""
    return month_dir(dataset, date) / f"r{int(res)}.parquet.lock"


def open_months(dataset: str) -> list:
    """The (res, date) groups of a dataset a compaction has work for: those with open
    part files, and those whose sealed file was left aside (see _restore_sealed)."""
    import re
    out = []
    root = STORE / f"dataset={dataset}"
    for f in sorted(root.rglob("r*.part-*.parquet")) + sorted(root.rglob("r*.parquet.old")):
        m = re.match(r"r(\d+)\.(?:part-|parquet\.old$)", f.name)
        rel = f.parent.relative_to(STORE / f"dataset={dataset}").as_posix()
        key = (float(m.group(1)), "" if rel == "static" else rel.replace("/", "-") + "-01")
        if key not in out:
            out.append(key)
    return out


def compact(dataset: str) -> list:
    """Compact every (res, month) of a dataset that open_months lists: those with open
    part files or a sealed file left aside. Returns the groups that are sealed now; one
    that another process holds locked for longer than the lock timeout stays open."""
    todo = open_months(dataset)
    for key in todo:
        compact_month(dataset, *key)
    left = open_months(dataset)
    return [key for key in todo if key not in left]
