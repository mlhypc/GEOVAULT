"""Shared engine for static COG sources (DEM, land cover, soil).

A static source has no scenes and no dates: given a bbox/AOI, it opens the
public COG assets covering the area and stores the intersecting tiles once
(date is the empty string, partition is static/). The tile lattice is aligned
to each source's own pixel grid via Grid.from_transform, so pixels are stored
exactly as published, no resampling.
"""

import math
import os
import time

import rasterio
from rasterio.transform import Affine, array_bounds

from ..aoi import tile_filter
from ..grid import Grid
from ..log import Progress, fmt_duration
from . import common

GDAL_ENV = dict(AWS_NO_SIGN_REQUEST="YES", GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
                GDAL_HTTP_MULTIPLEX="YES", VSI_CACHE="TRUE")
for _k, _v in GDAL_ENV.items():
    os.environ.setdefault(_k, _v)   # visible to every thread (rasterio.Env is thread-local)

PROGRESS_EVERY = 5000   # tiles between progress lines inside one asset


def degree_cells(bbox, aoi=None, step=1, pad=0.0):
    """South-west corners (lat, lon) of the step x step degree cells covering bbox. With an AOI
    only the cells the polygon touches are yielded: a scattered set of parcels has a huge bbox
    but touches few cells, and every yielded cell costs a remote open.

    `pad` (degrees) widens the search for files whose pixels sit off the degree lines: a
    pixel-centre registered file owns ground half a pixel west and north of its cell, so a
    bbox ending just past a degree line still needs the neighbouring file."""
    w, s, e, n = bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad
    if aoi is not None:
        from shapely.geometry import box
        from shapely.prepared import prep
        hit = prep(aoi).intersects
    for lat in range(math.floor(s / step) * step, math.ceil(n), step):
        for lon in range(math.floor(w / step) * step, math.ceil(e), step):
            cell = box(lon - pad, lat - pad, lon + step + pad, lat + step + pad) if aoi else None
            if aoi is not None and not hit(cell):
                continue
            yield lat, lon


def owned_window(transform, width, height, footprint=None):
    """(transform, width, height) of the part of a raster that an asset owns.

    Some products ship files that overlap their neighbours: SRTM-style 1-degree files of
    3601 x 3601 px share their edge rows, USGS 1 m DEM tiles carry a 6 m buffer. A tile on
    the shared strip would then be written twice with half the pixels each. `footprint`
    (west, south, east, north, native CRS) is the asset's nominal cell; it is snapped to the
    raster's own pixel lattice, so pixels are still stored exactly as published."""
    if footprint is None:
        return transform, width, height
    fw, fs, fe, fn = footprint
    col0 = round((fw - transform.c) / transform.a)
    row0 = round((fn - transform.f) / transform.e)
    ncol = round((fe - fw) / transform.a)
    nrow = round((fs - fn) / transform.e)
    return transform @ Affine.translation(col0, row0), ncol, nrow


def ingest_assets(hrefs, band, writer, bbox, tile_px, skip_keys=None, aoi=None,
                  nodata=None, source_id="", sign=None, log=print) -> int:
    """Open each asset, tile the part intersecting bbox/aoi, store the tiles.

    `hrefs` items are URLs or (URL, footprint) pairs, footprint as in owned_window().
    `sign`, if given, maps a URL to the URL actually opened (e.g. Planetary Computer SAS
    signing); it is applied just before each open so short-lived tokens do not expire.

    Progress goes to `log`: one line per asset (tiles, elapsed, rate, ETA over the assets)
    and one every PROGRESS_EVERY tiles inside an asset, so a Turkey-wide WorldCover run
    (20 assets, 290k tiles, two hours) is never silent for more than a few minutes."""
    skip_keys = skip_keys or set()
    in_aoi = tile_filter(aoi)
    written = 0
    t0 = time.time()
    prog = Progress(len(hrefs))

    failed = 0
    with rasterio.Env(**GDAL_ENV):
        for i, item in enumerate(hrefs, 1):
            href, footprint = item if isinstance(item, tuple) else (item, None)
            name = href.split("?", 1)[0].rsplit("/", 1)[-1]
            try:
                ds = rasterio.open(sign(href) if sign else href)
            except rasterio.errors.RasterioIOError as exc:
                # through /vsizip/ every failure reads "does not exist", so only plain URLs are
                # classified by message; archive sources filter missing cells beforehand
                missing = ("404", "No such file") if href.startswith("/vsizip/") else (
                    "404", "No such file", "does not exist")
                if any(t in str(exc) for t in missing):
                    # assets over open water simply do not exist
                    log(f"    [{i}/{len(hrefs)}] {name}: not found (open water)  | {prog.tick()}")
                else:
                    # expired signature, server error, timeout: the asset exists, rerun to fetch it
                    failed += 1
                    log(f"    [{i}/{len(hrefs)}] {name}: FAILED to open ({exc})  | {prog.tick()}")
                continue
            asset_written = 0
            with ds:
                crs = str(ds.crs) if ds.crs.to_epsg() is None else f"EPSG:{ds.crs.to_epsg()}"
                own_tf, own_w, own_h = owned_window(ds.transform, ds.width, ds.height, footprint)
                grid = Grid.from_transform(crs, own_tf, tile_px)
                if len(hrefs) > 1 and not grid.aligned_with_raster(own_tf, own_w, own_h):
                    # a tile would straddle this file and its neighbour: both files would write the
                    # same key with half the pixels each. Refuse rather than store half-empty tiles.
                    raise ValueError(
                        f"{name}: {own_w}x{own_h} px is not tiled by "
                        f"tile_px={tile_px}; pick a tile_px that divides the file size")
                nd = nodata if nodata is not None else (ds.nodata if ds.nodata is not None else 0)
                nw, ns, ne, nn = common.bbox_to_crs(bbox, crs)
                rl, rbot, rr, rt = array_bounds(own_h, own_w, own_tf)
                w = max(nw, rl); s = max(ns, rbot)
                e = min(ne, rr); n = min(nn, rt)
                if w >= e or s >= n:
                    log(f"    [{i}/{len(hrefs)}] {name}: outside bbox  | {prog.tick()}")
                    continue
                # candidates per AOI part (see common.candidate_tiles), clipped to this raster's
                # extent; the list is materialised so the progress lines can say "of how many"
                todo = []
                for tx, ty in common.candidate_tiles(grid, bbox, crs, aoi):
                    tw, ts, te, tn = grid.tile_bounds(tx, ty)
                    if te <= w or tw >= e or tn <= s or ts >= n:
                        continue
                    if (band, "", crs, tx, ty) in skip_keys:
                        continue
                    wb = common.wgs84_bounds(grid, tx, ty, crs)
                    if in_aoi(wb):
                        todo.append((tx, ty, wb))
                for k, (tx, ty, wb) in enumerate(todo, 1):
                    arr = common.read_tile_window(ds, grid, tx, ty, nd)
                    if arr is not None:
                        blob = common.encode_geotiff(arr, grid, tx, ty, crs, nd)
                        if writer.add(band, "", tx, ty, crs, grid.res, tile_px,
                                      wb, nd, None, source_id, blob):
                            written += 1
                            asset_written += 1
                    if k % PROGRESS_EVERY == 0 and k < len(todo):
                        log(f"      {name}: {k}/{len(todo)} tiles read, {written} stored so far, "
                            f"elapsed {fmt_duration(time.time() - t0)}")
            log(f"    [{i}/{len(hrefs)}] {name}: {asset_written} tiles  | {prog.tick()}")
    log(f"    {band}: {written} tiles")
    if failed:
        log(f"    {band}: {failed} assets failed to open; run the same command again to fetch them")
    return written
