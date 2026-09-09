"""Landsat Collection 2 Level-2 via Microsoft Planetary Computer. Free, no account.

STAC: https://planetarycomputer.microsoft.com/api/stac/v1, collection
landsat-c2-l2: surface reflectance (L2SP) plus surface temperature for Landsat
4-5 TM, 7 ETM+ and 8-9 OLI/TIRS, 1982 to today. 30 m, UTM, uint16 digital
numbers. Only Tier 1 scenes are taken (T2 has degraded geolocation).

Why Landsat next to Sentinel-2: the thermal band (no thermal on S2) and the
archive back to the 1980s (S2 starts 2015-2017). Not an S2 replacement: 30 m and
a 16-day cycle per satellite (8 days with L8+L9 together).

Band names are the STAC common names, so one adapter reads all generations:
    coastal (L8/9 only), blue, green, red, nir08, swir16, swir22   surface reflectance
    lwir                                                          surface temperature
    qa_pixel                                                      CFMask bit flags
`lwir` is the asset `lwir11` on Landsat 8/9 (TIRS band 10) and `lwir` on
Landsat 4-7 (TM/ETM+ band 6).

Pixels are stored RAW (lossless uint16). The physical value is
    reflectance = DN * 2.75e-5 - 0.2        (SR bands)
    kelvin      = DN * 0.00341802 + 149.0   (lwir)
and both factors are written into each GeoTIFF blob as band scale/offset
(rasterio: ds.scales[0], ds.offsets[0]). DN 0 is nodata for SR and lwir.
qa_pixel has no scale; its fill value is 1 (bit 0). Cloud share per tile is
computed locally from qa_pixel bits 1-4 (dilated cloud, cirrus, cloud, shadow),
the same role SCL plays for Sentinel-2.

Grid: Landsat C2 pixel EDGES sit 15 m off the UTM origin on both axes (scene
origins end in ...85 / ...15), identically for every scene of every generation.
The tile lattice is therefore anchored at (15, 15), not at the CRS origin as for
the Sentinel grids, and not per scene as Grid.from_transform would do (a
per-scene anchor modulo the tile size differs between scenes and would break the
dedup key). 256 px tiles = 7.68 km.

Asset hrefs must be signed (planetary_computer.sign); anonymous signing works,
a free API key only raises rate limits. Planetary Computer publishes a Landsat
scene about a week after acquisition (USGS L2 processing plus their ingest);
Sentinel-2 on earth-search is same-day.
"""

import io
import os
from concurrent.futures import ThreadPoolExecutor

import planetary_computer as pc
import rasterio
from pystac_client import Client

from ..grid import Grid
from . import common

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "landsat-c2-l2"
COLLECTION_CATEGORY = "T1"

# band name -> candidate asset keys (first present wins)
BANDS = {
    "coastal": ("coastal",),
    "blue": ("blue",),
    "green": ("green",),
    "red": ("red",),
    "nir08": ("nir08",),
    "swir16": ("swir16",),
    "swir22": ("swir22",),
    "lwir": ("lwir11", "lwir"),
    "qa_pixel": ("qa_pixel",),
}
DEFAULT_BANDS = list(BANDS)
QA_BAND = "qa_pixel"
RES = 30
TILE_PX = 256
ANCHOR = 15.0                 # pixel edges at 30k + 15 in both axes, all generations
NODATA = 0                    # SR and lwir
NODATA_QA = 1                 # qa_pixel fill (bit 0 set, nothing else)
SR_SCALE, SR_OFFSET = 2.75e-05, -0.2
ST_SCALE, ST_OFFSET = 0.00341802, 149.0
QA_CLOUD_MASK = (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4)   # dilated cloud, cirrus, cloud, shadow
WORKERS = 6                   # band parallelism inside a scene

_ENV = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_MULTIPLEX="YES",
            VSI_CACHE="TRUE")
for _k, _v in _ENV.items():
    os.environ.setdefault(_k, _v)   # visible to every thread (rasterio.Env is thread-local)


def grid_for(crs) -> Grid:
    return Grid(crs, RES, TILE_PX, ANCHOR, ANCHOR)


def _asset_key(item, band):
    for key in BANDS[band]:
        if key in item.assets:
            return key
    return None


def _scale_offset(item, band, key):
    """(scale, offset) for a band: from the asset's raster:bands when present, else the
    Collection 2 constants. qa_pixel has none."""
    if band == QA_BAND:
        return None, None
    rb = item.assets[key].extra_fields.get("raster:bands") or [{}]
    default = (ST_SCALE, ST_OFFSET) if band == "lwir" else (SR_SCALE, SR_OFFSET)
    return rb[0].get("scale", default[0]), rb[0].get("offset", default[1])


def cloud_pct_from_qa(qa):
    """Unusable-pixel share (0-100) from a qa_pixel array; None if all fill."""
    valid = (qa & 1) == 0
    if not valid.any():
        return None
    cloudy = ((qa & QA_CLOUD_MASK) != 0) & valid
    return round(float(cloudy.sum()) / float(valid.sum()) * 100.0, 2)


def search(bbox, start, end, cloud_max=None, limit=None):
    """STAC search -> Tier 1 scenes of every Landsat generation, oldest first."""
    client = Client.open(STAC_URL, modifier=pc.sign_inplace)
    q = {"landsat:collection_category": {"eq": COLLECTION_CATEGORY}}
    if cloud_max is not None:
        q["eo:cloud_cover"] = {"lte": cloud_max}
    items = list(client.search(
        collections=[COLLECTION], bbox=list(bbox), datetime=f"{start}/{end}", query=q,
    ).items())
    items.sort(key=lambda i: i.properties["datetime"])
    return items[:limit] if limit else items


def plan_scene(item, bbox, bands=None, skip_keys=None, keep_offzone=False, aoi=None) -> dict:
    """band -> number of tiles this scene would still fetch (nothing is read).
    Edge tiles outside the scene footprint are counted too, so a small residue
    on scene edges is normal after a full ingest."""
    from ..aoi import tile_filter
    bands = bands or DEFAULT_BANDS
    skip_keys = skip_keys or set()
    date, crs = common.scene_date_crs(item)
    grid = grid_for(crs)
    in_aoi = tile_filter(aoi)
    out = {}
    for band in bands:
        if _asset_key(item, band) is None:
            continue
        n = len(common.plan_tiles(grid, bbox, crs, band, date, skip_keys, keep_offzone, in_aoi))
        if n:
            out[band] = n
    return out


def ingest_scene(item, bbox, writer, bands=None, skip_keys=None, log=print,
                 keep_offzone=False, aoi=None) -> int:
    """Fetch the requested bands of one scene over bbox/aoi; returns tiles written.

    Two waves like Sentinel-2: qa_pixel first (the cloud_pct source), then the
    other bands in parallel. All Landsat bands share one 30 m grid, so a band
    tile's cloud_pct is simply the qa tile with the same (x, y)."""
    bands = bands or DEFAULT_BANDS
    skip_keys = skip_keys or set()
    date, crs = common.scene_date_crs(item)
    grid = grid_for(crs)
    scene = item.id
    qa = {}

    from ..aoi import tile_filter
    in_aoi = tile_filter(aoi)

    def fetch_band(band):
        """One band -> list of prepared rows (thread-safe: no writer access)."""
        key = _asset_key(item, band)
        if key is None:
            return []
        todo = common.plan_tiles(grid, bbox, crs, band, date, skip_keys, keep_offzone, in_aoi)
        if not todo:
            return []
        nodata = NODATA_QA if band == QA_BAND else NODATA
        scale, offset = _scale_offset(item, band, key)
        rows = []
        with rasterio.open(item.assets[key].href) as ds:
            for tx, ty in todo:
                arr = common.read_tile_window(ds, grid, tx, ty, nodata)
                if arr is None:
                    continue
                if band == QA_BAND:
                    qa[(tx, ty)] = arr
                    cloud = cloud_pct_from_qa(arr)
                else:
                    q = qa.get((tx, ty))
                    cloud = cloud_pct_from_qa(q) if q is not None else None
                rows.append((band, date, tx, ty, crs, RES, TILE_PX,
                             common.wgs84_bounds(grid, tx, ty, crs), nodata, cloud, scene,
                             common.encode_geotiff(arr, grid, tx, ty, crs, nodata, scale, offset),
                             int((arr != nodata).sum())))   # valid_px: fuller scene wins a same-day key
        return rows

    written = 0
    with rasterio.Env(**_ENV):
        if QA_BAND in bands:
            for row in fetch_band(QA_BAND):
                written += 1 if writer.add(*row) else 0
        rest = [b for b in bands if b != QA_BAND]
        qa_skipped = any(k[0] == QA_BAND and k[1] == date for k in skip_keys)
        if rest and (not qa or qa_skipped):
            _load_qa_from_store(writer.dataset, date, crs, qa)
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for rows in ex.map(fetch_band, rest):
                for row in rows:
                    written += 1 if writer.add(*row) else 0
    log(f"    {written} tiles")
    return written


def _load_qa_from_store(dataset, date, crs, qa):
    """Fill the qa tile dict from already-stored qa_pixel blobs for this date/crs, so a
    re-run over a partially cached scene still writes real cloud_pct values."""
    import duckdb
    from ..store import month_files
    files = month_files(dataset, RES, date)
    if not files:
        return
    lst = ", ".join(f"'{f.as_posix()}'" for f in files)
    con = duckdb.connect()
    rows = con.execute(
        f"SELECT x, y, data FROM read_parquet([{lst}], union_by_name=true) "
        f"WHERE band=? AND date=? AND crs=?", [QA_BAND, date, crs]).fetchall()
    con.close()
    for x, y, blob in rows:
        if (x, y) in qa:
            continue    # freshly fetched this run wins over the stored copy
        with rasterio.MemoryFile(io.BytesIO(bytes(blob))) as mem:
            with mem.open() as ds:
                qa[(x, y)] = ds.read(1)
