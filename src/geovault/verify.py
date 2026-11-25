"""Verify: is what a dataset holds on disk whole, and does its catalog say what is there?

    from geovault.verify import verify
    report = verify("s2")               # files, rows and the catalog: seconds
    report = verify("s2", deep=True)    # and every tile read: minutes per 10 GB

Nothing is written, moved or locked, and every file is read through DuckDB, which
shares a file with a compaction that replaces it: verify can run beside an ingest. What
it finds then is the store of that moment (open part files), and when a file goes away
while it is read verify says so and asks to be run again.
"""

import math
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import numpy as np

from . import catalog, store
from .blob import BadTile, check
from .sources.common import observed
from .store import COLS, KEY, scan

# A store file is r<res>.parquet (a sealed month) or r<res>.part-<id>.parquet (an open part).
_FILE = re.compile(r"r(\d+)\.(part-[0-9a-f]+\.)?parquet$")
_EPSG = re.compile("EPSG:([0-9]+)")
# The columns of a catalog, as catalog.rebuild writes them.
_CATALOG = ["band", "date", "x", "y", "west", "south", "east", "north", "cloud_pct",
            "scene_id", "size_bytes", "file", "crs", "acq", "valid_px"]
# How many tiles of a file are named when a finding concerns many.
_NAMED = 3
# One worker of the deep check reads about this many bytes of a file before it takes the
# next run of rows: a static layer is one file of gigabytes.
_RUN_BYTES = 64_000_000
# Where a tile lies is compared with where its number says, in tile sizes.
_PLACE_TOLERANCE = 1e-6


@dataclass
class Report:
    """What `verify` found. `problems` is empty when the dataset is whole; `notes` are
    things to know that are not wrong."""
    dataset: str
    files: int = 0
    tiles: int = 0
    read: int = 0                       # tiles whose pixels were read (deep)
    problems: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


class _Run:
    """One verify: its report, the dataset's folder, and the files that went away."""

    def __init__(self, dataset, date):
        self.report = Report(dataset)
        self.root = store.STORE / f"dataset={dataset}"
        self.month = (date or "")[:7]       # the months in scope: those that start with this
        self.date = date
        self.gone = set()

    def name(self, path) -> str:
        """A file of the dataset, relative to its folder."""
        text = str(path).replace("\\", "/")
        base = self.root.as_posix()
        return text[len(base) + 1:] if text.lower().startswith(base.lower() + "/") else text

    def failed(self, path, error, doing) -> None:
        """A file that could not be read: it went away (a compaction replaced it), or it
        is damaged."""
        if not Path(path).exists():
            self.gone.add(Path(path))
        else:
            self.report.problems.append(
                f"{self.name(path)} {doing} ({_first_line(error)}). Delete the file, run "
                f"`geovault rebuild-catalog {self.report.dataset}` and ingest its month again")


def _connect():
    """A DuckDB connection that draws no progress bar: verify is run from scripts."""
    con = duckdb.connect()
    con.execute("SET enable_progress_bar = false")
    return con


def _first_line(error) -> str:
    return str(error).splitlines()[0] if str(error) else type(error).__name__


def _files(run, con) -> dict:
    """{store file: its number of rows} for the files in scope that open, and a finding
    for everything else that lies in the dataset's folder."""
    report, root = run.report, run.root
    files, cannot, parts, tmp, old, locks = {}, 0, 0, 0, 0, 0
    for f in sorted(root.rglob("*")) if root.exists() else []:
        if f.is_dir():
            continue
        rel = f.parent.relative_to(root).as_posix()
        month = "" if rel == "static" else rel.replace("/", "-")
        if run.month and not month.startswith(run.month):      # a static layer has no dates
            continue
        name = f.name
        if name.endswith(".parquet.tmp"):
            tmp += 1
        elif name.endswith(".parquet.old"):
            old += 1
            if not f.with_suffix("").exists():      # r10.parquet.old -> r10.parquet
                report.problems.append(
                    f"{run.name(f)} is a sealed month left aside and nothing is in its "
                    f"place: run `geovault compact {report.dataset}`")
        elif name.endswith(".parquet.lock"):
            locks += 1
        elif name.endswith(".parquet"):
            if not _FILE.match(name):
                # catalog.rebuild lists every Parquet file below the folder, so readers
                # are served the rows of this one
                report.problems.append(
                    f"{run.name(f)} is not a file of the store, and the catalog lists the "
                    f"rows of every Parquet file here: move it away and run "
                    f"`geovault rebuild-catalog {report.dataset}`")
            parts += ".part-" in name
            try:
                names = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {scan([f])}").fetchall()]
                rows = con.execute(f"SELECT count(*) FROM {scan([f])}").fetchone()[0]
            except (duckdb.Error, OSError) as e:
                cannot += 1
                run.failed(f, e, "cannot be opened")
                continue
            if names != COLS:
                cannot += 1
                report.problems.append(
                    f"{run.name(f)} does not have the columns of a store file of this "
                    f"version: it was written by another version, ingest the dataset again")
                continue
            files[f] = rows
        else:
            report.notes.append(f"{run.name(f)} is not a file of the store")
    report.files = len(files) + cannot
    report.tiles = sum(files.values())
    if parts:
        report.problems.append(
            f"{parts} open part files: an ingest was interrupted or is running. "
            f"Run `geovault compact {report.dataset}` once no ingest is")
    if tmp:
        report.notes.append(f"{tmp} .parquet.tmp files of an interrupted write: never read, delete them")
    if old:
        report.notes.append(f"{old} .parquet.old files of an interrupted compaction")
    if locks:
        report.notes.append(f"{locks} lock files: a month is being compacted, or a killed run left "
                            f"them behind (they lock nothing)")
    return files


def _readable(run, con, files) -> dict:
    """The files whose columns other than the tiles can be read from end to end. One
    that cannot (a damaged page) is reported and left out of what follows."""
    # every value has to pass through the query: a count is answered from the footer
    cols = ", ".join(c for c in COLS if c != "data")
    good = {}
    for f, rows in files.items():
        try:
            con.execute(f"SELECT bit_xor(hash({cols})) FROM {scan([f])}").fetchone()
        except (duckdb.Error, OSError) as e:
            run.failed(f, e, "cannot be read")
            continue
        good[f] = rows
    return good


def _rows(run, con, files) -> None:
    """What the rows of the files in scope say without their pixels: every row in the
    file of its own month and resolution, no tile stored twice, sizes and counts that can
    be true."""
    report = run.report
    src = scan(list(files), filename=True)
    bad = con.execute(
        f"SELECT count(*) FILTER (WHERE size_bytes IS NULL OR size_bytes <= 0 "
        f"       OR tile_px IS NULL OR tile_px <= 0), "
        f"       count(*) FILTER (WHERE valid_px < 0 OR valid_px > CAST(tile_px AS BIGINT) * tile_px), "
        f"       count(*) FILTER (WHERE band IS NULL OR date IS NULL OR crs IS NULL "
        f"       OR x IS NULL OR y IS NULL OR res IS NULL) "
        f"FROM {src}").fetchone()
    for n, what in zip(bad, ("without a size", "with a count of observed pixels no tile of "
                             "their size can have",
                             "without a band, date, CRS, tile number or resolution")):
        if n:
            report.problems.append(f"{n} rows {what}")

    # a row belongs in the folder of its month and the file of its resolution
    placed = con.execute(
        f"SELECT filename, count(*) FROM {src} WHERE NOT coalesce(contains("
        f"  replace(filename, chr(92), '/'), "
        f"  CASE WHEN date = '' THEN '/static' ELSE '/' || replace(substr(date, 1, 7), '-', '/') END "
        f"  || '/r' || CAST(CAST(trunc(res) AS BIGINT) AS VARCHAR) || '.'), false) "
        f"GROUP BY filename ORDER BY filename").fetchall()
    for name, n in placed:
        report.problems.append(
            f"{run.name(name)} holds {n} rows of another month or resolution")

    # A tile in two rows is expected in a month with open parts: compaction resolves it.
    # In a sealed month no command does.
    open_months = {f.parent for f in files if ".part-" in f.name}
    for where, group in (("open", [f for f in files if f.parent in open_months]),
                         ("sealed", [f for f in files if f.parent not in open_months])):
        if not group:
            continue
        tiles, extra = con.execute(
            f"SELECT count(*), sum(n - 1) FROM (SELECT count(*) n FROM {scan(group)} "
            f"GROUP BY {KEY} HAVING count(*) > 1)").fetchone()
        if not tiles:
            continue
        if where == "open":
            report.problems.append(
                f"{tiles} tiles are stored in more than one row ({extra} rows too many) in "
                f"months with open part files: `geovault compact {report.dataset}` resolves them")
        else:
            report.problems.append(
                f"{tiles} tiles are stored in more than one row of a sealed month ({extra} "
                f"rows too many): scenes of one overpass that could not be merged, or a file "
                f"changed by hand. No command resolves them; `--deep` says which tiles are "
                f"not whole")


def _catalog(run, con, files) -> None:
    """Does the catalog list exactly the rows of the files in scope, with the values the
    store holds, and does it name the files where they are?"""
    report = run.report
    p = catalog.path(report.dataset)
    rebuild = f"run `geovault rebuild-catalog {report.dataset}`"
    if not p.exists():
        if files:
            report.problems.append(f"no catalog: {rebuild}")
        return
    try:
        names = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {scan([p])}").fetchall()]
    except (duckdb.Error, OSError) as e:
        report.problems.append(f"the catalog cannot be read ({_first_line(e)}): {rebuild}")
        return
    if names != _CATALOG:
        report.problems.append(f"the catalog does not have the columns of this version: {rebuild}")
        return

    # A file is compared by its place below the dataset's folder: the same folder can be
    # written with another drive letter, or relative to another working directory.
    mark = "'/dataset=" + report.dataset.replace("'", "''") + "/'"
    tail = ("substr(replace(CAST({0} AS VARCHAR), chr(92), '/'), "
            "strpos(replace(CAST({0} AS VARCHAR), chr(92), '/'), " + mark + ") + length(" + mark + "))")
    cols = ", ".join(c for c in _CATALOG if c != "file")
    listed = f"SELECT {cols}, {tail.format('file')} f FROM {scan([p])}"
    args = []
    if run.month:           # the rows the catalog has for these months, and for these files
        here = ", ".join("?" for _ in files)
        listed += " WHERE date LIKE ?" + (f" OR {tail.format('file')} IN ({here})" if files else "")
        args = [run.month + "%", *(run.name(f) for f in files)]
    try:
        if files:
            stored = f"SELECT {cols}, {tail.format('filename')} f FROM {scan(list(files), filename=True)}"
            missing = con.execute(f"SELECT count(*) FROM ({stored} EXCEPT ALL {listed})", args).fetchone()[0]
            extra = con.execute(f"SELECT count(*) FROM ({listed} EXCEPT ALL {stored})", args).fetchone()[0]
        else:
            missing = 0
            extra = con.execute(f"SELECT count(*) FROM ({listed})", args).fetchone()[0]
        named = [r[0] for r in con.execute(
            f"SELECT DISTINCT file FROM {scan([p])}" + (" WHERE date LIKE ?" if run.month else ""),
            args[:1]).fetchall()]
    except (duckdb.Error, OSError) as e:
        if any(not f.exists() for f in files):
            run.gone.update(f for f in files if not f.exists())
        else:
            report.problems.append(f"the catalog cannot be compared with the store "
                                   f"({_first_line(e)}): {rebuild}")
        return
    if missing or extra:
        report.problems.append(
            f"the catalog is stale: the store holds {missing} tiles it does not list as they "
            f"are, and it lists {extra} the store does not hold. {rebuild.capitalize()}")
        return
    elsewhere, marker = 0, f"/dataset={report.dataset}/"
    for name in named:
        text = str(name).replace(chr(92), "/")
        try:
            elsewhere += (marker not in text
                          or not os.path.samefile(name, run.root / text.split(marker)[-1]))
        except OSError:
            elsewhere += 1
    if elsewhere:
        report.problems.append(
            f"the catalog names {elsewhere} files that are not the files of this data "
            f"folder (was it moved or copied?): {rebuild}")


def _same(a, b) -> bool:
    """Two nodata values, None for none: equal, with NaN equal to NaN."""
    if a is None or b is None:
        return a is None and b is None
    return a == b or (a != a and b != b)


def _tiles(path, lo, hi, date) -> tuple:
    """Read the tiles in rows lo..hi of one store file and compare each with its row.
    Returns (tiles read, [what is wrong with a tile]). Raises what DuckDB raises when the
    file cannot be read."""
    cols = ["band", "date", "x", "y", "crs", "acq", "res", "tile_px", "nodata", "size_bytes",
            "valid_px", "data"]
    sql = (f"SELECT {', '.join(cols)} FROM {scan([path], row_number=True)} "
           f"WHERE file_row_number >= ? AND file_row_number < ?")
    args = [lo, hi]
    if date:
        sql += " AND date LIKE ?"
        args.append(f"{date}%")
    read, wrong, places = 0, [], {}
    con = _connect()
    try:
        cur = con.execute(sql, args)
        while batch := cur.fetchmany(256):
            for row in batch:
                r = dict(zip(cols, row))
                where = (f"band {r['band']} date {r['date'] or 'static'} x {r['x']} y {r['y']} "
                         f"{r['crs']}" + (f" acq {r['acq']}" if r["acq"] else ""))
                blob = r["data"]
                if None in (r["tile_px"], r["x"], r["y"]) or not r["res"]:
                    wrong.append(f"{where}: its row has no size, tile number or resolution")
                    continue
                if blob is None or len(blob) != r["size_bytes"]:
                    wrong.append(f"{where}: {0 if blob is None else len(blob)} bytes, "
                                 f"its row says {r['size_bytes']}")
                    continue
                try:
                    arr, transform, nodata, epsg = check(blob, r["tile_px"])
                except BadTile as e:
                    wrong.append(f"{where}: {e}")
                    continue
                read += 1
                res, code = r["res"], _EPSG.fullmatch(str(r["crs"]))
                if not (math.isclose(transform.a, res, rel_tol=1e-9)
                        and math.isclose(-transform.e, res, rel_tol=1e-9)):
                    wrong.append(f"{where}: pixels of {transform.a} by {-transform.e}, "
                                 f"its row says {res}")
                    continue
                if not _same(nodata, r["nodata"]):
                    wrong.append(f"{where}: nodata value {nodata}, its row says {r['nodata']}")
                elif code and epsg is not None and int(code.group(1)) != epsg:
                    wrong.append(f"{where}: its pixels are in EPSG:{epsg}")
                elif r["valid_px"] is not None:
                    seen = int(np.count_nonzero(observed(arr, nodata)))
                    if seen != r["valid_px"]:
                        wrong.append(f"{where}: {seen} observed pixels, its row says {r['valid_px']}")
                # Where the lattice of the tile numbers starts, in tile sizes: the same
                # for every tile of one band, CRS and size (the bands of a source can
                # come from files that are registered differently).
                size = res * r["tile_px"]
                place = (round((transform.c - r["x"] * size) / size / _PLACE_TOLERANCE),
                         round((transform.f - (r["y"] + 1) * size) / size / _PLACE_TOLERANCE))
                places.setdefault((r["band"], r["crs"], res, r["tile_px"]), []).append((place, where))
    finally:
        con.close()
    for found in places.values():
        usual, n = Counter(p for p, _ in found).most_common(1)[0]
        if n > len(found) / 2:              # a run of one or two tiles cannot say which is off
            wrong += [f"{where}: it does not lie where its tile number says"
                      for place, where in found
                      if max(abs(place[0] - usual[0]), abs(place[1] - usual[1])) > 1]
    return read, wrong


def _deep(run, files, workers) -> None:
    """Read every tile of the files in scope, a few runs of rows at a time."""
    report = run.report
    jobs = []
    for f, rows in files.items():
        try:
            size = f.stat().st_size
        except OSError:
            run.gone.add(f)
            continue
        step = max(1, math.ceil(rows / max(1, math.ceil(size / _RUN_BYTES))))
        jobs += [(f, lo, min(lo + step, rows)) for lo in range(0, rows, step)]

    def one(job):
        try:
            return _tiles(job[0], job[1], job[2], run.date)
        except (duckdb.Error, OSError) as e:
            return e

    found, failed = {}, {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for (f, _, _), result in zip(jobs, ex.map(one, jobs)):
            if isinstance(result, Exception):
                failed.setdefault(f, result)
                continue
            report.read += result[0]
            found.setdefault(f, []).extend(result[1])
    for f, error in failed.items():
        run.failed(f, error, "cannot be read")
    for f in files:
        wrong = found.get(f)
        if wrong:
            more = f" (and {len(wrong) - _NAMED} more)" if len(wrong) > _NAMED else ""
            report.problems.append(
                f"{run.name(f)}: {len(wrong)} tiles are not what their rows say{more}: "
                + "; ".join(wrong[:_NAMED]))


def verify(dataset: str, deep: bool = False, date: str | None = None, workers: int = 8) -> Report:
    """Check what a dataset holds on disk. Reads only.

    Always: every store file opens, has the columns of this version and can be read;
    every row lies in the file of its own month and resolution; no tile is stored in two
    rows; no part file, and no sealed month left aside, waits for a compaction; nothing
    else lies in the dataset's folder that a catalog would list; the catalog lists
    exactly the rows of the store, with the values the store holds, and names the files
    of this data folder.

    deep: every tile is read as well (without GDAL, see blob.check) and compared with
    its row: the size of the blob, the size of the tile, its pixel size, its nodata
    value, the EPSG code of its CRS, its number of observed pixels, and whether it lies
    where its tile number says.

    date: a prefix ('2025', '2025-07', '2025-07-14'). The checks then cover the month
    files it names, as whole files, and the deep check reads the tiles of the dates it
    names. A row of those dates that lies in the file of another month is not seen.
    workers: runs of rows read at once by the deep check."""
    run = _Run(dataset, date)
    report = run.report
    con = _connect()
    try:
        files = _files(run, con)
        try:
            if files:
                _rows(run, con, files)
        except (duckdb.Error, OSError):
            # one of the files cannot be read to its end: find it, and check the others
            good = _readable(run, con, files)
            report.tiles -= sum(n for f, n in files.items() if f not in good)
            files = good
            try:
                if files:
                    _rows(run, con, files)
            except (duckdb.Error, OSError) as e:
                report.problems.append(f"the rows of the store cannot be read ({_first_line(e)})")
        _catalog(run, con, files)
    finally:
        con.close()
    if deep and files:
        _deep(run, files, workers)
    if run.gone:
        report.problems.append(
            f"{len(run.gone)} files went away while they were read: a compaction or an "
            f"ingest is running. Run verify again once it is done")
    if not report.files and not report.problems:
        report.notes.append("nothing is stored" + (f" for {date}" if date else ""))
    return report
