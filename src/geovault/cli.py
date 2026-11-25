"""GEOVAULT command line.

  geovault ingest --dataset s2 --geojson parcel.geojson --start 2024-06-01 --end 2026-06-01
  geovault ingest --dataset s2 --bbox 35.30 36.71 35.33 36.73 --start ... --end ... [--cloud-max 80]
  geovault coverage s2 [--bbox W S E N] [--date 2024-06]
  geovault compact s2
  geovault reencode agera5          # re-compress stored tiles with the current encoder (lossless)
  geovault rebuild-catalog s2
  geovault datasets                 # the datasets this version can ingest
  geovault --version

Also runnable as: python -m geovault ...
"""

import argparse
import sys
import time

from . import __version__, catalog
from .sources import REGISTRY
from .store import Writer, compact, reencode_month
from . import store as _store


def scene_months(item) -> list:
    """Months (YYYY-MM) a scene can write tiles to: every month from its start_datetime to
    its end_datetime, or the month of its datetime when it has no interval. A file that
    holds several days can cross a month end; the skip set must cover both months, or the
    days in the second one are fetched again on every re-run."""
    props = item.properties
    first = (props.get("start_datetime") or props["datetime"])[:7]
    last = (props.get("end_datetime") or props["datetime"])[:7]
    year, month = int(first[:4]), int(first[5:7])
    months = []
    while f"{year:04d}-{month:02d}" <= last:
        months.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months or [props["datetime"][:7]]


def dataset_lines() -> list:
    """One line per dataset of the registry: its name, whether it is static (stored once)
    or dated (ingested for a date range), and the first sentence of its adapter's
    description (which may run over several lines of the docstring)."""
    width = max(len(name) for name in REGISTRY)
    out = []
    for name, src in REGISTRY.items():
        kind = "static" if getattr(src, "STATIC", False) else "dated"
        opening = " ".join((src.__doc__ or "").strip().split("\n\n")[0].split())
        title = opening.split(". ")[0].rstrip(".")
        out.append(f"  {name:<{width}}  {kind:<6}  {title}")
    return out


def _report_open(dataset, say) -> None:
    """Name the months a compaction could not seal, and what to do about them."""
    left = _store.open_months(dataset)
    if not left:
        return
    say(f"  {len(left)} (res, month) groups are not sealed (open part files, or a sealed "
        f"file left aside): "
        + ", ".join(f"{date[:7] or 'static'} r{int(res)}" for res, date in left))
    # A compaction has just asked for each of these months. One that still has a lock
    # file is held by another process, whose number the file holds.
    locks = [f for f in (_store.lock_file(dataset, res, date) for res, date in left) if f.exists()]
    if locks:
        say("  another geovault process is compacting or re-encoding some of them right now "
            f"(its number is in {', '.join(str(f) for f in locks)}): "
            f"run `geovault compact {dataset}` once it has finished")
    if len(locks) < len(left):
        say(f"  a file of theirs was held open by another program, or a run was interrupted: "
            f"run `geovault compact {dataset}` once nothing holds them")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="geovault")
    ap.add_argument("--version", action="version", version=f"geovault {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("datasets", help="the datasets this version can ingest")

    p = sub.add_parser("ingest", help="fetch tiles covering an AOI or bbox")
    p.add_argument("--dataset", required=True, choices=list(REGISTRY))
    p.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"))
    p.add_argument("--geojson", help="AOI polygon file; tile selection uses true intersection")
    p.add_argument("--point", nargs=2, type=float, metavar=("LAT", "LON"),
                   help="point of interest; combined with --buffer into a small AOI")
    p.add_argument("--buffer", type=float, default=250,
                   help="half-size in meters of the square AOI around --point (default 250)")
    p.add_argument("--start", help="required for time-varying datasets")
    p.add_argument("--end", help="required for time-varying datasets")
    p.add_argument("--cloud-max", type=float,
                   help="scene-level cloud filter; keep it loose (80), select per tile at read time")
    p.add_argument("--bands", nargs="+")
    p.add_argument("--max-scenes", type=int)
    p.add_argument("--workers", type=int, default=3,
                   help="scenes fetched concurrently (each scene already reads its bands in parallel); default 3")
    p.add_argument("--keep-offzone", action="store_true",
                   help="Sentinel-2: also store the tiles of a granule that lie in the neighbouring "
                        "MGRS zone or hemisphere (the same ground is then stored in two CRS)")
    p.add_argument("--flush-mb", type=float, default=256,
                   help="write buffered tiles to disk once they exceed this many MB (default 256); "
                        "an interrupted run keeps every flushed part file")
    p.add_argument("--dry-run", action="store_true",
                   help="fetch nothing; report per scene how many tiles are not in the store yet "
                        "(exit 0 = complete, 1 = gaps). Use it to verify coverage after an ingest.")

    p = sub.add_parser("coverage", help="what is stored (dates, bands, tile counts)")
    p.add_argument("dataset")
    p.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"))
    p.add_argument("--date")

    p = sub.add_parser("compact", help="seal a dataset's open month part files (and put back a "
                                       "sealed file an interrupted run left aside)")
    p.add_argument("dataset")

    p = sub.add_parser("rebuild-catalog", help="regenerate the catalog from the store")
    p.add_argument("dataset")

    p = sub.add_parser("reencode", help="re-compress every stored tile of a dataset with the current "
                                        "encoder (lossless; pixels unchanged), month by month")
    p.add_argument("dataset")
    p.add_argument("--workers", type=int, default=8, help="tiles encoded concurrently per month (default 8)")

    a = ap.parse_args(argv)

    if a.cmd == "datasets":
        for line in dataset_lines():
            print(line)
        return 0

    if a.cmd == "compact":
        done = compact(a.dataset)
        print(f"compacted {len(done)} (res, month) groups")
        _report_open(a.dataset, print)
        catalog.rebuild(a.dataset)
        return 0

    if a.cmd == "reencode":
        groups = _store.months(a.dataset)
        tb = ta = n = 0
        t0 = time.time()
        for i, (res, date) in enumerate(groups, 1):
            r = reencode_month(a.dataset, res, date, workers=a.workers)
            if r is None:
                print(f"  [{i}/{len(groups)}] {date[:7] or 'static'} r{int(res)}: skipped (empty or locked)")
                continue
            rows, before, after = r
            n += rows
            tb += before
            ta += after
            print(f"  [{i}/{len(groups)}] {date[:7] or 'static'} r{int(res)}: {rows} tiles "
                  f"{before / 1e6:.1f} -> {after / 1e6:.1f} MB ({(after - before) / max(before, 1) * 100:+.0f}%)"
                  f"  {time.time() - t0:.0f}s", flush=True)
        print(f"reencoded {n} tiles: {tb / 1e9:.2f} -> {ta / 1e9:.2f} GB "
              f"({(ta - tb) / max(tb, 1) * 100:+.0f}%)")
        _report_open(a.dataset, print)
        catalog.rebuild(a.dataset)
        return 0

    if a.cmd == "rebuild-catalog":
        n = catalog.rebuild(a.dataset)
        print(f"catalog rebuilt: {a.dataset} ({n} rows)")
        return 0

    if a.cmd == "coverage":
        try:
            rows = catalog.coverage(a.dataset, tuple(a.bbox) if a.bbox else None, a.date)
        except ValueError as exc:
            ap.error(str(exc))
        if not rows:
            print("no records")
            return 0
        for date, band, tiles, cloud in rows:
            print(f"  {date or 'static':>10}  {band:<4} {tiles:>5} tiles  cloud={cloud}")
        return 0

    # ingest
    src = REGISTRY[a.dataset]
    acquisitions = getattr(src, "ACQUISITIONS", False)
    from .log import setup as _setup_log, Progress
    log = _setup_log(a.dataset)
    aoi = None
    if a.geojson:
        from .aoi import load_geojson
        aoi = load_geojson(a.geojson)
        a.bbox = list(aoi.bounds)
        log.info(f"AOI: {a.geojson} (bbox={tuple(round(v, 4) for v in aoi.bounds)})")
    elif a.point:
        from .aoi import point_buffer
        lat, lon = a.point
        aoi = point_buffer(lon, lat, a.buffer)
        a.bbox = list(aoi.bounds)
        log.info(f"AOI: point {lat}, {lon} with {a.buffer} m buffer")
    if not a.bbox:
        ap.error("ingest needs --bbox, --geojson or --point")

    bbox = tuple(a.bbox)
    t0 = time.time()

    if getattr(src, "STATIC", False):
        if a.dry_run:
            # Static sources have no scene plan to check against; refuse rather than fall
            # through to the real ingest, which would download while the user asked for a check.
            ap.error(f"{a.dataset} is static and does not support --dry-run")
        writer = Writer(a.dataset, max_buffer_mb=a.flush_mb, log=log.info)
        skip = set()
        for res in src.RES_LIST:
            skip |= writer.existing_keys(res, [""])
        log.info(f"[{a.dataset}] static ingest, bbox={bbox}")
        log.info(f"  {len(skip)} tiles already cached (will be skipped)")
        import inspect
        extra = {"workers": a.workers} if "workers" in inspect.signature(src.ingest).parameters else {}
        total = src.ingest(bbox, writer, bands=a.bands, skip_keys=skip,
                           aoi=aoi, log=log.info, **extra)
        writer.close()
        for f, n in writer.written.items():
            log.info(f"  wrote: {f} (+{n} rows)")
        sealed = compact(a.dataset)     # no open parts after a finished run (see the dated path)
        if sealed:
            log.info(f"  sealed {len(sealed)} (res, month) groups")
        _report_open(a.dataset, log.info)
        ncat = catalog.rebuild(a.dataset)
        log.info(f"  catalog updated: {ncat} rows")
        log.info(f"done: {total} new tiles in {time.time() - t0:.0f}s"
                 + (f", {writer.left_out} files, cells or tiles left out (re-run the same "
                    f"command to fetch them)" if writer.left_out else ""))
        return 2 if writer.left_out else 0

    if not (a.start and a.end):
        ap.error(f"{a.dataset} is time-varying: --start and --end are required")

    log.info(f"[{a.dataset}] STAC search: {a.start}..{a.end} bbox={bbox}")
    items = src.search(bbox, a.start, a.end, cloud_max=a.cloud_max, limit=a.max_scenes)
    log.info(f"  {len(items)} scenes found")
    if not items:
        return 0

    writer = Writer(a.dataset, max_buffer_mb=a.flush_mb, log=log.info)
    months = set()
    for item in items:
        months.update(scene_months(item))
    months = sorted(months)
    res_list = sorted({r for _, r in
                       (src.BAND_TO_ASSET.values() if hasattr(src, "BAND_TO_ASSET")
                        else [(None, src.RES)])})
    # what is stored already: a set of tile keys, or for a source with acquisitions the
    # scenes each stored tile holds (a partly empty tile may still gain from another scene)
    skip = {} if acquisitions else set()
    for res in res_list:
        skip |= writer.existing_scenes(res, months) if acquisitions else writer.existing_keys(res, months)
    if acquisitions:
        full = sum(1 for held in skip.values() if held is None)
        log.info(f"  {len(skip)} tiles already stored ({full} complete, "
                 f"{len(skip) - full} partly empty: those can still gain from other scenes)")
    else:
        log.info(f"  {len(skip)} tiles already cached (will be skipped)")

    if a.dry_run:
        # Coverage check against the store: what would still be fetched.
        # Exit 0 when nothing is missing, 1 otherwise. Tiles a scene holds no
        # data for are never stored, so a small residue on scene edges is
        # normal; a whole scene reporting all its tiles is a gap.
        if not hasattr(src, "plan_scene"):
            ap.error(f"{a.dataset} does not support --dry-run")
        missing = 0
        for i, item in enumerate(items, 1):
            plan = src.plan_scene(item, bbox, bands=a.bands, skip_keys=skip,
                                  keep_offzone=a.keep_offzone, aoi=aoi)
            n = sum(plan.values())
            missing += n
            if n:
                detail = ", ".join(f"{b}:{k}" for b, k in sorted(plan.items()))
                log.info(f"  [{i}/{len(items)}] {item.id}  {n} tiles not in store ({detail})")
        log.info(f"dry-run: {missing} tiles would be fetched across {len(items)} scenes")
        return 0 if missing == 0 else 1

    total = 0
    failed = []               # (scene_id, error) for scenes whose ingest raised
    FLUSH_EVERY = 10          # scenes per chunk; an interrupted run keeps every finished chunk
                              # (the Writer also flushes on its own past --flush-mb)
    SCENE_WORKERS = a.workers # scenes fetched concurrently (each scene fans out over its bands)
    from concurrent.futures import ThreadPoolExecutor

    prog = Progress(len(items))   # elapsed / rate / ETA on every scene line, from completed scenes

    def one(idx_item):
        i, item = idx_item
        try:
            n = src.ingest_scene(item, bbox, writer, bands=a.bands, skip_keys=skip,
                                 log=lambda m: None, keep_offzone=a.keep_offzone, aoi=aoi)
        except Exception as e:      # one bad scene must not sink the chunk
            failed.append((item.id, f"{type(e).__name__}: {e}"))
            log.info(f"  [{i}/{len(items)}] {item.id}  FAILED {type(e).__name__}: {e}  | {prog.tick()}")
            return 0
        log.info(f"  [{i}/{len(items)}] {item.id}  {n} tiles  | {prog.tick()}")
        return n

    with ThreadPoolExecutor(max_workers=SCENE_WORKERS) as ex:
        for start in range(0, len(items), FLUSH_EVERY):
            chunk = list(enumerate(items[start:start + FLUSH_EVERY], start + 1))
            total += sum(ex.map(one, chunk))
            # rows -> new part files (auto-compacted past the threshold). Scenes of one
            # acquisition are merged by the compaction at the end, not after every chunk.
            writer.close(merge=False)

    writer.close()
    for f, n in writer.written.items():
        log.info(f"  wrote: {f} (+{n} rows)")
    # Seal: a finished run leaves no open part files behind. Parts are the write-side mechanism
    # (cheap appends, crash-safe chunks); once the run is over each touched month is merged into
    # its single sealed file. Readers never cared (the catalog names the files), humans do.
    sealed = compact(a.dataset)
    if sealed:
        log.info(f"  sealed {len(sealed)} (res, month) groups")
    _report_open(a.dataset, log.info)
    ncat = catalog.rebuild(a.dataset)
    log.info(f"  catalog updated: {ncat} rows")
    if failed:
        log.info(f"  {len(failed)} scenes FAILED (re-run the same command to retry them):")
        for sid, err in failed:
            log.info(f"    {sid}  {err}")
    log.info(f"done: {total} new tiles, {prog.summary()}"
          + (f", {len(failed)} scenes failed" if failed else ""))
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
