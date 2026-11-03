"""Copernicus Global Land Service NDVI 300 m, 10-daily, version 3 (Sentinel-3 OLCI), from the
Copernicus Data Space Ecosystem (CDSE). Free, but NOT anonymous: the COGs live in the CDSE
object store (s3://eodata) and need S3 keys from a free CDSE account. The STAC search itself
is anonymous, so `--dry-run` and coverage planning work without keys.

Why it is here: a single-algorithm 300 m NDVI series from October 2019 to today, 10-day cadence,
about 70 pixels per 0.025 degree cell against about 7 at 1 km. The middle option between the
1 km MODIS overview (`modis_vi_1km`) and full 250 m MODIS (`modis_vi`).

STAC: https://stac.dataspace.copernicus.eu/v1, collection `clms_ndvi_global_300m_10daily_v3_cog`,
one global item per dekad (01, 11, 21), assets `ndvi`, `unc`, `nobs`, `qflag` as global COGs
(120960 x 47040 px, 1.3 GB for NDVI); only the tile windows are range-read. Pixel values are
stored raw with the COG's own nodata, scale and offset copied into each blob (NDVI is a uint8
digital number, physical value = DN * scale + offset; nobs and qflag are counts and bit flags).

Grid: plate carree, 1/336 degree pixels, pixel EDGES at k/336 - 1/672 (the global COG origin is
-180 - 1/672, 80 + 1/672: half a pixel off the degree lines), tiles of 168 px = 0.5 degree,
anchored at (-1/672, +1/672). Scene date = dekad start. cloud_pct = share of nodata pixels.

Credentials: api/cdse_s3 (see api/cdse_s3.example and api/README.md). Keys come from
https://eodata-s3keysmanager.dataspace.copernicus.eu after a free CDSE registration. Reading
goes through GDAL's /vsis3/ with the CDSE endpoint, so rasterio needs nothing extra.
"""

import os
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import rasterio
import requests

from ..grid import Grid
from . import common

STAC_URL = "https://stac.dataspace.copernicus.eu/v1"
COLLECTION = "clms_ndvi_global_300m_10daily_v3_cog"
S3_ENDPOINT = "eodata.dataspace.copernicus.eu"
CRS = "EPSG:4326"
RES = 1.0 / 336.0
TILE_PX = 168                                  # 0.5 degree tiles
OX, OY = -1.0 / 672.0, 1.0 / 672.0             # global COG origin modulo the tile size
WORKERS = 4
TIMEOUT = 120

# band -> STAC asset key (nodata / scale / offset are taken from the COG itself)
ASSETS = {"ndvi": "ndvi", "unc": "unc", "nobs": "nobs", "qflag": "qflag"}
ALL_BANDS = list(ASSETS)
DEFAULT_BANDS = ["ndvi", "qflag"]

_ENV = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_MULTIPLEX="YES", VSI_CACHE="TRUE",
            AWS_S3_ENDPOINT=S3_ENDPOINT, AWS_VIRTUAL_HOSTING="FALSE", AWS_HTTPS="YES",
            AWS_REGION="default")


class Item:
    """Minimal STAC-like item: .id, .properties['datetime'] (dekad start), .assets[key].href."""

    class Asset:
        def __init__(self, href):
            self.href = href

    def __init__(self, feature: dict):
        self.id = feature["id"]
        p = feature["properties"]
        self.properties = dict(p)
        self.properties["datetime"] = (p.get("start_datetime") or p["datetime"])[:19] + "Z"
        self.assets = {k: Item.Asset(a["href"]) for k, a in feature["assets"].items()}
        self.transform = None
        for a in feature["assets"].values():
            if "proj:transform" in a:
                self.transform = a["proj:transform"]
                break


def grid_for() -> Grid:
    return Grid(CRS, RES, TILE_PX, OX, OY)


def _date(item) -> str:
    return item.properties["datetime"][:10]


def _gdal_env() -> dict:
    """GDAL /vsis3/ settings for the CDSE object store, with the user's keys."""
    from ..credentials import cdse_s3
    key, secret = cdse_s3()
    return dict(_ENV, AWS_ACCESS_KEY_ID=key, AWS_SECRET_ACCESS_KEY=secret)


def _vsis3(href: str) -> str:
    return "/vsis3/" + href[len("s3://"):] if href.startswith("s3://") else href


def search(bbox, start, end, cloud_max=None, limit=None):
    """Anonymous STAC search (global items: bbox only sets the time window), oldest first."""
    items = []
    body = {"collections": [COLLECTION], "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z", "limit": 100}
    url = STAC_URL + "/search"
    while url:
        r = requests.post(url, json=body, timeout=TIMEOUT)
        r.raise_for_status()
        j = r.json()
        items += [Item(f) for f in j.get("features", [])]
        nxt = [l for l in j.get("links", []) if l.get("rel") == "next"]
        if not nxt:
            break
        url, body = nxt[0]["href"], nxt[0].get("body", body)
        if nxt[0].get("method", "POST").upper() == "GET":
            r = requests.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            j = r.json()
            items += [Item(f) for f in j.get("features", [])]
            break
    items.sort(key=lambda i: (i.properties["datetime"], i.id))
    return items[:limit] if limit else items


def _bands_of(item, bands):
    return [(b, ASSETS[b]) for b in bands if b in ASSETS and ASSETS[b] in item.assets]


def plan_scene(item, bbox, bands=None, skip_keys=None, keep_offzone=False, aoi=None) -> dict:
    from ..aoi import tile_filter
    bands = bands or DEFAULT_BANDS
    skip_keys = skip_keys or set()
    grid, date = grid_for(), _date(item)
    in_aoi = tile_filter(aoi)
    out = {}
    for band, _ in _bands_of(item, bands):
        n = len(common.plan_tiles(grid, bbox, CRS, band, date, skip_keys, True, in_aoi))
        if n:
            out[band] = n
    return out


def ingest_scene(item, bbox, writer, bands=None, skip_keys=None, log=print,
                 keep_offzone=False, aoi=None) -> int:
    from ..aoi import tile_filter
    bands = bands or DEFAULT_BANDS
    skip_keys = skip_keys or set()
    grid, date = grid_for(), _date(item)
    in_aoi = tile_filter(aoi)
    specs = _bands_of(item, bands)
    if not specs:
        return 0
    env = _gdal_env()

    def fetch_band(spec):
        band, key = spec
        todo = common.plan_tiles(grid, bbox, CRS, band, date, skip_keys, True, in_aoi)
        if not todo:
            return []
        rows = []
        with rasterio.Env(**env), rasterio.open(_vsis3(item.assets[key].href)) as ds:
            if not grid.aligned_with_raster(ds.transform, ds.width, ds.height):
                raise ValueError(f"{item.id} {key}: COG lattice does not match the cgls_ndvi300 grid")
            nodata = ds.nodata
            if nodata is None:
                raise ValueError(f"{item.id} {key}: COG carries no nodata value")
            nodata = np.dtype(ds.dtypes[0]).type(nodata)
            scale = ds.scales[0] if ds.scales and ds.scales[0] != 1.0 else None
            offset = ds.offsets[0] if ds.offsets and ds.offsets[0] != 0.0 else None
            for tx, ty in todo:
                arr = common.read_tile_window(ds, grid, tx, ty, nodata)
                if arr is None:
                    continue
                cloud = round(float((arr == nodata).sum()) / arr.size * 100.0, 2)
                blob = common.encode_geotiff(arr, grid, tx, ty, CRS, nodata, scale, offset)
                rows.append((band, date, tx, ty, CRS, RES, TILE_PX,
                             common.wgs84_bounds(grid, tx, ty, CRS), nodata, cloud, item.id,
                             blob, int((arr != nodata).sum())))
        return rows

    written = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for rows in ex.map(fetch_band, specs):
            for row in rows:
                written += 1 if writer.add(*row) else 0
    log(f"    {written} tiles")
    return written
