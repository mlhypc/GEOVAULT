"""AOI (polygon) helpers.

The polygon is only a SELECTOR: it decides which tiles to fetch and where to
clip at read time. Tiles themselves stay polygon-free (keyed to the ground), so
overlapping AOIs share tiles and redrawing an AOI never invalidates stored data.
"""

import json

import numpy as np
import shapely
from pyproj import Transformer
from shapely.geometry import shape, box
from shapely.ops import unary_union


def load_geojson(path):
    """GeoJSON file (Feature / FeatureCollection / bare geometry) -> shapely geometry, WGS84."""
    with open(path, encoding="utf-8") as f:
        gj = json.load(f)
    if gj.get("type") == "FeatureCollection":
        return unary_union([shape(feat["geometry"]) for feat in gj["features"]])
    if gj.get("type") == "Feature":
        return shape(gj["geometry"])
    return shape(gj)


def tile_filter(aoi):
    """Returns f(wgs84_bounds) -> bool: does this tile intersect the polygon?
    aoi None means everything passes (plain bbox mode)."""
    if aoi is None:
        return lambda b: True
    import math
    from shapely.prepared import prep
    hit = prep(aoi).intersects          # prepared: thousands of parcels, thousands of tile tests

    def f(b):
        # A tile of an interrupted projection (SoilGrids' Goode Homolosine) can fall outside the
        # projection's domain when the AOI bbox is very wide (parcels on several continents): its
        # corners come back as NaN/Inf from the transform, and GEOS raises on a NaN box. No ground
        # there anyway, so such a tile never intersects.
        if not all(math.isfinite(v) for v in b):
            return False
        if b[0] > b[2]:                     # a tile across the antimeridian: both its ends
            return hit(box(b[0], b[1], 180.0, b[3])) or hit(box(-180.0, b[1], b[2], b[3]))
        return hit(box(b[0], b[1], b[2], b[3]))
    return f


def _moved(geom, transformer):
    """A geometry with every vertex passed through a pyproj transformer."""
    def move(xy):
        return np.column_stack(transformer.transform(xy[:, 0], xy[:, 1]))
    return shapely.transform(geom, move)


def to_crs(geom, crs):
    """WGS84 shapely geometry -> native CRS (for read-time clipping)."""
    return _moved(geom, Transformer.from_crs("EPSG:4326", crs, always_xy=True))


def point_buffer(lon: float, lat: float, meters: float):
    """Square buffer around a WGS84 point, sized in meters (built in the point's
    canonical UTM zone, returned in WGS84). Turns a point into a small AOI."""
    from .sources.common import canonical_epsg
    crs = canonical_epsg(lon, lat)
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    inv = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    x, y = fwd.transform(lon, lat)
    b = box(x - meters, y - meters, x + meters, y + meters)
    return _moved(b, inv)
