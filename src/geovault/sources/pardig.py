"""PARDIG precipitation (Google WeatherNext), 15-minute, 0.1 degree, from the public bucket.

PARDIG is a machine-learning estimate of the instantaneous surface precipitation rate,
trained on the GPM combined radar-radiometer product (CORRA). Two variants, two datasets:

  pardig_nrt  near real time, 2016 to a few hours ago; inputs: ECMWF HRES analysis +
              SatWeave geostationary mosaic. An archive plus a live stream that the
              publisher extends in 6-hourly batches (2-8 h behind real time).
  pardig_lr   long record, 1999-2023, a static archive; inputs: ERA5 + GridSat-B1 +
              passive microwave sounders.

Bucket layout (gs://pardig_1, anonymous, CC BY 4.0). Segment names are discovered at search
time; the folder layout, band and grid below are fixed and checked:

  <NRT|LR>/zarr/<segment>/predictions.zarr                          map chunks (4 steps x globe)
  <NRT|LR>/zarr_rechunked_as_timeseries/<segment>/predictions.zarr  series chunks (all steps x 5 x 5 px)
  NRT/zarr/<live segment>/<YYYYMMDDTHHMMSS>/predictions.zarr        one global step per folder

A segment under `zarr/` that holds `predictions.zarr` is an ARCHIVE and is read from its
`zarr_rechunked_as_timeseries` copy: a 5 degree tile is then 100 chunk objects (10 x 10)
for the whole record instead of a whole-globe chunk per hour. Each object holds every step
of the archive, so it is downloaded once into a disk cache
(<GEOVAULT_DATA>/cache/<dataset>/<archive folder>/) and read again for every year scene and
run; the cache is emptied when the archive's zarr.json changes and can be deleted whenever
no ingest runs. An archive without its series copy yet is skipped with a warning. A segment
whose children are timestamp folders is the LIVE stream: each step is one 1801 x 3600 chunk
(~1.1 MB), read once and cut into tiles. A step is read once its folder holds a `success`
object (present in every finished step folder; not documented by the publisher); other
steps are left for the next run.

Grid: cell centres on multiples of 0.1 (lat -90..90 ASCENDING in the store, lon 0..359.9),
so the lattice anchor is (-0.05, -0.05). 50 px tiles = 5 degrees, exactly 10 x 10 series
chunks, never crossing the 0/360 seam; Turkey is 8 tiles. Tiles are north-up and keyed on
-180..180 like every other dataset; the 72 tile columns are tx -36..35, so the column at
the antimeridian is stored once.

Time: one tile per 15-minute step, `date` = 'YYYY-MM-DDTHH:MM' (valid time, UTC), read
from the store's own time axis. Band `precipitation`, RAW m h-1 (x 1000 = mm/h), float32,
instantaneous rate at the valid time, capped by the model at 0.075 m/h (75 mm/h); nodata
-9999 where the store has NaN or no cell (beyond the poles). Rows carry scene_id
'<NRT|LR>/<segment>' (e.g. 'NRT/2016_to_2025', 'NRT/2026_to_present').

Encoding: the field is sparse (about 95% exact zeros), so the TIFF predictor is pinned to 2.
Measured on 768 Turkey tiles of one day: the automatic choice produced the same bytes
(predictor 2 won every tile) in twice the time.
"""

import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date as _date, datetime, timedelta, timezone

import numpy as np

from ..grid import Grid
from . import common

BUCKET = "pardig_1"
CRS = "EPSG:4326"
RES = 0.1
TILE_PX = 50                  # 5 degrees: 10 x 10 series chunks per tile
ANCHOR = -0.05                # cell edges are 0.05 off the 0.1 lines (centres on the lines)
NODATA = -9999.0
NROW, NCOL = 1801, 3600       # lat -90..90 ascending, lon 0..359.9
TX_MIN = -36                  # tile columns -36..35 cover -180.05..179.95 once
TX_MAX = TX_MIN + NCOL // TILE_PX - 1
BAND = "precipitation"
DEFAULT_BANDS = [BAND]
PREDICTOR = 2
FETCH_WORKERS = 8             # concurrent object downloads per archive tile
LIVE_WORKERS = 4              # live steps decoded concurrently (26 MB each)
LIVE_MARKER_GRACE_H = 12      # a live day older than this with no `success` object is suspicious
STEP_RE = re.compile(r"^\d{8}T\d{6}$")
_UNITS = {"seconds": "s", "minutes": "m", "hours": "h", "days": "D"}

_log = logging.getLogger("geovault")


# ---------------------------------------------------------------- remote access

_FS = None
_FS_LOCK = threading.Lock()


def fs():
    """One shared anonymous GCS filesystem (gcsfs is safe across threads)."""
    global _FS
    with _FS_LOCK:
        if _FS is None:
            import gcsfs
            _FS = gcsfs.GCSFileSystem(token="anon")
        return _FS


def _name(path: str) -> str:
    return path.rstrip("/").split("/")[-1]


def _children(path: str) -> list:
    """Names under a bucket folder (gcsfs also lists the folder itself; dropped)."""
    me = path.rstrip("/")
    return [_name(p) for p in fs().ls(me) if p.rstrip("/") != me]


def check_array_meta(meta: dict, path: str) -> dict:
    """Validate a Zarr v3 array zarr.json the adapter can decode by hand (little-endian float32
    bytes + zstd, default chunk keys, NaN fill); fail loudly if the publisher changes it."""
    codecs = [c.get("name") for c in meta.get("codecs", [])]
    if meta.get("data_type") != "float32" or codecs != ["bytes", "zstd"]:
        raise RuntimeError(f"{path}: unsupported encoding {meta.get('data_type')} {codecs}")
    if meta["codecs"][0].get("configuration", {}).get("endian", "little") != "little":
        raise RuntimeError(f"{path}: big-endian data")
    if str(meta.get("fill_value", "NaN")).lower() != "nan":
        raise RuntimeError(f"{path}: fill value {meta.get('fill_value')!r}, expected NaN")
    cke = meta.get("chunk_key_encoding", {"name": "default"})
    if cke.get("name") != "default":
        raise RuntimeError(f"{path}: unsupported chunk key encoding {cke}")
    sep = cke.get("configuration", {}).get("separator", "/")
    return {"shape": tuple(meta["shape"]),
            "chunks": tuple(meta["chunk_grid"]["configuration"]["chunk_shape"]),
            "sep": sep}


def check_coordinates(lat: np.ndarray, lon: np.ndarray, where: str) -> None:
    """The store's latitude must run -90..90 ascending and longitude 0..359.9 in 0.1 steps."""
    if not (len(lat) == NROW and len(lon) == NCOL and np.isclose(lat[0], -90) and np.isclose(lat[-1], 90)
            and np.isclose(lon[0], 0) and np.isclose(lon[1] - lon[0], RES)):
        raise RuntimeError(f"{where}: unexpected coordinates")


def read_coordinates(base: str) -> tuple:
    """(latitude, longitude) arrays of a predictions.zarr group in the bucket."""
    import zarr
    g = zarr.open_group(f"gs://{base}", mode="r", storage_options={"token": "anon"})
    return g["latitude"][:], g["longitude"][:]


def chunk_key(array_path: str, idx: tuple, sep: str = "/") -> str:
    return f"{array_path}/c{sep}" + sep.join(str(i) for i in idx)


def decode_chunk(raw: bytes, shape: tuple) -> np.ndarray:
    from numcodecs import Zstd
    return np.frombuffer(Zstd().decode(raw), "<f4").reshape(shape)


def parse_time_units(units: str):
    """CF 'minutes since 1999-01-01 00:00:00' -> (numpy unit, numpy datetime64 epoch)."""
    m = re.match(r"^\s*(\w+)\s+since\s+(.+?)\s*$", units)
    if not m or m.group(1) not in _UNITS:
        raise RuntimeError(f"unsupported time units: {units!r}")
    return _UNITS[m.group(1)], np.datetime64(m.group(2).replace(" ", "T"), "m")


def step_date(t: np.datetime64) -> str:
    """'YYYY-MM-DDTHH:MM' of a valid time."""
    return np.datetime_as_string(t.astype("datetime64[m]"), unit="m")


def folder_time(name: str) -> np.datetime64:
    """Valid time of a live step folder ('20261116T043000'). The folder name is the step's
    time; the array's own scalar time is 0 in units relative to itself."""
    return np.datetime64(datetime.strptime(name, "%Y%m%dT%H%M%S"), "m")


# ---------------------------------------------------------------- grid

def grid_for() -> Grid:
    return Grid(CRS, RES, TILE_PX, ANCHOR, ANCHOR)


def tile_window(tx: int, ty: int) -> tuple:
    """Store indices of one tile: (row0, col0) of its SOUTH-WEST cell in the ascending-latitude,
    0..360 store. Rows run row0..row0+TILE_PX-1 (may exceed the store at the poles); columns
    never wrap inside a tile (NCOL is a multiple of TILE_PX)."""
    w, s, e, n = grid_for().tile_bounds(tx, ty)
    row0 = int(round((s + RES / 2 + 90.0) / RES))
    col0 = int(round((w + RES / 2) / RES)) % NCOL
    return row0, col0


def tile_from_store_block(block: np.ndarray) -> np.ndarray:
    """(TILE_PX, TILE_PX) block in store orientation (row 0 = south) -> north-up float32 tile
    with NaN mapped to nodata."""
    a = np.ascontiguousarray(block[::-1], dtype=np.float32)
    a[~np.isfinite(a)] = NODATA
    return a


def cut_tile(field2d: np.ndarray, tx: int, ty: int) -> np.ndarray:
    """One north-up tile from a whole (NROW, NCOL) store field; nodata beyond the poles."""
    row0, col0 = tile_window(tx, ty)
    block = np.full((TILE_PX, TILE_PX), np.nan, np.float32)
    r0, r1 = max(row0, 0), min(row0 + TILE_PX, NROW)
    if r1 > r0:
        block[r0 - row0:r1 - row0] = field2d[r0:r1, col0:col0 + TILE_PX]
    return tile_from_store_block(block)


# ---------------------------------------------------------------- segments

@dataclass
class Archive:
    product: str                  # 'NRT' | 'LR'
    name: str                     # e.g. '2016_to_2025'
    times: np.ndarray             # datetime64[m], ascending
    array_path: str               # .../zarr_rechunked_as_timeseries/<name>/predictions.zarr/precipitation
    meta: dict                    # check_array_meta() of the series array
    meta_raw: bytes               # zarr.json bytes, the cache validity token

    @property
    def scene_id(self):
        return f"{self.product}/{self.name}"


@dataclass
class Live:
    product: str
    name: str                     # e.g. '2026_to_present'
    steps: list                   # folder names, ascending
    root: str                     # .../zarr/<name>

    @property
    def scene_id(self):
        return f"{self.product}/{self.name}"


_SEGMENTS = {}
_SEG_LOCK = threading.Lock()


def _open_archive(product: str, name: str):
    """The archive's series copy, or None (with a warning) when it is not published yet."""
    import zarr
    base = f"{BUCKET}/{product}/zarr_rechunked_as_timeseries/{name}/predictions.zarr"
    f = fs()
    if not f.exists(f"{base}/{BAND}/zarr.json"):
        _log.warning(f"PARDIG {product}/{name}: no series copy at gs://{base} yet, segment skipped")
        return None
    raw = f.cat(f"{base}/{BAND}/zarr.json")
    meta = check_array_meta(json.loads(raw), base)
    if meta["shape"][1:] != (NROW, NCOL):
        raise RuntimeError(f"gs://{base}: unexpected grid {meta['shape']}")
    check_coordinates(*read_coordinates(base), f"gs://{base}")
    g = zarr.open_group(f"gs://{base}", mode="r", storage_options={"token": "anon"})
    unit, epoch = parse_time_units(g["time"].attrs["units"])
    times = (epoch + g["time"][:].astype(f"timedelta64[{unit}]")).astype("datetime64[m]")
    if len(times) != meta["shape"][0] or not np.all(np.diff(times) > np.timedelta64(0, "m")):
        raise RuntimeError(f"gs://{base}: time axis is not strictly ascending")
    return Archive(product, name, times, f"{base}/{BAND}", meta, raw)


def segments(product: str) -> list:
    """Archive and live segments of a product (cached per process)."""
    with _SEG_LOCK:
        if product in _SEGMENTS:
            return _SEGMENTS[product]
        root = f"{BUCKET}/{product}/zarr"
        out = []
        for name in sorted(_children(root)):
            children = _children(f"{root}/{name}")
            if "predictions.zarr" in children:
                seg = _open_archive(product, name)
                if seg is not None:
                    out.append(seg)
            else:
                steps = sorted(c for c in children if STEP_RE.match(c))
                if steps:
                    out.append(Live(product, name, steps, f"{root}/{name}"))
        _SEGMENTS[product] = out
        return out


# ---------------------------------------------------------------- items

class Item:
    """One scene for the CLI: a calendar year of an archive, or a day of the live stream."""

    def __init__(self, dataset: str, segment, t0: np.datetime64, t1: np.datetime64, steps=None):
        self.segment = segment
        self.t0, self.t1 = t0, t1                  # inclusive valid-time bounds
        self.steps = steps                         # live: folder names of this day
        label = str(t0)[:4] if isinstance(segment, Archive) else str(t0)[:10]
        self.id = f"{dataset}-{segment.name}-{label}"
        self.properties = {
            "datetime": f"{step_date(t0)}:00Z",
            "start_datetime": f"{step_date(t0)}:00Z",
            "end_datetime": f"{step_date(t1)}:00Z",
        }

    @property
    def scene_id(self):
        return self.segment.scene_id


def search(product: str, dataset: str, bbox, start, end, limit=None) -> list:
    """Archive years and live days that hold steps inside [start, end] (dates, inclusive),
    oldest first. Every valid time is taken from one segment only: archives in order of their
    first step, each from where the previous ones end, then the live stream after all of them."""
    lo = np.datetime64(_date.fromisoformat(start), "m")
    hi = np.datetime64(_date.fromisoformat(end) + timedelta(days=1), "m") - np.timedelta64(1, "m")
    segs = segments(product)
    items = []
    covered = None                                  # last valid time held by an archive
    for seg in sorted((s for s in segs if isinstance(s, Archive)), key=lambda s: s.times[0]):
        a = max(lo, seg.times[0]) if covered is None else max(lo, seg.times[0], covered + np.timedelta64(1, "m"))
        b = min(hi, seg.times[-1])
        covered = seg.times[-1] if covered is None else max(covered, seg.times[-1])
        if a > b:
            continue
        for year in range(int(str(a)[:4]), int(str(b)[:4]) + 1):
            y0 = max(a, np.datetime64(f"{year}-01-01T00:00"))
            y1 = min(b, np.datetime64(f"{year}-12-31T23:59"))
            i0 = int(np.searchsorted(seg.times, y0, side="left"))
            i1 = int(np.searchsorted(seg.times, y1, side="right"))
            if i1 > i0:
                items.append(Item(dataset, seg, seg.times[i0], seg.times[i1 - 1]))
    for seg in (s for s in segs if isinstance(s, Live)):
        by_day = {}
        for s in seg.steps:
            t = folder_time(s)
            if lo <= t <= hi and (covered is None or t > covered):
                by_day.setdefault(s[:8], []).append(s)
        for day in sorted(by_day):
            st = by_day[day]
            items.append(Item(dataset, seg, folder_time(st[0]), folder_time(st[-1]), st))
    items.sort(key=lambda it: it.t0)
    return items[:limit] if limit else items


# ---------------------------------------------------------------- planning

def _item_steps(item) -> list:
    """(date string, index or folder) of every step of an item."""
    seg = item.segment
    if isinstance(seg, Archive):
        i0 = int(np.searchsorted(seg.times, item.t0, side="left"))
        i1 = int(np.searchsorted(seg.times, item.t1, side="right"))
        return [(step_date(seg.times[i]), i) for i in range(i0, i1)]
    return [(step_date(folder_time(s)), s) for s in item.steps]


def _plan(item, bbox, bands, skip_keys, aoi) -> dict:
    """(tx, ty) -> [(date, index or folder)] still to fetch."""
    if bands and BAND not in bands:
        return {}
    from ..aoi import tile_filter
    grid = grid_for()
    skip = skip_keys or set()
    steps = _item_steps(item)
    out = {}
    # one global lat/lon CRS: the UTM zone rule does not apply (keep_offzone=True)
    tiles = common.plan_tiles(grid, bbox, CRS, BAND, date=None, skip_keys=set(), keep_offzone=True,
                              in_aoi=tile_filter(aoi))
    for tx, ty in tiles:
        if not TX_MIN <= tx <= TX_MAX:              # the antimeridian column is stored once
            continue
        todo = [(d, k) for d, k in steps if (BAND, d, CRS, tx, ty) not in skip]
        if todo:
            out[(tx, ty)] = todo
    return out


def plan_scene(item, bbox, bands=None, skip_keys=None, keep_offzone=False, aoi=None) -> dict:
    """band -> number of (step, tile) rows this scene would still fetch (nothing is read).
    keep_offzone is accepted for CLI symmetry; there are no UTM zones here."""
    n = sum(len(v) for v in _plan(item, bbox, bands, skip_keys, aoi).values())
    return {BAND: n} if n else {}


# ---------------------------------------------------------------- archive reads

_LOCKS = {}
_LOCKS_LOCK = threading.Lock()


def _lock(key: str) -> threading.Lock:
    with _LOCKS_LOCK:
        return _LOCKS.setdefault(key, threading.Lock())


def cache_dir(dataset: str, seg: Archive):
    from ..store import ROOT
    return ROOT / "cache" / dataset / seg.name


def _ensure_cache(dataset: str, seg: Archive):
    """Cache folder of a segment; emptied when the remote array metadata has changed."""
    d = cache_dir(dataset, seg)
    with _lock(str(d)):
        d.mkdir(parents=True, exist_ok=True)
        token = d / "zarr.json"
        if not token.exists() or token.read_bytes() != seg.meta_raw:
            for p in d.iterdir():
                p.unlink(missing_ok=True)
            token.write_bytes(seg.meta_raw)
    return d


def _write_atomic(path, data: bytes) -> None:
    tmp = path.parent / f"{path.name}.{os.getpid()}.tmp"
    tmp.write_bytes(data)
    try:
        os.replace(tmp, path)
    except PermissionError:                     # Windows: another process holds the same file
        tmp.unlink(missing_ok=True)


def _cached_chunk(seg: Archive, idx: tuple, cache):
    """Path of one series chunk in the disk cache, downloading it first if needed, or None
    when the bucket has no such object (Zarr leaves all-fill chunks unwritten; recorded as a
    `.missing` marker so it is not asked again). An empty data file is never trusted: it is
    what an interrupted write leaves, and it is fetched again."""
    p = cache / "_".join(str(i) for i in idx)
    missing = p.parent / f"{p.name}.missing"
    with _lock(str(p)):                         # concurrent scenes need the same chunks
        if p.exists() and p.stat().st_size > 0:
            return p
        if missing.exists():
            return None
        try:
            raw = fs().cat(chunk_key(seg.array_path, idx, seg.meta["sep"]))
        except FileNotFoundError:
            _log.warning(f"PARDIG {seg.scene_id}: no object for chunk {idx}, read as nodata")
            _write_atomic(missing, b"")
            return None
        if not raw:
            raise RuntimeError(f"PARDIG {seg.scene_id}: empty object for chunk {idx}")
        _write_atomic(p, raw)
        return p


def tile_chunks(chunks: tuple, row0: int, col0: int, i0: int, i1: int) -> list:
    """Chunk indices (time, row, col) holding steps i0..i1-1 of the tile at (row0, col0)."""
    ct, cr, cc = chunks
    return [(tc, r // cr, c // cc)
            for r in range(max(row0, 0), min(row0 + TILE_PX, NROW), cr)
            for c in range(col0, col0 + TILE_PX, cc)
            for tc in range(i0 // ct, (i1 - 1) // ct + 1)]


def read_archive_block(get_chunk, chunks: tuple, row0: int, col0: int, i0: int, i1: int) -> np.ndarray:
    """(i1 - i0, TILE_PX, TILE_PX) block of a tile from series chunks, store orientation
    (row 0 = south), NaN where the store has no cell or no chunk. `get_chunk(idx)` returns
    the decoded chunk as a full (ct, cr, cc) array (Zarr pads edge chunks to full size) or
    None; chunks are decoded one at a time."""
    ct, cr, cc = chunks
    out = np.full((i1 - i0, TILE_PX, TILE_PX), np.nan, np.float32)
    for tc, ri, ci in tile_chunks(chunks, row0, col0, i0, i1):
        arr = get_chunk((tc, ri, ci))
        if arr is None:
            continue
        r, c = ri * cr, ci * cc
        t_lo, t_hi = max(i0, tc * ct), min(i1, (tc + 1) * ct)
        rows = min(cr, NROW - r)
        out[t_lo - i0:t_hi - i0, r - row0:r - row0 + rows, c - col0:c - col0 + cc] = \
            arr[t_lo - tc * ct:t_hi - tc * ct, :rows, :]
    return out


def _ingest_archive(item, plan, writer) -> int:
    seg = item.segment
    ct, cr, cc = seg.meta["chunks"]
    if TILE_PX % cr or TILE_PX % cc:
        raise RuntimeError(f"{seg.scene_id}: chunks {seg.meta['chunks']} do not tile {TILE_PX} px")
    cache = _ensure_cache(writer.dataset, seg)
    grid = grid_for()
    written = 0
    for (tx, ty), todo in plan.items():
        row0, col0 = tile_window(tx, ty)
        i0, i1 = todo[0][1], todo[-1][1] + 1
        # download every chunk object of this tile to the disk cache, concurrently
        idxs = tile_chunks((ct, cr, cc), row0, col0, i0, i1)
        with ThreadPoolExecutor(FETCH_WORKERS) as ex:
            paths = dict(zip(idxs, ex.map(lambda k: _cached_chunk(seg, k, cache), idxs)))

        def get_chunk(idx):
            p = paths.get(idx)
            return None if p is None else decode_chunk(p.read_bytes(), (ct, cr, cc))

        block = read_archive_block(get_chunk, (ct, cr, cc), row0, col0, i0, i1)
        wb = common.wgs84_bounds(grid, tx, ty, CRS)
        for d, i in todo:
            a = tile_from_store_block(block[i - i0])
            valid = int((a != NODATA).sum())
            if not valid:
                continue
            blob = common.encode_geotiff(a, grid, tx, ty, CRS, NODATA, predictor=PREDICTOR)
            if writer.add(BAND, d, tx, ty, CRS, RES, TILE_PX, wb, NODATA, None, seg.scene_id, blob, valid):
                written += 1
    return written


# ---------------------------------------------------------------- live reads

_LIVE_META = {}


def _successful(seg: Live, steps: list) -> set:
    """Folders of `steps` that hold a `success` object (one prefix listing per day, ~2k
    objects; a glob would list the whole segment)."""
    out = set()
    for day in sorted({s[:8] for s in steps}):
        for p in fs().find(seg.root, prefix=f"{day}T"):
            if p.endswith("/success"):
                out.add(p.split("/")[-2])
    return out & set(steps)


def _live_meta(seg: Live, step: str) -> dict:
    """Encoding, layout and coordinates of the live stream, checked once per segment and run."""
    with _lock(f"live:{seg.root}"):
        if seg.root not in _LIVE_META:
            base = f"{seg.root}/{step}/predictions.zarr"
            meta = check_array_meta(json.loads(fs().cat(f"{base}/{BAND}/zarr.json")), base)
            if meta["shape"] != (NROW, NCOL) or meta["chunks"] != (NROW, NCOL):
                raise RuntimeError(f"gs://{base}: unexpected layout {meta['shape']} {meta['chunks']}")
            check_coordinates(*read_coordinates(base), f"gs://{base}")
            _LIVE_META[seg.root] = meta
        return _LIVE_META[seg.root]


def _read_live_step(seg: Live, step: str) -> np.ndarray:
    meta = _live_meta(seg, step)
    key = chunk_key(f"{seg.root}/{step}/predictions.zarr/{BAND}", (0, 0), meta["sep"])
    return decode_chunk(fs().cat(key), (NROW, NCOL))


def _ingest_live(item, plan, writer) -> int:
    seg = item.segment
    by_step = {}
    for (tx, ty), todo in plan.items():
        for d, s in todo:
            by_step.setdefault(s, []).append((d, tx, ty))
    ready = _successful(seg, list(by_step))
    if by_step and not ready:
        age = datetime.now(timezone.utc).replace(tzinfo=None) - item.t1.astype(datetime)
        if age > timedelta(hours=LIVE_MARKER_GRACE_H):
            _log.warning(f"PARDIG {seg.scene_id}: no `success` object in any of {len(by_step)} step folders "
                        f"of {item.id}; nothing stored (has the publisher's layout changed?)")
    grid = grid_for()
    written = 0

    def one(step):
        f = _read_live_step(seg, step)
        rows = []
        for d, tx, ty in by_step[step]:
            a = cut_tile(f, tx, ty)
            valid = int((a != NODATA).sum())
            if valid:
                rows.append((d, tx, ty, a, valid))
        return rows

    with ThreadPoolExecutor(LIVE_WORKERS) as ex:
        for rows in ex.map(one, sorted(ready)):
            for d, tx, ty, a, valid in rows:
                blob = common.encode_geotiff(a, grid, tx, ty, CRS, NODATA, predictor=PREDICTOR)
                if writer.add(BAND, d, tx, ty, CRS, RES, TILE_PX, common.wgs84_bounds(grid, tx, ty, CRS),
                              NODATA, None, seg.scene_id, blob, valid):
                    written += 1
    return written


def ingest_scene(item, bbox, writer, bands=None, skip_keys=None, log=print,
                 keep_offzone=False, aoi=None) -> int:
    """Store every 15-minute tile of this scene over bbox/aoi that is not in the store yet."""
    plan = _plan(item, bbox, bands, skip_keys, aoi)
    if not plan:
        return 0
    if isinstance(item.segment, Archive):
        n = _ingest_archive(item, plan, writer)
    else:
        n = _ingest_live(item, plan, writer)
    log(f"    {n} tiles ({item.segment.scene_id}, {len(plan)} tiles x {item.id})")
    return n
