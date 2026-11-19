"""Reader: the API other projects import.

    from geovault.reader import tiles_for_bbox, read_tile, mosaic, clip, series

Lookups go through the catalog (never glob the store); pixel reads open only the
month files the catalog names. Everything returns numpy plus geo metadata.
DuckDB-only alternative for non-Python consumers:

    SELECT ... FROM read_parquet('GEOVAULT/data/catalog/s2.parquet') WHERE ...
"""

import math
import warnings
from pathlib import Path

import duckdb
import numpy as np

from . import catalog
from .blob import decode as _decode
from .sources.common import observed as _observed


def tiles_for_bbox(dataset, bbox, band=None, date=None) -> list:
    """Catalog rows intersecting a WGS84 bbox. date: None or any prefix of the stored date
    strings ('YYYY', 'YYYY-MM', 'YYYY-MM-DD', 'YYYY-MM-DDTHH', ...). Returns a list of dict
    rows (no pixels)."""
    p = catalog.path(dataset)
    if not p.exists():
        return []
    where, args = [catalog.bbox_sql(bbox)], []
    if band:
        where.append("band = ?")
        args.append(band)
    if date:
        where.append("date LIKE ?")
        args.append(f"{date}%")
    con = duckdb.connect()
    cur = con.execute(
        f"SELECT * FROM read_parquet('{p.as_posix()}') "
        f"WHERE {' AND '.join(where)} ORDER BY band, date, x, y", args)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    con.close()
    return rows


def read_tile(row):
    """One catalog row -> (array, rasterio profile). Opens only the named file.
    The CRS and the acquisition are part of the tile key: UTM tile numbers restart in
    every zone, so the same (x, y) can be stored twice in one file, for ground in two
    zones, and a date can hold the same tile from two overpasses."""
    sql = (f"SELECT data FROM read_parquet('{Path(row['file']).as_posix()}') "
           f"WHERE band=? AND date=? AND x=? AND y=? "
           f"AND crs IS NOT DISTINCT FROM ? AND acq IS NOT DISTINCT FROM ?")
    args = [row["band"], row["date"], row["x"], row["y"], row["crs"], row["acq"]]
    if row["acq"] is not None:          # until a month is compacted, one row per scene
        sql += " AND scene_id IS NOT DISTINCT FROM ?"
        args.append(row["scene_id"])
    with duckdb.connect() as con:
        found = con.execute(sql, args).fetchall()
    if len(found) != 1:
        raise RuntimeError(f"catalog row matches {len(found)} stored tiles in {row['file']} "
                           f"(band {row['band']} date {row['date']} x {row['x']} y {row['y']}): "
                           f"rebuild the catalog")
    return _decode(found[0][0])


def _needs_nodata(band, nodata):
    if nodata is None:
        raise ValueError(f"band {band!r} has no nodata value, so a pixel without an observation "
                         f"cannot be marked in a plain array: call with masked=True")


def _request_points(bbox, within, row):
    """(lon, lat) points sampling a request: the WGS84 bbox, or the geometry `within`.

    A lattice of points an eighth of a tile apart (`row` is a catalog row of such a
    tile), with at least 32 points per axis (8 for each part of a multi-part geometry)
    and at most 512, so a small request is sampled finer than its pixels and the step
    exceeds an eighth of a tile only once a request is wider than 64 tiles. Each part of
    a geometry gets its own lattice, its vertices and a point inside it, so a small or
    thin part is never skipped."""
    # the width of the tile in longitude, also when it lies across the antimeridian
    step_x = ((row["east"] - row["west"]) % 360 or 360.0) / 8
    step_y = (row["north"] - row["south"]) / 8

    def lattice(w, s, e, n, least):
        nx = int(np.clip(np.ceil((e - w) / step_x), least, 512))
        ny = int(np.clip(np.ceil((n - s) / step_y), least, 512))
        x, y = np.meshgrid(w + (np.arange(nx) + 0.5) / nx * (e - w),
                           s + (np.arange(ny) + 0.5) / ny * (n - s))
        return x.ravel(), y.ravel()

    if within is None:
        return lattice(*bbox, 32)
    from shapely import contains_xy, get_coordinates
    parts = list(getattr(within, "geoms", [within]))
    lons, lats = [], []
    for part in parts:
        x, y = lattice(*part.bounds, 32 if len(parts) == 1 else 8)
        inside = contains_xy(part, x, y)
        own = np.vstack([get_coordinates(part), get_coordinates(part.representative_point())])
        lons += [x[inside], own[:, 0]]
        lats += [y[inside], own[:, 1]]
    return np.concatenate(lons), np.concatenate(lats)


def _seen_at(observed, transform, crs, lon, lat, reach=0):
    """Per (lon, lat) point: does the mosaic (its observation mask and georeferencing)
    hold an observation there, or within `reach` pixels of it?"""
    from pyproj import Transformer
    x, y = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(lon, lat)
    col = np.floor((x - transform.c) / transform.a).astype(int)
    row = np.floor((y - transform.f) / transform.e).astype(int)
    nrows, ncols = observed.shape
    seen = np.zeros(col.size, bool)
    for dr in range(-reach, reach + 1):
        for dc in range(-reach, reach + 1):
            r, c = row + dr, col + dc
            ok = (r >= 0) & (r < nrows) & (c >= 0) & (c < ncols)
            seen[ok] |= observed[r[ok], c[ok]]
    return seen


def _left_out(observed, transform, crs, dropped, bbox, within):
    """True when tiles of another CRS, dropped to keep the mosaic in one CRS, hold an
    observation on ground of the request where the mosaic has none.

    observed, transform, crs: the mosaic's observation mask and its georeferencing.
    dropped: the (row, array, profile) tiles left out. The request is the WGS84 bbox,
    narrowed to the geometry `within` when given, sampled by _request_points: a left-out
    strip narrower than the lattice step can go unnoticed. A point counts as covered
    when the mosaic has an observation within one pixel of it: the same swath resampled
    to two CRS differs by a pixel along its edge."""
    from pyproj import Transformer
    lon, lat = _request_points(bbox, within, dropped[0][0])

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
    return not _seen_at(observed, transform, crs, lon[elsewhere], lat[elsewhere], reach=1).all()


def _compose(tiles, bbox, band, masked):
    """Tiles (row, array, profile) -> (array, observed, nodata, transform, crs, dropped):
    the mosaic in one CRS and the tiles of other CRS that were left out of it."""
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
    return out, observed, nodata, Affine(res, 0, x0, 0, -res, y1), crs, dropped


def _mosaic(dataset, bbox, band, date, masked, within=None, acq=None):
    """(array, observed, nodata, transform, crs) or None. `observed` is False where no stored
    tile covers a pixel or the tile holds nodata there; those pixels of `array` hold the
    band's nodata value, or 0 when the band has none (allowed only with `masked`). `within`
    narrows the request the two warnings look at from the bbox to a WGS84 geometry."""
    found = tiles_for_bbox(dataset, bbox, band, date)
    exact = [r for r in found if r["date"] == date]
    rows = exact or found
    if acq is not None:
        rows = [r for r in rows if r["acq"] == acq]
    if not rows:
        return None
    tiles = [None] * len(rows)              # one query per month file, not one per tile
    for i, blob in _blobs_by_row(rows):
        tiles[i] = (rows[i], *_decode(blob))

    # One acquisition: a date can hold two overpasses of the same ground (Sentinel-1 in
    # the morning and in the evening, two Sentinel-2 satellites), and they are different
    # images. Without a choice by the caller, the one observed on most of the request;
    # an acquisition whose tiles reach the request without an observation on it does not
    # count. Tiles without an acquisition (None) are a group of their own.
    order = lambda n: (n is None, n or "")
    names = sorted({r["acq"] for r in rows}, key=order)
    if exact and acq is None and len(names) > 1:
        lon, lat = _request_points(bbox, within, rows[0])
        made, cover = {}, {}
        for name in names:
            made[name] = _compose([t for t in tiles if t[0]["acq"] == name], bbox, band, masked)
            cover[name] = int(_seen_at(made[name][1], made[name][3], made[name][4], lon, lat).sum())
        seen = [n for n in names if cover[n]] or names[:1]
        best = min(seen, key=lambda n: (-cover[n], order(n)))
        if len(seen) > 1:
            say = lambda n: f"{n or 'tiles without an acquisition'} ({cover[n]})"
            warnings.warn(f"{dataset} {band} {date}: {len(seen)} acquisitions on this date observed "
                          f"the requested area. The result is {say(best)}, not "
                          f"{', '.join(say(n) for n in seen if n != best)}: in brackets, how many "
                          f"of {lon.size} sample points of the request each one observed. "
                          f"Pass acq= to choose.", stacklevel=3)
        out, observed, nodata, transform, crs, dropped = made[best]
    else:
        out, observed, nodata, transform, crs, dropped = _compose(tiles, bbox, band, masked)

    if dropped and _left_out(observed, transform, crs, dropped, bbox, within):
        others = ", ".join(sorted({str(t[2]["crs"]) for t in dropped}))
        warnings.warn(f"{dataset} {band} {date}: the requested area is stored in more than one "
                      f"CRS; the result is in {crs} and leaves out ground observed only in "
                      f"tiles of {others}. Request each side separately to read it all.",
                      stacklevel=3)
    return out, observed, nodata, transform, crs


def acquisitions(dataset, bbox, band, date) -> list:
    """The acquisitions with a stored tile of one band and date over a WGS84 bbox,
    sorted: the values `mosaic` and `clip` take as `acq=` and `series` records carry.
    A tile reaching the bbox is enough; it need not hold an observation inside it.
    Empty when nothing is stored there, and for sources whose tiles carry no
    acquisition."""
    rows = tiles_for_bbox(dataset, bbox, band, date)
    return sorted({r["acq"] for r in rows if r["date"] == date and r["acq"] is not None})


def _masked(arr, observed, nodata):
    return np.ma.MaskedArray(arr, mask=~observed, fill_value=nodata)


def mosaic(dataset, bbox, band, date, masked=False, acq=None):
    """Mosaic all tiles of one (band, date) over a bbox into a single array.

    Overlapping tiles fill each other's gaps, and the cleanest tile (lowest
    cloud_pct) wins where both have data. The output is in one CRS: when the
    tiles over the bbox are in several (a UTM zone line, or scenes of two zones
    on a multi-date request) the zone the MGRS grid assigns to the bbox center
    is preferred, else the CRS with the most tiles, and the tiles of the others
    are not used. A warning is raised when that leaves out ground observed only
    in them.

    A date can hold several acquisitions of the same ground (two overpasses on
    one day). On an exact date they are never blended: acq names the one to
    read (see `acquisitions`); without it the one observed on most of the
    request is read, the first by name when equal, and a warning names it and
    the others. A date prefix ('YYYY-MM') is a composite over everything it
    matches; acq then narrows it to the acquisitions of that name (a Landsat
    path and satellite recur every 16 days).

    A pixel without an observation (no stored tile covers it, or the tile holds
    nodata there) gets the band's nodata value. masked=True returns a numpy
    masked array with exactly those pixels masked instead; a band that has no
    nodata value can only be read that way, and raises ValueError otherwise.
    Returns (array, transform, crs) in native CRS, or None."""
    m = _mosaic(dataset, bbox, band, date, masked, acq=acq)
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
    yielded file by file, so memory holds one batch at a time, not the whole request.

    A row the catalog names but its file no longer holds raises, and so does a row that
    matches several stored tiles, as read_tile does: the catalog is stale and must be
    rebuilt."""
    by_file = {}
    for i, r in enumerate(rows):
        by_file.setdefault(r["file"], []).append(i)
    with duckdb.connect() as con:
        for f, idx in by_file.items():
            want, served = {}, set()
            for i in idx:
                r = rows[i]
                scene = r["scene_id"] if r["acq"] is not None else None
                want.setdefault((r["band"], r["date"], r["x"], r["y"], r["crs"], r["acq"],
                                 scene), []).append(i)
            sub = [rows[i] for i in idx]
            dates = [r["date"] for r in sub]
            bands, xs, ys = (sorted({r[c] for r in sub}) for c in ("band", "x", "y"))
            # The values are listed in the query, one placeholder each. Handed over as
            # one list to unnest in a subquery they are joined to the file, which took
            # twice as long on most reads (37 against 15 ms for one tile of a static
            # file of 1.4 GB, 21 against 6 ms for 35 tiles of a month).
            listed = lambda values: ", ".join("?" * len(values))
            cur = con.execute(
                f"SELECT band, date, x, y, crs, acq, scene_id, data "
                f"FROM read_parquet('{Path(f).as_posix()}') "
                f"WHERE band IN ({listed(bands)}) AND date BETWEEN ? AND ? "
                f"AND x IN ({listed(xs)}) AND y IN ({listed(ys)})",
                [*bands, min(dates), max(dates), *xs, *ys])
            while batch := cur.fetchmany(256):
                for band, d, x, y, crs, acq, scene, data in batch:
                    key = (band, d, x, y, crs, acq, scene if acq is not None else None)
                    if key not in want:
                        continue
                    if key in served:
                        raise RuntimeError(f"a catalog row matches several stored tiles in {f} "
                                           f"(band {band} date {d} x {x} y {y}): rebuild the catalog")
                    served.add(key)
                    for i in want[key]:
                        yield i, data
            left = [rows[i] for key, v in want.items() if key not in served for i in v]
            if left:
                r = left[0]
                raise RuntimeError(f"{len(left)} catalog rows not found in {f} (e.g. band {r['band']} "
                                   f"date {r['date']} x {r['x']} y {r['y']}): rebuild the catalog")


def series(dataset, lon, lat, band, date=None):
    """Pixel time series at a WGS84 point.

    For each stored date, reads only the tile containing the point and extracts
    the single pixel value; the blobs come from one query per month file. Returns a
    list of dicts: {date, value, cloud_pct, scene_id, crs, acq}, oldest first, nodata
    pixels skipped. A date has one record per stored tile at the point: one per
    acquisition when two overpasses imaged it that day (acq is None for sources without
    acquisitions), and one per CRS where the point is stored in two (see docs/storage.md on
    UTM zones). date: None | 'YYYY' | 'YYYY-MM' | any prefix of the stored date strings."""
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
                    "cloud_pct": r["cloud_pct"], "scene_id": r["scene_id"], "crs": crs,
                    "acq": r["acq"]})
    out.sort(key=lambda d: (d["date"], d["acq"] or "", d["crs"]))
    return out


def clip(dataset, geom_or_path, band, date, all_touched=True, masked=False, acq=None):
    """Mosaic clipped to a polygon. geom_or_path is a shapely geometry (WGS84) or a
    GeoJSON file path. Pixels outside the polygon are set to the band's nodata
    value, like the pixels without an observation inside it. masked=True returns
    a numpy masked array with those pixels masked instead; a band that has no
    nodata value can only be read that way, and raises ValueError otherwise.
    acq: the acquisition to read when the date holds several (see `mosaic`).
    Returns (array, transform, crs) or None."""
    from .aoi import load_geojson, to_crs
    geom = load_geojson(geom_or_path) if isinstance(geom_or_path, (str, Path)) else geom_or_path
    m = _mosaic(dataset, geom.bounds, band, date, masked, within=geom, acq=acq)
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
