"""GEOVAULT command line.

  geovault ingest --dataset s2 --geojson parcel.geojson --start 2024-06-01 --end 2026-06-01
  geovault ingest --dataset s2 --bbox 35.30 36.71 35.33 36.73 --start ... --end ... [--cloud-max 80]
  geovault coverage s2 [--bbox W S E N] [--date 2024-06]
  geovault compact s2
  geovault rebuild-catalog s2

Also runnable as: python -m geovault ...
"""

import argparse
import sys
import time

from . import catalog
from .sources import REGISTRY
from .store import Writer, compact


def main(argv=None):
    ap = argparse.ArgumentParser(prog="geovault")
    sub = ap.add_subparsers(dest="cmd", required=True)

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
                   help="allow duplicate storage across UTM zone boundaries")
    p.add_argument("--dry-run", action="store_true",
                   help="fetch nothing; report per scene how many tiles are not in the store yet "
                        "(exit 0 = complete, 1 = gaps). Use it to verify coverage after an ingest.")

    p = sub.add_parser("coverage", help="what is stored (dates, bands, tile counts)")
    p.add_argument("dataset")
    p.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"))
    p.add_argument("--date")

    p = sub.add_parser("compact", help="seal a dataset's open month part files")
    p.add_argument("dataset")

    p = sub.add_parser("rebuild-catalog", help="regenerate the catalog from the store")
    p.add_argument("dataset")

    a = ap.parse_args(argv)

    if a.cmd == "compact":
        done = compact(a.dataset)
        print(f"compacted {len(done)} (res, month) groups")
        catalog.rebuild(a.dataset)
        return 0

    if a.cmd == "rebuild-catalog":
        n = catalog.rebuild(a.dataset)
        print(f"catalog rebuilt: {a.dataset} ({n} rows)")
        return 0

    if a.cmd == "coverage":
        rows = catalog.coverage(a.dataset, tuple(a.bbox) if a.bbox else None, a.date)
        if not rows:
            print("no records")
            return 0
        for date, band, tiles, cloud in rows:
            print(f"  {date or 'static':>10}  {band:<4} {tiles:>5} tiles  cloud={cloud}")
        return 0

    # ingest
    aoi = None
    if a.geojson:
        from .aoi import load_geojson
        aoi = load_geojson(a.geojson)
        a.bbox = list(aoi.bounds)
        print(f"AOI: {a.geojson} (bbox={tuple(round(v, 4) for v in aoi.bounds)})")
    elif a.point:
        from .aoi import point_buffer
        lat, lon = a.point
        aoi = point_buffer(lon, lat, a.buffer)
        a.bbox = list(aoi.bounds)
        print(f"AOI: point {lat}, {lon} with {a.buffer} m buffer")
    if not a.bbox:
        ap.error("ingest needs --bbox, --geojson or --point")

    src = REGISTRY[a.dataset]
    bbox = tuple(a.bbox)
    t0 = time.time()

    if getattr(src, "STATIC", False):
        if a.dry_run:
            # Static sources have no scene plan to check against; until they do, refuse rather than
            # silently run the real ingest (a 294-tile Turkey fetch once happened under --dry-run).
            ap.error(f"{a.dataset} is static and does not support --dry-run")
        writer = Writer(a.dataset)
        skip = set()
        for res in src.RES_LIST:
            skip |= writer.existing_keys(res, [""])
        print(f"[{a.dataset}] static ingest, bbox={bbox}")
        print(f"  {len(skip)} tiles already cached (will be skipped)")
        import inspect
        extra = {"workers": a.workers} if "workers" in inspect.signature(src.ingest).parameters else {}
        total = src.ingest(bbox, writer, bands=a.bands, skip_keys=skip,
                           aoi=aoi, log=lambda m: print(m, flush=True), **extra)
        for f, n in writer.close().items():
            print(f"  wrote: {f} (+{n} rows)")
        sealed = compact(a.dataset)     # no open parts after a finished run (see the dated path)
        if sealed:
            print(f"  sealed {len(sealed)} (res, month) groups")
        ncat = catalog.rebuild(a.dataset)
        print(f"  catalog updated: {ncat} rows")
        print(f"done: {total} new tiles in {time.time() - t0:.0f}s")
        return 0

    if not (a.start and a.end):
        ap.error(f"{a.dataset} is time-varying: --start and --end are required")

    print(f"[{a.dataset}] STAC search: {a.start}..{a.end} bbox={bbox}")
    items = src.search(bbox, a.start, a.end, cloud_max=a.cloud_max, limit=a.max_scenes)
    print(f"  {len(items)} scenes found")
    if not items:
        return 0

    writer = Writer(a.dataset)
    months = sorted({i.properties["datetime"][:7] for i in items})
    res_list = sorted({r for _, r in
                       (src.BAND_TO_ASSET.values() if hasattr(src, "BAND_TO_ASSET")
                        else [(None, src.RES)])})
    skip = set()
    for res in res_list:
        skip |= writer.existing_keys(res, months)
    print(f"  {len(skip)} tiles already cached (will be skipped)")

    if a.dry_run:
        # Coverage check against the catalog: what would still be fetched.
        # Exit 0 when nothing is missing, 1 otherwise. Edge tiles outside a
        # scene's footprint are never stored, so a small residue on granule
        # edges is normal; a whole scene reporting all its tiles is a gap.
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
                print(f"  [{i}/{len(items)}] {item.id}  {n} tiles not in store ({detail})")
        print(f"dry-run: {missing} tiles would be fetched across {len(items)} scenes")
        return 0 if missing == 0 else 1

    total = 0
    written = {}
    failed = []               # (scene_id, error) for scenes whose ingest raised
    FLUSH_EVERY = 25          # scenes per chunk; an interrupted run keeps every finished chunk
    SCENE_WORKERS = a.workers # scenes fetched concurrently (each scene fans out over its bands)
    from concurrent.futures import ThreadPoolExecutor

    def one(idx_item):
        i, item = idx_item
        try:
            n = src.ingest_scene(item, bbox, writer, bands=a.bands, skip_keys=skip,
                                 log=lambda m: None, keep_offzone=a.keep_offzone, aoi=aoi)
        except Exception as e:      # one bad scene must not sink the chunk
            failed.append((item.id, f"{type(e).__name__}: {e}"))
            print(f"  [{i}/{len(items)}] {item.id}  FAILED {type(e).__name__}: {e}", flush=True)
            return 0
        print(f"  [{i}/{len(items)}] {item.id}  {n} tiles", flush=True)
        return n

    with ThreadPoolExecutor(max_workers=SCENE_WORKERS) as ex:
        for start in range(0, len(items), FLUSH_EVERY):
            chunk = list(enumerate(items[start:start + FLUSH_EVERY], start + 1))
            total += sum(ex.map(one, chunk))
            part = writer.close()          # rows -> new part files (auto-compacted past the threshold)
            for f, n in part.items():
                written[f] = written.get(f, 0) + n
            if part:
                print(f"  flushed {sum(part.values())} rows to disk", flush=True)

    for f, n in writer.close().items():
        written[f] = written.get(f, 0) + n
    for f, n in written.items():
        print(f"  wrote: {f} (+{n} rows)")
    # Seal: a finished run leaves no open part files behind. Parts are the write-side mechanism
    # (cheap appends, crash-safe chunks); once the run is over each touched month is merged into
    # its single sealed file. Readers never cared (the catalog names the files), humans do.
    sealed = compact(a.dataset)
    if sealed:
        print(f"  sealed {len(sealed)} (res, month) groups")
    ncat = catalog.rebuild(a.dataset)
    print(f"  catalog updated: {ncat} rows")
    if failed:
        print(f"  {len(failed)} scenes FAILED (re-run the same command to retry them):")
        for sid, err in failed:
            print(f"    {sid}  {err}")
    print(f"done: {total} new tiles in {time.time() - t0:.0f}s"
          + (f", {len(failed)} scenes failed" if failed else ""))
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
