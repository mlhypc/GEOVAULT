"""Shared engine for static COG sources (DEM, land cover, soil).

A static source has no scenes and no dates: given a bbox/AOI, it opens the
public COG assets covering the area and stores the intersecting tiles once
(date is the empty string, partition is static/). The tile lattice is aligned
to each source's own pixel grid via Grid.from_transform, so pixels are stored
exactly as published, no resampling.
"""

import os
import time

import rasterio

from ..aoi import tile_filter
from ..grid import Grid
from ..log import Progress, fmt_duration
from . import common

_ENV = dict(AWS_NO_SIGN_REQUEST="YES", GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
            GDAL_HTTP_MULTIPLEX="YES", VSI_CACHE="TRUE")
for _k, _v in _ENV.items():
    os.environ.setdefault(_k, _v)   # visible to every thread (rasterio.Env is thread-local)

PROGRESS_EVERY = 5000   # tiles between progress lines inside one asset


def ingest_assets(hrefs, band, writer, bbox, tile_px, skip_keys=None, aoi=None,
                  nodata=None, source_id="", log=print) -> int:
    """Open each asset, tile the part intersecting bbox/aoi, store the tiles.

    Progress goes to `log`: one line per asset (tiles, elapsed, rate, ETA over the assets)
    and one every PROGRESS_EVERY tiles inside an asset, so a Turkey-wide WorldCover run
    (20 assets, 290k tiles, two hours) is never silent for more than a few minutes."""
    skip_keys = skip_keys or set()
    in_aoi = tile_filter(aoi)
    written = 0
    t0 = time.time()
    prog = Progress(len(hrefs))

    with rasterio.Env(**_ENV):
        for i, href in enumerate(hrefs, 1):
            name = href.rsplit("/", 1)[-1]
            try:
                ds = rasterio.open(href)
            except rasterio.errors.RasterioIOError:
                log(f"    [{i}/{len(hrefs)}] {name}: not found (open water)  | {prog.tick()}")
                continue   # assets over open water simply do not exist
            asset_written = 0
            with ds:
                crs = str(ds.crs) if ds.crs.to_epsg() is None else f"EPSG:{ds.crs.to_epsg()}"
                grid = Grid.from_transform(crs, ds.transform, tile_px)
                if len(hrefs) > 1 and not grid.aligned_with_raster(ds.transform, ds.width, ds.height):
                    # a tile would straddle this file and its neighbour: both files would write the
                    # same key with half the pixels each. Refuse rather than store half-empty tiles.
                    raise ValueError(
                        f"{href.rsplit('/', 1)[-1]}: {ds.width}x{ds.height} px is not tiled by "
                        f"tile_px={tile_px}; pick a tile_px that divides the file size")
                nd = nodata if nodata is not None else (ds.nodata if ds.nodata is not None else 0)
                nw, ns, ne, nn = common.bbox_to_crs(bbox, crs)
                rb = ds.bounds
                w = max(nw, rb.left); s = max(ns, rb.bottom)
                e = min(ne, rb.right); n = min(nn, rb.top)
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
                    if (band, "", tx, ty) in skip_keys:
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
    return written
