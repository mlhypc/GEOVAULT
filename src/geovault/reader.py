"""Reader: the API other projects import.

    from geovault.reader import tiles_for_bbox, read_tile, mosaic, clip

Lookups go through the catalog (never glob the store); pixel reads open only the
month files the catalog names. Everything returns numpy plus geo metadata.
DuckDB-only alternative for non-Python consumers:

    SELECT ... FROM read_parquet('GEOVAULT/data/catalog/s2.parquet') WHERE ...
"""

import io
from pathlib import Path

import duckdb
import numpy as np
import rasterio

from . import catalog


def tiles_for_bbox(dataset, bbox, band=None, date=None) -> list:
    """Catalog rows intersecting a WGS84 bbox. date: None | 'YYYY' | 'YYYY-MM' |
    'YYYY-MM-DD'. Returns a list of dict rows (no pixels)."""
    p = catalog.path(dataset)
    if not p.exists():
        return []
    w, s, e, n = bbox
    where = [f"east > {w} AND west < {e} AND north > {s} AND south < {n}"]
    if band:
        where.append(f"band = '{band}'")
    if date:
        where.append(f"date LIKE '{date}%'")
    con = duckdb.connect()
    cur = con.execute(
        f"SELECT band, date, x, y, west, south, east, north, cloud_pct, scene_id, file "
        f"FROM read_parquet('{p.as_posix()}') WHERE {' AND '.join(where)} "
        f"ORDER BY band, date, x, y")
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    con.close()
    return rows


def read_tile(row):
    """One catalog row -> (array, rasterio profile). Opens only the named file."""
    con = duckdb.connect()
    blob = con.execute(
        f"SELECT data FROM read_parquet('{Path(row['file']).as_posix()}', union_by_name=true) "
        f"WHERE band=? AND date=? AND x=? AND y=?",
        [row["band"], row["date"], row["x"], row["y"]]).fetchone()[0]
    con.close()
    with rasterio.MemoryFile(io.BytesIO(bytes(blob))) as mem:
        with mem.open() as ds:
            return ds.read(1), ds.profile


def mosaic(dataset, bbox, band, date):
    """Mosaic all tiles of one (band, date) over a bbox into a single array.

    Nodata-aware: overlapping tiles fill each other's gaps, and the cleanest
    tile (lowest cloud_pct) wins where both have data. If the bbox spans a UTM
    zone boundary the zone of the bbox center is preferred, so the output is
    single-CRS. Returns (array, transform, crs) in native CRS, or None."""
    rows = [r for r in tiles_for_bbox(dataset, bbox, band, date) if r["date"] == date] \
        or tiles_for_bbox(dataset, bbox, band, date)
    if not rows:
        return None
    tiles = [(r, *read_tile(r)) for r in rows]

    # Single CRS: prefer the canonical zone of the bbox center, else majority.
    from .sources.common import canonical_epsg
    want = canonical_epsg((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)
    crss = {str(t[2]["crs"]) for t in tiles}
    crs = want if want in crss else max(
        crss, key=lambda c: sum(1 for t in tiles if str(t[2]["crs"]) == c))
    tiles = [t for t in tiles if str(t[2]["crs"]) == crs]

    # Cleanest first, so later (cloudier) tiles only fill gaps.
    tiles.sort(key=lambda t: (t[0]["cloud_pct"] if t[0]["cloud_pct"] is not None else 1e9))

    res = tiles[0][2]["transform"].a
    px = tiles[0][1].shape[0]
    xs = [t[2]["transform"].c for t in tiles]
    ys = [t[2]["transform"].f for t in tiles]
    x0, y1 = min(xs), max(ys)
    ncols = int(round((max(xs) - x0) / res)) + px
    nrows = int(round((y1 - min(ys)) / res)) + px
    nodata = tiles[0][2].get("nodata")
    out = np.full((nrows, ncols), nodata, dtype=tiles[0][1].dtype)
    for _, arr, prof in tiles:
        c = int(round((prof["transform"].c - x0) / res))
        r = int(round((y1 - prof["transform"].f) / res))
        dst = out[r:r + px, c:c + px]
        gap = dst == nodata if nodata is not None else np.ones_like(dst, bool)
        incoming = arr != nodata if nodata is not None else np.ones_like(arr, bool)
        dst[gap & incoming] = arr[gap & incoming]
    from rasterio.transform import Affine
    return out, Affine(res, 0, x0, 0, -res, y1), crs


def series(dataset, lon, lat, band, date=None):
    """Pixel time series at a WGS84 point.

    For each stored date, opens only the tile containing the point and extracts
    the single pixel value. Returns a list of dicts:
    {date, value, cloud_pct, scene_id}, oldest first, nodata pixels skipped.
    date: None | 'YYYY' | 'YYYY-MM' to restrict the range."""
    from pyproj import Transformer
    eps = 1e-9
    rows = tiles_for_bbox(dataset, (lon - eps, lat - eps, lon + eps, lat + eps),
                          band=band, date=date)
    out = []
    tr_cache = {}
    for r in rows:
        arr, prof = read_tile(r)
        crs = str(prof["crs"])
        if crs not in tr_cache:
            tr_cache[crs] = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        x, y = tr_cache[crs].transform(lon, lat)
        t = prof["transform"]
        col = int((x - t.c) / t.a)
        row_i = int((y - t.f) / t.e)
        if not (0 <= row_i < arr.shape[0] and 0 <= col < arr.shape[1]):
            continue
        v = arr[row_i, col]
        nd = prof.get("nodata")
        if nd is not None and v == nd:
            continue
        out.append({"date": r["date"], "value": v.item(),
                    "cloud_pct": r["cloud_pct"], "scene_id": r["scene_id"]})
    out.sort(key=lambda d: d["date"])
    return out


def clip(dataset, geom_or_path, band, date, all_touched=True):
    """Polygon -> masked mosaic. geom_or_path is a shapely geometry (WGS84) or a
    GeoJSON file path. Pixels outside the polygon are set to nodata.
    Returns (array, transform, crs) or None."""
    from .aoi import load_geojson, to_crs
    geom = load_geojson(geom_or_path) if isinstance(geom_or_path, (str, Path)) else geom_or_path
    m = mosaic(dataset, geom.bounds, band, date)
    if m is None:
        return None
    arr, transform, crs = m
    from rasterio.features import geometry_mask
    native = to_crs(geom, crs)
    inside = geometry_mask([native.__geo_interface__], out_shape=arr.shape,
                           transform=transform, invert=True, all_touched=all_touched)
    out = arr.copy()
    nodata = 0 if arr.dtype.kind == "u" else -32768
    out[~inside] = nodata
    return out, transform, crs
