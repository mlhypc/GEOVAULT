"""Offline tests for the PARDIG adapters: lattice, store orientation, chunk assembly, time
axes, search and an ingest end to end against a fake bucket (no network)."""

import json

import duckdb
import numpy as np
import pytest

from geovault import store
from geovault.sources import pardig, pardig_lr, pardig_nrt
from geovault.store import Writer


def value(t, row, col):
    """Synthetic store value at (time index, store row, store col), exact in float32."""
    return np.float32(((t * 7 + row * 13 + col * 3) % 1000) / 1000.0)


def store_field(t):
    rows = np.arange(pardig.NROW)[:, None]
    cols = np.arange(pardig.NCOL)[None, :]
    return (((t * 7 + rows * 13 + cols * 3) % 1000) / 1000.0).astype(np.float32)


# ---------------------------------------------------------------- lattice

def test_turkey_is_eight_tiles():
    tiles = list(pardig.grid_for().tiles_for_bounds(25.6, 35.8, 44.9, 42.2))
    assert len(tiles) == 8


def test_tile_window_points_at_the_south_west_cell():
    g = pardig.grid_for()
    tx, ty = g.tile_of_point(27.43, 38.61)
    assert g.tile_bounds(tx, ty) == pytest.approx((24.95, 34.95, 29.95, 39.95))
    assert pardig.tile_window(tx, ty) == (1250, 250)      # lat 35.0 -> row 1250, lon 25.0 -> col 250


def test_tile_window_wraps_west_of_greenwich():
    g = pardig.grid_for()
    tx, ty = g.tile_of_point(-2.0, 10.0)
    assert g.tile_bounds(tx, ty)[0] == pytest.approx(-5.05)
    assert pardig.tile_window(tx, ty)[1] == 3550           # lon -5.0 -> 355.0


def test_every_tile_is_whole_chunks_and_never_crosses_the_seam():
    g = pardig.grid_for()
    for tx, ty in g.tiles_for_bounds(-180, -90, 180, 90):
        row0, col0 = pardig.tile_window(tx, ty)
        assert row0 % 5 == 0 and col0 % 5 == 0
        assert 0 <= col0 and col0 + pardig.TILE_PX <= pardig.NCOL


# ---------------------------------------------------------------- orientation

def test_cut_tile_is_north_up():
    f = store_field(0)
    g = pardig.grid_for()
    tx, ty = g.tile_of_point(27.43, 38.61)
    row0, col0 = pardig.tile_window(tx, ty)
    a = pardig.cut_tile(f, tx, ty)
    assert a[0, 0] == f[row0 + 49, col0]                  # first row = northernmost cell
    assert a[-1, -1] == f[row0, col0 + 49]


def test_cut_tile_beyond_the_pole_is_nodata():
    g = pardig.grid_for()
    tx, ty = g.tile_of_point(10.0, 92.0)                  # tile 89.95..94.95: one valid row (lat 90)
    a = pardig.cut_tile(store_field(0), tx, ty)
    assert (a[-1] != pardig.NODATA).all() and (a[:-1] == pardig.NODATA).all()


def test_nan_becomes_nodata():
    block = np.zeros((50, 50), np.float32)
    block[0, 0] = np.nan
    a = pardig.tile_from_store_block(block)
    assert a[-1, 0] == pardig.NODATA and a.dtype == np.float32


# ---------------------------------------------------------------- series chunks

def fake_chunk(chunks, ntime):
    """get_chunk for a synthetic series array; edge chunks padded to full size like Zarr."""
    ct, cr, cc = chunks

    def get(idx):
        tc, i, j = idx
        t = np.arange(tc * ct, (tc + 1) * ct)[:, None, None]
        r = np.arange(i * cr, (i + 1) * cr)[None, :, None]
        c = np.arange(j * cc, (j + 1) * cc)[None, None, :]
        a = (((t * 7 + r * 13 + c * 3) % 1000) / 1000.0).astype(np.float32)
        a[np.broadcast_to(t >= ntime, a.shape)] = np.nan
        a[np.broadcast_to(r >= pardig.NROW, a.shape)] = np.nan
        return a
    return get


@pytest.mark.parametrize("lonlat", [(27.43, 38.61), (10.0, 92.0), (-2.0, -88.0)])
def test_archive_block_matches_whole_fields(lonlat):
    g = pardig.grid_for()
    tx, ty = g.tile_of_point(*lonlat)
    row0, col0 = pardig.tile_window(tx, ty)
    chunks, ntime = (3, 5, 5), 8
    block = pardig.read_archive_block(fake_chunk(chunks, ntime), chunks, row0, col0, 2, 7)
    for k, t in enumerate(range(2, 7)):
        assert np.array_equal(pardig.tile_from_store_block(block[k]), pardig.cut_tile(store_field(t), tx, ty))


def test_tile_chunks_cover_time_and_space():
    idx = pardig.tile_chunks((3, 5, 5), 1250, 250, 2, 7)
    assert {i[0] for i in idx} == {0, 1, 2}
    assert len(idx) == 3 * 10 * 10


# ---------------------------------------------------------------- metadata and time

def meta(codecs=None, dtype="float32", shape=(10, 1801, 3600), chunks=(10, 5, 5)):
    return {"shape": list(shape), "data_type": dtype,
            "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": list(chunks)}},
            "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
            "codecs": codecs or [{"name": "bytes", "configuration": {"endian": "little"}},
                                 {"name": "zstd", "configuration": {"level": 0, "checksum": False}}]}


def test_array_meta_accepts_the_published_encoding():
    m = pardig.check_array_meta(meta(), "x")
    assert m["chunks"] == (10, 5, 5) and m["sep"] == "/"
    assert pardig.chunk_key("a/precipitation", (0, 258, 70)) == "a/precipitation/c/0/258/70"


@pytest.mark.parametrize("bad", [
    {"codecs": [{"name": "bytes"}, {"name": "blosc"}]},
    {"dtype": "float64"},
])
def test_array_meta_rejects_other_encodings(bad):
    with pytest.raises(RuntimeError):
        pardig.check_array_meta(meta(**bad), "x")


def test_time_units_and_dates():
    unit, epoch = pardig.parse_time_units("minutes since 1999-01-01 00:00:00")
    t = epoch + np.array([0, 15, 1440 * 366]).astype(f"timedelta64[{unit}]")
    assert [pardig.step_date(x) for x in t] == ["1999-01-01T00:00", "1999-01-01T00:15", "2000-01-02T00:00"]
    assert pardig.step_date(pardig.folder_time("20261116T043000")) == "2026-11-16T04:30"
    with pytest.raises(RuntimeError):
        pardig.parse_time_units("fortnights since 1999-01-01")


# ---------------------------------------------------------------- search

def archive(name="2016_to_2025", start="2025-12-31T23:00", steps=4, product="NRT"):
    times = np.datetime64(start) + np.arange(steps) * np.timedelta64(15, "m")
    m = pardig.check_array_meta(meta(shape=(steps, 1801, 3600), chunks=(steps, 5, 5)), "x")
    return pardig.Archive(product, name, times.astype("datetime64[m]"),
                          f"pardig_1/{product}/zarr_rechunked_as_timeseries/{name}/predictions.zarr/precipitation",
                          m, b"meta-v1")


def live(steps=("20251231T234500", "20260101T000000", "20260101T001500", "20260101T003000",
                 "20260102T000000")):
    return pardig.Live("NRT", "2026_to_present", list(steps), "pardig_1/NRT/zarr/2026_to_present")


def test_search_splits_years_and_skips_live_days_the_archive_holds(monkeypatch):
    monkeypatch.setattr(pardig, "segments", lambda product: [archive(), live()])
    items = pardig_nrt.search(None, "2025-12-31", "2026-01-02")
    assert [i.id for i in items] == ["pardig_nrt-2016_to_2025-2025",
                                     "pardig_nrt-2026_to_present-2026-01-01",
                                     "pardig_nrt-2026_to_present-2026-01-02"]
    assert items[0].properties["start_datetime"] == "2025-12-31T23:00:00Z"
    # the archive holds 2025-12-31 23:45, so the live folder of that step is not used
    assert all(not s.startswith("20251231") for i in items[1:] for s in i.steps)
    assert items[1].steps == ["20260101T000000", "20260101T001500", "20260101T003000"]


def test_search_clamps_to_the_requested_range(monkeypatch):
    monkeypatch.setattr(pardig, "segments", lambda product: [archive(steps=4 * 24 * 3)])
    items = pardig_lr.search(None, "2026-01-01", "2026-01-01")
    assert len(items) == 1
    assert items[0].properties["start_datetime"] == "2026-01-01T00:00:00Z"
    assert items[0].properties["end_datetime"] == "2026-01-01T23:45:00Z"


# ---------------------------------------------------------------- ingest end to end

class FakeFS:
    """Serves Zarr chunk objects of the synthetic field and live `success` markers."""

    def __init__(self, seg_archive, seg_live, ntime):
        self.a, self.l, self.ntime = seg_archive, seg_live, ntime
        self.cats = []

    def cat(self, key):
        from numcodecs import Zstd
        self.cats.append(key)
        z = Zstd()
        if key.startswith(self.a.array_path):
            tc, i, j = (int(v) for v in key.rsplit("/c/", 1)[1].split("/"))
            return z.encode(fake_chunk(self.a.meta["chunks"], self.ntime)((tc, i, j)).tobytes())
        if key.endswith("zarr.json"):
            return json.dumps(meta(shape=(1801, 3600), chunks=(1801, 3600))).encode()
        step = key.split("/")[-6]                         # <step>/predictions.zarr/precipitation/c/0/0
        t = int(step[-6:-2]) // 15 + 100                   # synthetic time index of a live step
        return z.encode(store_field(t).tobytes())

    def find(self, root, prefix=""):
        return [f"{root}/{s}/success" for s in self.l.steps if s.startswith(prefix) and s != "20260101T001500"]


@pytest.fixture
def fake(tmp_path, monkeypatch):
    pytest.importorskip("numcodecs", reason="needs the [pardig] extra")
    monkeypatch.setattr(store, "STORE", tmp_path / "store")
    monkeypatch.setattr(store, "ROOT", tmp_path)
    a, l = archive(), live()
    f = FakeFS(a, l, len(a.times))
    monkeypatch.setattr(pardig, "fs", lambda: f)
    monkeypatch.setattr(pardig, "segments", lambda product: [a, l])
    monkeypatch.setattr(pardig, "read_coordinates",
                        lambda base: (np.linspace(-90, 90, pardig.NROW), np.arange(pardig.NCOL) * pardig.RES))
    pardig._LIVE_META.clear()
    return f


def stored(tmp_path):
    con = duckdb.connect()
    rows = con.execute(
        f"SELECT date, x, y, scene_id, data FROM read_parquet('{(tmp_path / 'store').as_posix()}/**/*.parquet') "
        f"ORDER BY date").fetchall()
    con.close()
    return rows


def decode(blob):
    import io
    import rasterio
    with rasterio.MemoryFile(io.BytesIO(bytes(blob))) as mem:
        with mem.open() as ds:
            return ds.read(1)


def test_ingest_archive_and_live_end_to_end(fake, tmp_path):
    bbox = (27.3, 38.4, 28.3, 38.9)                       # one tile
    items = pardig_nrt.search(bbox, "2025-12-31", "2026-01-01")
    w = Writer("pardig_nrt")
    n = sum(pardig_nrt.ingest_scene(i, bbox, w, log=lambda m: None) for i in items)
    w.close()
    rows = stored(tmp_path)
    # 4 archive steps on 2025-12-31 (23:00..23:45) + 2 live steps (00:15 has no success marker)
    assert n == len(rows) == 6
    assert [r[0] for r in rows] == ["2025-12-31T23:00", "2025-12-31T23:15", "2025-12-31T23:30",
                                    "2025-12-31T23:45", "2026-01-01T00:00", "2026-01-01T00:30"]
    assert {r[3] for r in rows} == {"NRT/2016_to_2025", "NRT/2026_to_present"}
    tx, ty = rows[0][1], rows[0][2]
    for (d, _, _, _, blob), t in zip(rows, [0, 1, 2, 3, 100, 102]):
        assert np.array_equal(decode(blob), pardig.cut_tile(store_field(t), tx, ty)), d


def test_ingest_skips_what_the_store_holds_and_reuses_the_cache(fake, tmp_path):
    bbox = (27.3, 38.4, 28.3, 38.9)
    item = pardig_nrt.search(bbox, "2025-12-31", "2025-12-31")[0]
    w = Writer("pardig_nrt")
    pardig_nrt.ingest_scene(item, bbox, w, log=lambda m: None)
    w.close()
    skip = w.existing_keys(pardig.RES, ["2025-12"])
    assert pardig_nrt.plan_scene(item, bbox, skip_keys=skip) == {}
    fake.cats.clear()
    w2 = Writer("pardig_nrt")
    assert pardig_nrt.ingest_scene(item, bbox, w2, skip_keys=set(), log=lambda m: None) == 4
    assert fake.cats == []                                 # every chunk came from the disk cache


def test_cache_is_emptied_when_the_remote_array_changes(fake, tmp_path):
    a = archive()
    d = pardig._ensure_cache("pardig_nrt", a)
    (d / "0_0_0").write_bytes(b"old")
    a.meta_raw = b"meta-v2"
    pardig._ensure_cache("pardig_nrt", a)
    assert not (d / "0_0_0").exists() and (d / "zarr.json").read_bytes() == b"meta-v2"


def test_other_bands_plan_nothing(fake):
    item = pardig_nrt.search(None, "2025-12-31", "2025-12-31")[0]
    assert pardig_nrt.plan_scene(item, (27.3, 38.4, 28.3, 38.9), bands=["t2m"]) == {}


def test_reader_series_reads_sub_hourly_rows(fake, tmp_path, monkeypatch):
    from geovault import catalog, reader
    monkeypatch.setattr(catalog, "STORE", tmp_path / "store")
    monkeypatch.setattr(catalog, "CATALOG", tmp_path / "catalog")
    bbox = (27.3, 38.4, 28.3, 38.9)
    w = Writer("pardig_nrt")
    for item in pardig_nrt.search(bbox, "2025-12-31", "2026-01-01"):
        pardig_nrt.ingest_scene(item, bbox, w, log=lambda m: None)
    w.close()
    catalog.rebuild("pardig_nrt")
    # one point in each half of a 0.1 deg cell: a half-cell lattice shift moves one of them
    for lon, lat in ((27.43, 38.61), (27.47, 38.67)):
        row = int(round((lat + 90) / pardig.RES))
        col = int(round(lon / pardig.RES))
        s = reader.series("pardig_nrt", lon, lat, "precipitation")
        assert [x["date"] for x in s] == ["2025-12-31T23:00", "2025-12-31T23:15", "2025-12-31T23:30",
                                          "2025-12-31T23:45", "2026-01-01T00:00", "2026-01-01T00:30"]
        assert [x["value"] for x in s] == [float(value(t, row, col)) for t in (0, 1, 2, 3, 100, 102)]
    assert len(reader.series("pardig_nrt", lon, lat, "precipitation", date="2025-12-31T23")) == 4


def test_pinned_predictor_is_written_and_lossless():
    import rasterio
    from rasterio.transform import Affine
    from geovault.sources import common
    y, x = np.mgrid[0:50, 0:50]
    a = (280.0 + np.sin(x / 7.0) * 3 + np.cos(y / 5.0) * 2).astype(np.float32)
    t = Affine(0.1, 0, 25.0, 0, -0.1, 40.0)
    for pred in (2, 3):
        blob = common.encode_blob(a, "EPSG:4326", t, -9999.0, predictor=pred)
        with rasterio.MemoryFile(blob) as mem, mem.open() as ds:
            assert ds.tags(ns="IMAGE_STRUCTURE").get("PREDICTOR") == str(pred)
            assert np.array_equal(ds.read(1), a)


def test_a_missing_chunk_is_recorded_and_an_empty_cache_file_is_fetched_again(fake, tmp_path):
    a = archive()
    cache = pardig._ensure_cache("pardig_nrt", a)
    real_cat = fake.cat

    def cat(key):
        if key.endswith("/c/0/250/50"):
            raise FileNotFoundError(key)
        return real_cat(key)
    fake.cat = cat
    assert pardig._cached_chunk(a, (0, 250, 50), cache) is None
    assert (cache / "0_250_50.missing").exists()
    fake.cats.clear()
    assert pardig._cached_chunk(a, (0, 250, 50), cache) is None and fake.cats == []
    (cache / "0_250_51").write_bytes(b"")                 # what an interrupted write can leave
    p = pardig._cached_chunk(a, (0, 250, 51), cache)
    assert p is not None and p.stat().st_size > 0


def test_the_antimeridian_column_is_planned_once(fake):
    item = pardig_nrt.search(None, "2025-12-31", "2025-12-31")[0]
    plan = pardig._plan(item, (-180, -5, 180, 5), None, set(), None)
    assert sorted({tx for tx, _ in plan}) == list(range(pardig.TX_MIN, pardig.TX_MAX + 1))
    cols = [pardig.tile_window(tx, 0)[1] for tx in range(pardig.TX_MIN, pardig.TX_MAX + 1)]
    assert sorted(cols) == list(range(0, pardig.NCOL, pardig.TILE_PX))


def test_coordinates_are_checked():
    lat, lon = np.linspace(-90, 90, pardig.NROW), np.arange(pardig.NCOL) * pardig.RES
    pardig.check_coordinates(lat, lon, "x")
    with pytest.raises(RuntimeError):
        pardig.check_coordinates(lat[::-1], lon, "x")
