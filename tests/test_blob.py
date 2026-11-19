"""Offline tests for blob.decode: a tile comes out as rasterio reads it, the array bit for
bit and the profile key for key, whether it is the first tile of its kind (decoded with
GDAL) or a later one (decoded without it), and a GeoTIFF that is not laid out like a tile
is still read."""

import struct
import zlib

import numpy as np
import pytest
import rasterio
from rasterio.transform import Affine

from geovault import blob
from geovault.sources.common import encode_blob

UTM = "EPSG:32636"
SINU = "+proj=sinu +lon_0=0 +x_0=0 +y_0=0 +R=6371007.181 +units=m +no_defs"
INTS = ["uint8", "int8", "uint16", "int16", "uint32", "int32"]
FLOATS = ["float32", "float64"]


@pytest.fixture(autouse=True)
def no_kinds():
    blob._kinds.clear()
    yield
    blob._kinds.clear()


def gdal(data):
    with rasterio.MemoryFile(data) as mem, mem.open() as ds:
        return ds.read(1), ds.profile


def pixels(dtype, shape=(40, 30), seed=0):
    """Values over the whole range of the type; floats with NaN, infinities and -0.0."""
    rng = np.random.default_rng(seed)
    dtype = np.dtype(dtype)
    if dtype.kind == "f":
        arr = (rng.standard_normal(shape) * 10.0 ** rng.integers(-3, 6, shape)).astype(dtype)
        arr.flat[:4] = [np.nan, np.inf, -np.inf, -0.0]
        return arr
    info = np.iinfo(dtype)
    arr = rng.integers(info.min, info.max, shape, dtype=dtype, endpoint=True)
    arr.flat[:2] = [info.min, info.max]
    return arr


def tile(arr, x=500000.0, y=4200000.0, res=10.0, crs=UTM, nodata=0, **kw):
    return encode_blob(arr, crs, Affine(res, 0, x, 0, -res, y), nodata, **kw)


def same_profile(a, b):
    if type(a) is not type(b) or list(a) != list(b):
        return False
    for key in a:
        x, y = a[key], b[key]
        if type(x) is not type(y) or not (x == y or (x != x and y != y)):      # NaN nodata
            return False
    return True


def assert_as_gdal(got, data):
    arr, profile = got
    want, want_profile = gdal(data)
    assert arr.dtype == want.dtype and arr.shape == want.shape
    assert arr.tobytes() == want.tobytes()
    assert same_profile(profile, want_profile), (dict(profile), dict(want_profile))


def decode_without_gdal(monkeypatch, data):
    """Decode a tile of a kind that is known by now; GDAL must not be needed for it."""
    def refuse(_):
        raise AssertionError("the tile was read by GDAL")
    with monkeypatch.context() as m:
        m.setattr(blob, "_decode_gdal", refuse)
        return blob.decode(data)


@pytest.mark.parametrize("dtype, predictor",
                         [(t, 2) for t in INTS] + [(t, p) for t in FLOATS for p in (2, 3)])
def test_every_type_and_predictor(monkeypatch, dtype, predictor):
    first = tile(pixels(dtype, seed=1), predictor=predictor)
    later = tile(pixels(dtype, seed=2), x=500300.0, y=4199600.0, predictor=predictor)
    assert_as_gdal(blob.decode(first), first)
    assert list(blob._kinds.values()) != [None]
    assert_as_gdal(decode_without_gdal(monkeypatch, later), later)
    assert len(blob._kinds) == 1


@pytest.mark.parametrize("shape", [(1, 1), (50, 50), (37, 91), (240, 240), (256, 256), (500, 500)])
@pytest.mark.parametrize("dtype", ["uint16", "float32"])
def test_tile_sizes_and_strip_counts(monkeypatch, shape, dtype):
    """A tile is cut into strips of about 8 KB: one strip for a small tile, 125 for one
    of 500 x 500 float32."""
    first, later = tile(pixels(dtype, shape, 1)), tile(pixels(dtype, shape, 2), x=0.0, y=0.0)
    assert_as_gdal(blob.decode(first), first)
    assert_as_gdal(decode_without_gdal(monkeypatch, later), later)


@pytest.mark.parametrize("crs, x, y, res", [
    (UTM, 399960.0, 4100040.0, 20.0),
    ("EPSG:32736", 399960.0, 9100040.0, 60.0),
    ("EPSG:4326", 29.95, 39.95, 0.1),
    ("EPSG:4326", 34.125 - 1 / 7200, 37.675 + 1 / 7200, 1 / 3600),      # pixel centres on the lattice
    (SINU, 3335851.559, 4447802.078667, 463.3127165279165),
    ("EPSG:26911", 285500.0, 4405000.0, 1.0),
])
@pytest.mark.parametrize("dtype, nodata", [
    ("uint16", 0), ("uint8", None), ("int16", -32768), ("float32", -9999.0),
    ("float32", float("nan")), ("float32", 3.4028234663852886e+38),
])
def test_georeferencing_and_nodata(monkeypatch, crs, x, y, res, dtype, nodata):
    def make(seed, tx, ty):
        return tile(pixels(dtype, (24, 24), seed), x + tx * 24 * res, y - ty * 24 * res,
                    res, crs, nodata)
    first, later = make(1, 0, 0), make(2, 7, 3)
    assert_as_gdal(blob.decode(first), first)
    assert_as_gdal(decode_without_gdal(monkeypatch, later), later)


def test_tile_with_scale_and_offset(monkeypatch):
    """The scale and offset of a band are metadata of the GeoTIFF: neither the array nor
    the profile holds them, and tiles that differ in them are of one kind."""
    first = tile(pixels("uint16", seed=1), scale=2.75e-05, offset=-0.2)
    later = tile(pixels("uint16", seed=2), scale=0.00341802, offset=149.0)
    assert_as_gdal(blob.decode(first), first)
    assert_as_gdal(decode_without_gdal(monkeypatch, later), later)


def test_a_predictor_makes_a_kind(monkeypatch):
    """Float tiles of one band are stored with predictor 2 or 3, whichever is smaller."""
    two, three = (tile(pixels("float32", seed=s), predictor=p) for s, p in ((1, 2), (2, 3)))
    for data in (two, three):
        assert_as_gdal(blob.decode(data), data)
    assert len(blob._kinds) == 2 and None not in blob._kinds.values()
    for data in (two, three):
        assert_as_gdal(decode_without_gdal(monkeypatch, data), data)


def test_the_array_is_writable_and_the_profile_is_the_callers(monkeypatch):
    data = tile(pixels("int16"))
    blob.decode(data)
    arr, profile = decode_without_gdal(monkeypatch, data)
    arr[0, 0] = 7
    profile["nodata"] = 123
    assert_as_gdal(decode_without_gdal(monkeypatch, data), data)


def other_layout(then=None, **options):
    """A GeoTIFF of the test pixels that is not laid out as the encoder writes a tile.
    `then` is called with the dataset once its pixels are written."""
    arr = pixels("uint16", (64, 64))
    opts = dict(driver="GTiff", width=64, height=64, count=1, dtype="uint16", crs=UTM,
                transform=Affine(10, 0, 500000, 0, -10, 4200000), nodata=0)
    opts.update(options)
    with rasterio.MemoryFile() as mem:
        with mem.open(**opts) as dst:
            for band in range(1, opts["count"] + 1):
                dst.write(arr + band, band)
            if then:
                then(dst)
        return mem.read()


@pytest.mark.parametrize("options", [
    dict(compress="deflate", predictor=2, tiled=True, blockxsize=16, blockysize=16),
    dict(compress="lzw", predictor=2),
    dict(compress="zstd"),
    dict(),                                             # not compressed
    dict(compress="deflate", predictor=2, count=2),
    dict(compress="deflate", predictor=2, ENDIANNESS="BIG"),
    dict(compress="deflate", predictor=2, BIGTIFF="YES"),
    dict(compress="deflate", predictor=2, transform=Affine(10, 0, 500000, 0, 10, 4200000)),
    dict(compress="deflate", predictor=2, transform=Affine(10, 1, 500000, 1, -10, 4200000)),
], ids=["tiled", "lzw", "zstd", "plain", "two bands", "big endian", "bigtiff", "south up",
        "rotated"])
def test_other_layouts_are_read_by_gdal(options):
    data = other_layout(**options)
    for _ in range(2):
        assert_as_gdal(blob.decode(data), data)
    assert set(blob._kinds.values()) <= {None}


def test_deflate_strips_without_predictor(monkeypatch):
    data = other_layout(compress="deflate")
    assert_as_gdal(blob.decode(data), data)
    assert_as_gdal(decode_without_gdal(monkeypatch, data), data)


def test_a_tile_with_an_overview_is_read_by_gdal():
    data = other_layout(lambda dst: dst.build_overviews([2]), compress="deflate", predictor=2)
    with rasterio.MemoryFile(data) as mem, mem.open() as ds:
        assert ds.overviews(1) == [2]
    for _ in range(2):
        assert_as_gdal(blob.decode(data), data)
    assert set(blob._kinds.values()) <= {None}


@pytest.mark.parametrize("name", ["PIXELTYPE", "pixeltype"])
def test_a_tile_that_names_a_pixel_type_is_read_by_gdal(name):
    """PIXELTYPE in the metadata is how signed bytes were declared before GDAL had the
    type: what such a tile holds is for GDAL to say, whatever the case of the name."""
    xml = f'<GDALMetadata><Item name="{name}" domain="IMAGE_STRUCTURE">SIGNEDBYTE</Item></GDALMetadata>'
    data = with_field(tile(pixels("uint8")), blob.GDAL_METADATA, "B", list(xml.encode() + b"\x00"),
                      add=True)
    for _ in range(2):
        assert_as_gdal(blob.decode(data), data)
    assert not blob._kinds


def test_pixel_is_point_is_read_by_gdal():
    """Where GDAL places a raster whose pixels stand for points, half a pixel off its
    tiepoint or not, depends on a configuration option, so such a raster is never read
    here: not even when the option makes its first tile come out the same."""
    data = other_layout(lambda dst: dst.update_tags(AREA_OR_POINT="Point"),
                        compress="deflate", predictor=2)
    with rasterio.MemoryFile(data) as mem, mem.open() as ds:
        assert ds.tags()["AREA_OR_POINT"] == "Point"
    for ignore in (False, True, False):
        with rasterio.Env(GTIFF_POINT_GEO_IGNORE=ignore):
            assert_as_gdal(blob.decode(data), data)
        assert list(blob._kinds.values()) == [None]


@pytest.mark.parametrize("data", [b"", b"II*\x00", b"II*\x00\x08\x00\x00\x00\xff\xff",
                                  b"not a tile", tile(pixels("uint8"))[:120]],
                         ids=["empty", "header only", "entries cut off", "text", "cut tile"])
def test_bytes_that_are_no_geotiff_raise_what_gdal_raises(data):
    with pytest.raises(Exception) as want:
        gdal(data)
    with pytest.raises(want.type):
        blob.decode(data)


# The tiles the tests below change: 256 x 256 uint16, so 16 strips of 16 rows of 512 bytes.
DAMAGED, ROW, STRIP_ROWS = (256, 256), 512, 16


def strip_table(data):
    """(offsets, sizes) of the strips of a tile."""
    fields = blob._fields(data)
    return (list(blob._numbers(fields, blob.STRIP_OFFSETS, blob.INTEGERS)),
            list(blob._numbers(fields, blob.STRIP_SIZES, blob.INTEGERS)))


def with_field(data, tag, code, values, add=False):
    """A tile with one field replaced or, with `add`, with a second entry of the tag:
    the values and a new directory of the fields are put at the end of the blob."""
    raw = struct.pack(f"<{len(values)}{code}", *values)
    data += bytes(len(data) % 2)
    if len(raw) > 4:
        entry, data = struct.pack("<I", len(data)), data + raw + bytes(len(raw) % 2)
    else:
        entry = raw.ljust(4, b"\x00")
    entry = (tag, {"B": 2, "H": 3, "I": 4, "d": 12}[code], len(values), entry)
    (at,) = struct.unpack_from("<I", data, 4)
    (n,) = struct.unpack_from("<H", data, at)
    entries = [struct.unpack_from("<HHI4s", data, at + 2 + 12 * k) for k in range(n)]
    entries = entries + [entry] if add else [entry if e[0] == tag else e for e in entries]
    entries.sort(key=lambda e: e[0])
    directory = struct.pack("<H", len(entries)) + b"".join(
        struct.pack("<HHI4s", *e) for e in entries) + bytes(4)
    return data[:4] + struct.pack("<I", len(data)) + data[8:] + directory


def with_strips(data, strips):
    """A tile whose strip table names these compressed strips instead of its own."""
    offsets = []
    for strip in strips:
        offsets.append(len(data))
        data += strip
    data = with_field(data, blob.STRIP_OFFSETS, "I", offsets)
    return with_field(data, blob.STRIP_SIZES, "I", [len(s) for s in strips])


def rows_of(data):
    """The bytes of the rows of a tile, as its strips hold them."""
    return b"".join(zlib.decompress(data[at:at + n]) for at, n in zip(*strip_table(data)))


def one_strip_with_every_row(data):
    return with_strips(data, [zlib.compress(rows_of(data))])


def first_strip_a_row_too_long(data):
    raw, strip = rows_of(data), STRIP_ROWS * ROW
    parts = [raw[:strip + ROW], raw[strip + ROW:2 * strip]] + [
        raw[k * strip:(k + 1) * strip] for k in range(2, len(raw) // strip)]
    return with_strips(data, [zlib.compress(p) for p in parts])


def last_strip_past_the_end(data):
    _, sizes = strip_table(data)
    return with_field(data, blob.STRIP_SIZES, "I", sizes[:-1] + [sizes[-1] + 10_000])


def first_strip_damaged(data):
    at = strip_table(data)[0][0]
    return data[:at + 4] + bytes(12) + data[at + 16:]


def strips_in_another_order(data):
    offsets, sizes = strip_table(data)
    data = with_field(data, blob.STRIP_OFFSETS, "I", offsets[::-1])
    return with_field(data, blob.STRIP_SIZES, "I", sizes[::-1])


def second_tiepoint(data):
    return with_field(data, blob.TIEPOINT, "d", (0, 0, 0, 123.0, 456.0, 0), add=True)


def second_strip_table(data):
    return with_field(data, blob.STRIP_OFFSETS, "I", strip_table(data)[0][::-1], add=True)


def offsets_as_doubles(data):
    return with_field(data, blob.STRIP_OFFSETS, "d", strip_table(data)[0])


def sizes_as_doubles(data):
    return with_field(data, blob.STRIP_SIZES, "d", strip_table(data)[1])


def a_strip_that_inflates_to_far_more(data):
    return with_strips(data, [zlib.compress(bytes(50_000_000))] + [b""] * (len(strip_table(data)[0]) - 1))


GDAL_SAYS = (np.full(DAMAGED, 7, "uint16"), {"driver": "GTiff"})


@pytest.mark.parametrize("damage", [
    one_strip_with_every_row, first_strip_a_row_too_long, last_strip_past_the_end,
    first_strip_damaged, second_tiepoint, second_strip_table, offsets_as_doubles,
    sizes_as_doubles, a_strip_that_inflates_to_far_more])
def test_a_damaged_tile_is_handed_to_gdal(monkeypatch, damage):
    """Tiles whose bytes were changed after they were written, each of a kind that is
    read here. None of them is read here: GDAL is asked, for the first tile of a kind
    and for a later one, and what it answers (an error, or what it can read of the
    tile) is what comes back. GDAL itself is not run on them in this test: what it
    makes of such bytes differs between its builds (one raises where another returns
    what was in its buffer)."""
    good = tile(pixels("uint16", DAMAGED, seed=1))
    bad = damage(tile(pixels("uint16", DAMAGED, seed=2), x=0.0, y=0.0))
    assert len(strip_table(good)[0]) == DAMAGED[0] // STRIP_ROWS
    for known in (False, True):
        blob._kinds.clear()
        if known:
            blob.decode(good)
            assert None not in blob._kinds.values()
        asked = []
        with monkeypatch.context() as m:
            m.setattr(blob, "_decode_gdal", lambda data: asked.append(data) or GDAL_SAYS)
            arr, profile = blob.decode(bad)
        assert arr is GDAL_SAYS[0] and profile is GDAL_SAYS[1] and asked == [bad]
        if not known:                       # a damaged first tile teaches no kind
            assert set(blob._kinds.values()) <= {None}

    def refuse(data):
        raise rasterio.errors.RasterioIOError("what GDAL says of the tile")

    monkeypatch.setattr(blob, "_decode_gdal", refuse)
    with pytest.raises(rasterio.errors.RasterioIOError, match="what GDAL says"):
        blob.decode(bad)


def test_strips_stored_in_another_order_are_read_here(monkeypatch):
    """Where the strips lie in the blob is for the strip table to say."""
    good = tile(pixels("uint16", DAMAGED, seed=1))
    other = strips_in_another_order(tile(pixels("uint16", DAMAGED, seed=2), x=0.0, y=0.0))
    assert strip_table(other)[0] != sorted(strip_table(other)[0])
    assert_as_gdal(blob.decode(good), good)
    assert_as_gdal(decode_without_gdal(monkeypatch, other), other)


def test_a_strip_is_not_inflated_past_the_size_of_its_rows():
    """A strip of 50 KB that inflates to 50 MB is given up after the 8 KB its rows hold."""
    import tracemalloc
    data = a_strip_that_inflates_to_far_more(tile(pixels("uint16", DAMAGED)))
    fields = blob._fields(data)
    layout = blob._layout(fields)
    tracemalloc.start()
    with pytest.raises(blob._NotPlain):
        blob._pixels(data, fields, layout)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < 1_000_000


def test_a_kind_that_does_not_come_out_as_gdal_reads_it_is_left_to_gdal(monkeypatch):
    first, later = tile(pixels("int16", seed=1)), tile(pixels("int16", seed=2), x=0.0)
    with monkeypatch.context() as m:
        real = blob._pixels
        m.setattr(blob, "_pixels", lambda *a: real(*a) + 1)
        assert_as_gdal(blob.decode(first), first)
    assert list(blob._kinds.values()) == [None]
    assert_as_gdal(blob.decode(later), later)
    with pytest.raises(AssertionError, match="read by GDAL"):
        decode_without_gdal(monkeypatch, later)


def test_the_number_of_kinds_is_capped(monkeypatch):
    monkeypatch.setattr(blob, "MAX_KINDS", 2)
    tiles = [tile(pixels("uint8", (n, n))) for n in (8, 9, 10, 11, 12)]
    for data in tiles + tiles:
        assert_as_gdal(blob.decode(data), data)
        assert len(blob._kinds) <= 2


def test_accepts_what_a_query_returns():
    data = tile(pixels("float32"))
    for given in (bytearray(data), memoryview(data)):
        blob.decode(given)
        assert_as_gdal(blob.decode(given), data)
