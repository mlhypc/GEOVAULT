"""Export: stored pixels out of the vault as files other tools open.

    from geovault.export import geotiff, series_csv
    geotiff("s2", "B4", "2025-07-14", "b4.tif", aoi=polygon)      # one band, one date, one area
    series_csv("chirps", 35.3, 36.7, ["precip"], open("p.csv", "w", newline=""))

Both are the reader's own calls (`clip` / `mosaic`, `series`) with their result written
down: the values are the stored ones, in the CRS and on the pixel grid they are stored in.
"""

import csv
import math
import os
from pathlib import Path

import duckdb
import numpy as np
import rasterio
from rasterio.transform import Affine

from . import catalog, reader
from .sources.common import bbox_to_crs
from .store import scan

CSV_FIELDS = ["band", "date", "value", "cloud_pct", "acq", "crs", "scene_id"]


def _known(dataset, band) -> None:
    """Raise ValueError, naming what there is, when the dataset is not stored or has no
    such band: a misspelled name must not read as an area without data."""
    p = catalog.path(dataset)
    if not p.exists():
        raise ValueError(f"no dataset {dataset!r} is stored")
    with duckdb.connect() as con:
        bands = [r[0] for r in con.execute(
            f"SELECT DISTINCT band FROM {scan([p])} ORDER BY band").fetchall()]
    if band not in bands:
        raise ValueError(f"{dataset} has no band {band!r}; it has {', '.join(bands)}")


def _band_tags(row) -> tuple:
    """(nodata, scale, offset) a stored tile of the band carries; None for one it has not."""
    _, blob = next(reader._blobs_by_row([row]))
    with rasterio.MemoryFile(bytes(blob)) as mem, mem.open() as ds:
        scale = ds.scales[0] if ds.scales and ds.scales[0] != 1.0 else None
        offset = ds.offsets[0] if ds.offsets and ds.offsets[0] != 0.0 else None
        return ds.nodata, scale, offset


def _window(transform, shape, bounds) -> tuple:
    """(row0, row1, col0, col1) of the pixels of a north-up raster that touch a rectangle
    given in its CRS, kept inside the raster. A pixel edge the rectangle only meets
    within a millionth of a pixel does not count: the edge of a pixel is not a number a
    computer holds exactly, and a rectangle drawn along pixel edges means those pixels."""
    w, s, e, n = bounds
    eps = 1e-6
    col0 = max(0, math.floor((w - transform.c) / transform.a + eps))
    col1 = min(shape[1], math.ceil((e - transform.c) / transform.a - eps))
    row0 = max(0, math.floor((n - transform.f) / transform.e + eps))
    row1 = min(shape[0], math.ceil((s - transform.f) / transform.e - eps))
    return row0, row1, col0, col1


def geotiff(dataset, band, date, path, bbox=None, aoi=None, acq=None):
    """Write one band of one date over an area as a GeoTIFF, in the CRS and on the pixel
    grid the tiles are stored in. Returns (rows, columns, crs) of the file, or None, and
    no file, when no stored tile holds an observation there.

    The area is a WGS84 bbox (west, south, east, north) or `aoi`, a shapely geometry in
    WGS84. The file covers the pixels that touch the bounds of the area, as far as tiles
    are stored there: it ends with the outermost stored tile, and with `aoi` the pixels
    outside the polygon hold no value. date: the stored date ('' for a static layer), or
    a prefix of it for a composite, as in `reader.mosaic`; acq: the acquisition to read
    when the date holds several.

    A pixel without a value holds the band's nodata value. A band that has none (every
    value is a measurement) gets a mask inside the file instead, which GDAL, rasterio
    and QGIS read. The scale and offset a source documents (Landsat) are carried over:
    the stored numbers are written as they are. The file is written beside its place
    and moved there when it is whole, so a failure leaves no half-written file and no
    file of before is lost to one.

    Raises ValueError for a dataset that is not stored, a band it does not have, an
    acquisition the date does not hold, and a dated dataset asked for without a date."""
    if (bbox is None) == (aoi is None):
        raise ValueError("give either bbox or aoi")
    _known(dataset, band)
    area = tuple(aoi.bounds) if aoi is not None else tuple(bbox)
    rows = reader.tiles_for_bbox(dataset, area, band, date or None)
    if not rows:
        return None
    if not date and any(r["date"] for r in rows):
        raise ValueError(f"{dataset} is stored by date: give the date to export "
                         f"({rows[0]['date']}), or a prefix of it for a composite")
    if acq is not None and all(r["acq"] != acq for r in rows):
        held = sorted({r["acq"] for r in rows if r["acq"] is not None})
        raise ValueError(f"{dataset} {band} {date} has no acquisition {acq!r} there; it has "
                         + (", ".join(held) or "none"))
    if aoi is not None:
        made = reader.clip(dataset, aoi, band, date, masked=True, acq=acq)
    else:
        made = reader.mosaic(dataset, area, band, date, masked=True, acq=acq)
    if made is None:
        return None
    arr, transform, crs = made
    row0, row1, col0, col1 = _window(transform, arr.shape, bbox_to_crs(area, crs))
    if row0 >= row1 or col0 >= col1:
        return None
    arr = arr[row0:row1, col0:col1]
    seen = ~np.ma.getmaskarray(arr)
    if not seen.any():
        return None
    transform = Affine(transform.a, 0.0, transform.c + col0 * transform.a,
                       0.0, transform.e, transform.f + row0 * transform.e)

    nodata, scale, offset = _band_tags(rows[0])
    profile = dict(driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=1,
                   dtype=arr.dtype, crs=crs, transform=transform, nodata=nodata,
                   compress="deflate", predictor=3 if arr.dtype.kind == "f" else 2)
    if max(arr.shape) > 1024:               # blocks, so a viewer need not read the whole file
        profile.update(tiled=True, blockxsize=256, blockysize=256)
    path = Path(path)
    tmp = path.with_name(path.name + ".part")
    try:
        with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True):
            with rasterio.open(tmp, "w", **profile) as dst:
                dst.write(np.ma.getdata(arr) if nodata is None else arr.filled(nodata), 1)
                if nodata is None:
                    dst.write_mask(seen)
                if scale is not None:
                    dst.scales = [scale]
                if offset is not None:
                    dst.offsets = [offset]
                dst.set_band_description(1, band)
                dst.update_tags(dataset=dataset, band=band, date=date or "static",
                                **({"acq": acq} if acq else {}))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return arr.shape[0], arr.shape[1], crs


def _text(value, dtype) -> str:
    """A pixel value as text that reads back to the same number. A float32 is written
    with the digits that tell it apart from its neighbours (0.1), not with those of the
    float64 it becomes on the way (0.10000000149011612)."""
    if value is None:
        return ""
    if dtype == np.float32:
        return str(np.float32(value))
    return str(value)


def series_csv(dataset, lon, lat, bands, out, date=None) -> int:
    """Write the values of one or more bands at a WGS84 point through time as CSV to the
    text stream `out` (opened with newline=''). One line per record of `reader.series`:
    band, date, value, cloud_pct, acq, crs, scene_id; bands in the order given, each
    oldest first. date: None or a prefix of the stored dates. Returns the number of
    records written; the header is written also when there is none. Raises ValueError
    for a dataset that is not stored and for a band it does not have."""
    for band in bands:
        _known(dataset, band)
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_FIELDS)
    eps, n = 1e-9, 0
    for band in bands:
        records = reader.series(dataset, lon, lat, band, date)
        if not records:
            continue
        rows = reader.tiles_for_bbox(dataset, (lon - eps, lat - eps, lon + eps, lat + eps),
                                     band=band, date=date)
        dtype = reader.read_tile(rows[0])[0].dtype      # the tiles of a band share their type
        for r in records:
            writer.writerow([band, r["date"], _text(r["value"], dtype),
                             "" if r["cloud_pct"] is None else r["cloud_pct"],
                             r["acq"] or "", r["crs"], r["scene_id"] or ""])
        n += len(records)
    return n
