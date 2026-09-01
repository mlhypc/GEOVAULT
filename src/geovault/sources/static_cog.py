"""Shared engine for static COG sources (DEM, land cover, soil).

A static source has no scenes and no dates: given a bbox/AOI, it opens the
public COG assets covering the area and stores the intersecting tiles once
(date is the empty string, partition is static/). The tile lattice is aligned
to each source's own pixel grid via Grid.from_transform, so pixels are stored
exactly as published, no resampling.
"""

import rasterio

from ..aoi import tile_filter
from ..grid import Grid
from . import common

_ENV = dict(AWS_NO_SIGN_REQUEST="YES", GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
            GDAL_HTTP_MULTIPLEX="YES", VSI_CACHE="TRUE")


def ingest_assets(hrefs, band, writer, bbox, tile_px, skip_keys=None, aoi=None,
                  nodata=None, source_id="", log=print) -> int:
    """Open each asset, tile the part intersecting bbox/aoi, store the tiles."""
    skip_keys = skip_keys or set()
    in_aoi = tile_filter(aoi)
    written = 0

    with rasterio.Env(**_ENV):
        for href in hrefs:
            try:
                ds = rasterio.open(href)
            except rasterio.errors.RasterioIOError:
                continue   # assets over open water simply do not exist
            with ds:
                crs = str(ds.crs) if ds.crs.to_epsg() is None else f"EPSG:{ds.crs.to_epsg()}"
                grid = Grid.from_transform(crs, ds.transform, tile_px)
                nd = nodata if nodata is not None else (ds.nodata if ds.nodata is not None else 0)
                nw, ns, ne, nn = common.bbox_to_crs(bbox, crs)
                rb = ds.bounds
                w = max(nw, rb.left); s = max(ns, rb.bottom)
                e = min(ne, rb.right); n = min(nn, rb.top)
                if w >= e or s >= n:
                    continue
                for tx, ty in grid.tiles_for_bounds(w, s, e, n):
                    if (band, "", tx, ty) in skip_keys:
                        continue
                    wb = common.wgs84_bounds(grid, tx, ty, crs)
                    if not in_aoi(wb):
                        continue
                    arr = common.read_tile_window(ds, grid, tx, ty, nd)
                    if arr is None:
                        continue
                    blob = common.encode_geotiff(arr, grid, tx, ty, crs, nd)
                    if writer.add(band, "", tx, ty, crs, grid.res, tile_px,
                                  wb, nd, None, source_id, blob):
                        written += 1
    log(f"    {band}: {written} tiles")
    return written
