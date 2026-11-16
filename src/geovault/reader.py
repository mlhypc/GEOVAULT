"""Reader: the API other projects import.

    from geovault.reader import tiles_for_bbox, read_tile, mosaic, clip

Lookups go through the catalog (never glob the store); pixel reads open only the
month files the catalog names. Everything returns numpy plus geo metadata.
DuckDB-only alternative for non-Python consumers:

    SELECT ... FROM read_parquet('GEOVAULT/data/catalog/s2.parquet') WHERE ...
"""

import io
import math
import warnings
from pathlib import Path

import duckdb
import numpy as np
import rasterio

from . import catalog


def tiles_for_bbox(dataset, bbox, band=None, date=None) -> list:
    """Catalog rows intersecting a WGS84 bbox. date: None or any prefix of the stored date
    strings ('YYYY', 'YYYY-MM', 'YYYY-MM-DD', 'YYYY-MM-DDTHH', ...). Returns a list of dict
    rows (no pixels)."""
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
    # crs is in catalogs built by 0.5.0 and later; older catalogs yield NULL until rebuilt
    cur = con.execute(
        f"SELECT * FROM read_parquet('{p.as_posix()}', union_by_name=true) "
        f"WHERE {' AND '.join(where)} ORDER BY band, date, x, y")
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    con.close()
    return rows


def read_tile(row):
    """One catalog row -> (array, rasterio profile). Opens only the named file.
    The CRS is part of the tile key: UTM tile numbers restart in every zone, so the
    same (x, y) can be stored twice in one file, for ground in two zones."""
    sql = (f"SELECT data FROM read_parquet('{Path(row['file']).as_posix()}', "
           f"union_by_name=true) WHERE band=? AND date=? AND x=? AND y=?")
    args = [row["band"], row["date"], row["x"], row["y"]]
    if row.get("crs") is not None:
        sql += " AND crs=?"
        args.append(row["crs"])
    with duckdb.connect() as con:
        blob = con.execute(sql, args).fetchone()[0]
    return _decode(blob)


def _observed(arr, nodata):
    """True where a pixel holds an observation. A band without a nodata value has one
    everywhere."""
    if nodata is None:
        return np.ones(arr.shape, bool)
    if nodata != nodata:                    # NaN
        return ~np.isnan(arr)
    return arr != nodata


def _needs_nodata(band, nodata):
    if nodata is None:
        raise ValueError(f"band {band!r} has no nodata value, so a pixel without an observation "
                         f"cannot be marked in a plain array: call with masked=True")


def _left_out(observed, transform, crs, dropped, bbox, within):
    """True when tiles of another CRS, dropped to keep the mosaic in one CRS, hold an
    observation on ground of the request where the mosaic has none.

    observed, transform, crs: the mosaic's observation mask and its georeferencing.
    dropped: the (row, array, profile) tiles left out. The request is the WGS84 bbox,
    narrowed to the geometry `within` when given.

    The request is sampled on a lattice of points an eighth of a tile apart, with at least
    32 points per axis (8 for each part of a multi-part geometry) and at most 512, so a
    small request is sampled finer than its pixels, and a left-out strip narrower than the
    lattice step can go unnoticed (the step exceeds an eighth of a tile once a request is
    wider than 64 tiles). Each part of a geometry gets its own lattice, its vertices and
    a point inside it, so a small or thin part is never skipped. A point counts as covered
    when the mosaic has an observation within one pixel of it: the same swath resampled to
    two CRS differs by a pixel along its edge."""
    from pyproj import Transformer
    r0 = dropped[0][0]
    step_x, step_y = (r0["east"] - r0["west"]) / 8, (r0["north"] - r0["south"]) / 8

    def lattice(w, s, e, n, least):
        nx = int(np.clip(np.ceil((e - w) / step_x), least, 512))
        ny = int(np.clip(np.ceil((n - s) / step_y), least, 512))
        x, y = np.meshgrid(w + (np.arange(nx) + 0.5) / nx * (e - w),
                           s + (np.arange(ny) + 0.5) / ny * (n - s))
        return x.ravel(), y.ravel()

    if within is None:
        lon, lat = lattice(*bbox, 32)
    else:
        from shapely import contains_xy, get_coordinates
        parts = list(getattr(within, "geoms", [within]))
        lons, lats = [], []
        for part in parts:
            x, y = lattice(*part.bounds, 32 if len(parts) == 1 else 8)
            inside = contains_xy(part, x, y)
            own = np.vstack([get_coordinates(part), get_coordinates(part.representative_point())])
            lons += [x[inside], own[:, 0]]
            lats += [y[inside], own[:, 1]]
        lon, lat = np.concatenate(lons), np.concatenate(lats)

    # which sample points hold an observation in a dropped tile
    elsewhere = np.zeros(lon.size, bool)
    by_crs = {}
    for _, arr, prof in dropped:
        by_crs.setdefault(str(prof["crs"]), []).append((arr, prof))
    for src, group in by_crs.items():
        x, y = Transformer.from_crs("EPSG:4326", src, always_xy=True).transform(lon, lat)
        t0 = group[0][1]["transform"]
        sx, sy = t0.a * group[0][1]["width"], -t0.e * group[0][1]["height"]
        tiles_at = {}
        for arr, prof in group:
            t = prof["transform"]
            cell = (round((t.c - t0.c) / sx), round((t0.f - t.f) / sy))
            tiles_at.setdefault(cell, []).append((arr, prof))
        cells = np.stack([np.floor((x - t0.c) / sx), np.floor((t0.f - y) / sy)]).astype(np.int64)
        keys, inverse = np.unique(cells, axis=1, return_inverse=True)
        inverse = inverse.ravel()
        order = np.argsort(inverse, kind="stable")
        parts = np.split(order, np.cumsum(np.bincount(inverse))[:-1])
        for key, idx in zip(keys.T.tolist(), parts):
            for arr, prof in tiles_at.get(tuple(key), ()):
                t = prof["transform"]
                col = np.clip(np.floor((x[idx] - t.c) / t.a).astype(int), 0, arr.shape[1] - 1)
                row = np.clip(np.floor((y[idx] - t.f) / t.e).astype(int), 0, arr.shape[0] - 1)
                elsewhere[idx] |= _observed(arr, prof.get("nodata"))[row, col]
    if not elsewhere.any():
        return False

    # of those, which have no observation in the mosaic within one pixel
    kx, ky = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(
        lon[elsewhere], lat[elsewhere])
    col = np.floor((kx - transform.c) / transform.a).astype(int)
    row = np.floor((ky - transform.f) / transform.e).astype(int)
    nrows, ncols = observed.shape
    covered = np.zeros(col.size, bool)
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            r, c = row + dr, col + dc
            ok = (r >= 0) & (r < nrows) & (c >= 0) & (c < ncols)
            covered[ok] |= observed[r[ok], c[ok]]
    return not covered.all()


def _mosaic(dataset, bbox, band, date, masked, within=None):
    """(array, observed, nodata, transform, crs) or None. `observed` is False where no stored
    tile covers a pixel or the tile holds nodata there; those pixels of `array` hold the
    band's nodata value, or 0 when the band has none (allowed only with `masked`). `within`
    narrows the area the single-CRS warning looks at from the bbox to a WGS84 geometry."""
    rows = [r for r in tiles_for_bbox(dataset, bbox, band, date) if r["date"] == date] \
        or tiles_for_bbox(dataset, bbox, band, date)
    if not rows:
        return None
    tiles = [(r, *read_tile(r)) for r in rows]

    # Single CRS: prefer the zone MGRS assigns to the bbox center, else the majority.
    from .sources.common import mgrs_epsg
    want = mgrs_epsg((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)
    crss = sorted({str(t[2]["crs"]) for t in tiles})
    crs = want if want in crss else max(
        crss, key=lambda c: sum(1 for t in tiles if str(t[2]["crs"]) == c))
    dropped = [t for t in tiles if str(t[2]["crs"]) != crs]
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
    if not masked:
        _needs_nodata(band, nodata)
    out = np.full((nrows, ncols), 0 if nodata is None else nodata, dtype=tiles[0][1].dtype)
    observed = np.zeros((nrows, ncols), bool)
    for _, arr, prof in tiles:
        c = int(round((prof["transform"].c - x0) / res))
        r = int(round((y1 - prof["transform"].f) / res))
        dst = out[r:r + px, c:c + px]
        seen = observed[r:r + px, c:c + px]
        take = ~seen & _observed(arr, prof.get("nodata"))
        dst[take] = arr[take]
        seen |= take
    from rasterio.transform import Affine
    transform = Affine(res, 0, x0, 0, -res, y1)
    if dropped and _left_out(observed, transform, crs, dropped, bbox, within):
        others = ", ".join(sorted({str(t[2]["crs"]) for t in dropped}))
        warnings.warn(f"{dataset} {band} {date}: the requested area is stored in more than one "
                      f"CRS; the result is in {crs} and leaves out ground observed only in "
                      f"tiles of {others}. Request each side separately to read it all.",
                      stacklevel=3)
    return out, observed, nodata, transform, crs


def _masked(arr, observed, nodata):
    return np.ma.MaskedArray(arr, mask=~observed, fill_value=nodata)


def mosaic(dataset, bbox, band, date, masked=False):
    """Mosaic all tiles of one (band, date) over a bbox into a single array.

    Overlapping tiles fill each other's gaps, and the cleanest tile (lowest
    cloud_pct) wins where both have data. The output is in one CRS: when the
    tiles over the bbox are in several (a UTM zone line, or scenes of two zones
    on a multi-date request) the zone the MGRS grid assigns to the bbox center
    is preferred, else the CRS with the most tiles, and the tiles of the others
    are not used. A warning is raised when that leaves out ground observed only
    in them.

    A pixel without an observation (no stored tile covers it, or the tile holds
    nodata there) gets the band's nodata value. masked=True returns a numpy
    masked array with exactly those pixels masked instead; a band that has no
    nodata value can only be read that way, and raises ValueError otherwise.
    Returns (array, transform, crs) in native CRS, or None."""
    m = _mosaic(dataset, bbox, band, date, masked)
    if m is None:
        return None
    arr, observed, nodata, transform, crs = m
    if masked:
        return _masked(arr, observed, nodata), transform, crs
    return arr, transform, crs


def _blobs_by_row(rows):
    """Yield (row index, pixel blob) for many catalog rows, one query per month file instead
    of one per row. Hourly and sub-hourly layers hold thousands of dates per month; a query
    per date cost about 20 ms each, mostly connection and file-open overhead. Blobs are
    yielded file by file, so memory holds one batch at a time, not the whole series.

    A row the catalog names but its file no longer holds raises, as read_tile does: the
    catalog is stale and must be rebuilt. Catalogs built before 0.5.0 have no crs column;
    their rows match a stored tile on (band, date, x, y), and when one file holds the same
    tile numbers in two UTM zones each catalog row takes a different stored tile."""
    by_file = {}
    for i, r in enumerate(rows):
        by_file.setdefault(r["file"], []).append(i)
    with duckdb.connect() as con:
        for f, idx in by_file.items():
            want = {}
            for i in idx:
                r = rows[i]
                want.setdefault((r["band"], r["date"], r["x"], r["y"], r.get("crs")), []).append(i)
            sub = [rows[i] for i in idx]
            dates = [r["date"] for r in sub]
            cur = con.execute(
                f"SELECT band, date, x, y, crs, data FROM read_parquet('{Path(f).as_posix()}', "
                f"union_by_name=true) WHERE band IN (SELECT unnest(?)) AND date BETWEEN ? AND ? "
                f"AND x IN (SELECT unnest(?)) AND y IN (SELECT unnest(?))",
                [sorted({r["band"] for r in sub}), min(dates), max(dates),
                 sorted({r["x"] for r in sub}), sorted({r["y"] for r in sub})])
            while batch := cur.fetchmany(256):
                for band, d, x, y, crs, data in batch:
                    hits = want.get((band, d, x, y, crs)) or want.get((band, d, x, y, None))
                    if hits:
                        yield hits.pop(), data
            left = [rows[i] for v in want.values() for i in v]
            if left:
                r = left[0]
                raise RuntimeError(f"{len(left)} catalog rows not found in {f} (e.g. band {r['band']} "
                                   f"date {r['date']} x {r['x']} y {r['y']}): rebuild the catalog")


def _decode(blob):
    with rasterio.MemoryFile(io.BytesIO(bytes(blob))) as mem:
        with mem.open() as ds:
            return ds.read(1), ds.profile


def series(dataset, lon, lat, band, date=None):
    """Pixel time series at a WGS84 point.

    For each stored date, reads only the tile containing the point and extracts
    the single pixel value; the blobs come from one query per month file. Returns a
    list of dicts: {date, value, cloud_pct, scene_id, crs}, oldest first, nodata pixels
    skipped. Where the point is stored in two CRS (see the README on UTM zones) a date
    has one record per CRS. date: None | 'YYYY' | 'YYYY-MM' | any prefix of the stored date strings."""
    from pyproj import Transformer
    eps = 1e-9
    rows = tiles_for_bbox(dataset, (lon - eps, lat - eps, lon + eps, lat + eps),
                          band=band, date=date)
    out = []
    tr_cache = {}
    for i, blob in _blobs_by_row(rows):
        r = rows[i]
        arr, prof = _decode(blob)
        crs = str(prof["crs"])
        if crs not in tr_cache:
            tr_cache[crs] = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        x, y = tr_cache[crs].transform(lon, lat)
        t = prof["transform"]
        col = math.floor((x - t.c) / t.a)        # floor, not int(): a point just west or
        row_i = math.floor((y - t.f) / t.e)      # north of the tile must not land in it
        if not (0 <= row_i < arr.shape[0] and 0 <= col < arr.shape[1]):
            continue
        v = arr[row_i, col]
        nd = prof.get("nodata")
        if nd is not None and (v == nd or (nd != nd and v != v)):
            continue
        out.append({"date": r["date"], "value": v.item(),
                    "cloud_pct": r["cloud_pct"], "scene_id": r["scene_id"], "crs": crs})
    out.sort(key=lambda d: d["date"])
    return out


def clip(dataset, geom_or_path, band, date, all_touched=True, masked=False):
    """Mosaic clipped to a polygon. geom_or_path is a shapely geometry (WGS84) or a
    GeoJSON file path. Pixels outside the polygon are set to the band's nodata
    value, like the pixels without an observation inside it. masked=True returns
    a numpy masked array with those pixels masked instead; a band that has no
    nodata value can only be read that way, and raises ValueError otherwise.
    Returns (array, transform, crs) or None."""
    from .aoi import load_geojson, to_crs
    geom = load_geojson(geom_or_path) if isinstance(geom_or_path, (str, Path)) else geom_or_path
    m = _mosaic(dataset, geom.bounds, band, date, masked, within=geom)
    if m is None:
        return None
    arr, observed, nodata, transform, crs = m
    from rasterio.features import geometry_mask
    native = to_crs(geom, crs)
    inside = geometry_mask([native.__geo_interface__], out_shape=arr.shape,
                           transform=transform, invert=True, all_touched=all_touched)
    arr[~inside] = 0 if nodata is None else nodata
    if masked:
        return _masked(arr, observed & inside, nodata), transform, crs
    return arr, transform, crs
