"""Decoding of tile blobs.

A stored tile is a small single-band DEFLATE GeoTIFF (sources.common.encode_blob).
Opening one through GDAL costs 0.5 to 2 ms, and on a small tile nearly all of that is
spent on the dataset, not on the pixels: 0.54 ms for a 50 x 50 float32 tile whose
pixels take 0.03 ms. A series at a point opens one tile per date, so on a layer with
many small tiles that overhead is most of the read.

`decode` reads the layout the encoder writes straight from the bytes: the strips are
inflated with zlib and the predictor is undone with numpy. What it returns is what
rasterio returns for the same blob, the array bit for bit and the profile key for key:

- The profile of a kind of tile (its size, data type, nodata value, CRS, strip layout)
  is not rebuilt here. The first tile of each kind is opened with GDAL as well, and the
  profile GDAL gives is kept for the tiles of that kind that follow; only the transform
  is taken from each tile, from its tiepoint.
- That first tile is also decoded both ways. A kind whose pixels or transform differ
  from what GDAL reads is left to GDAL from then on.
- A blob that is laid out in any other way (tiles instead of strips, another
  compression, several bands, a tag the encoder does not write) is read by GDAL, so
  every GeoTIFF that could be read before still is. So is a tile whose strips are not
  what its size calls for: GDAL reports what is wrong with it.

The profile kept for a kind is the one GDAL gave when the first tile of the kind was
read. A process that changes a GDAL configuration option which alters how a GeoTIFF is
read (GTIFF_SRS_SOURCE, for one) while it reads tiles has to empty `_kinds` with it.
"""

import struct
import sys
import zlib

import numpy as np
import rasterio
from rasterio.profiles import Profile
from rasterio.transform import Affine

# The TIFF tags of a tile as the encoder writes it.
WIDTH, HEIGHT, BITS, COMPRESSION, PHOTOMETRIC = 256, 257, 258, 259, 262
STRIP_OFFSETS, SAMPLES, ROWS_PER_STRIP, STRIP_SIZES, PLANAR = 273, 277, 278, 279, 284
PREDICTOR, SAMPLE_FORMAT = 317, 339
PIXEL_SCALE, TIEPOINT = 33550, 33922
GEO_KEYS, GEO_DOUBLES, GEO_ASCII = 34735, 34736, 34737
GDAL_METADATA, GDAL_NODATA = 42112, 42113

# What differs from one tile of a kind to the next: where its strips lie, and where the
# tile lies on the ground. Every other tag is part of what makes a kind.
PER_TILE = (STRIP_OFFSETS, STRIP_SIZES, TIEPOINT)
# GDAL_METADATA holds the scale and offset of a band, which are in neither the array nor
# the profile, so it is not part of the kind. One entry of it changes the data type
# (older GDAL versions mark a signed 8 bit band there, as PIXELTYPE): a tile that
# carries it is read by GDAL.
KIND = (WIDTH, HEIGHT, BITS, COMPRESSION, PHOTOMETRIC, SAMPLES, ROWS_PER_STRIP, PLANAR,
        PREDICTOR, SAMPLE_FORMAT, PIXEL_SCALE, GEO_KEYS, GEO_DOUBLES, GEO_ASCII, GDAL_NODATA)
TAGS = frozenset(KIND + PER_TILE + (GDAL_METADATA,))

# TIFF field type -> (bytes per value, struct code): BYTE, ASCII, SHORT, LONG, DOUBLE
FIELDS = {1: (1, "B"), 2: (1, "s"), 3: (2, "H"), 4: (4, "I"), 12: (8, "d")}
INTEGERS, DOUBLES = (1, 3, 4), (12,)
# (SampleFormat, BitsPerSample) -> numpy type
DTYPES = {(1, 8): "u1", (1, 16): "u2", (1, 32): "u4",
          (2, 8): "i1", (2, 16): "i2", (2, 32): "i4",
          (3, 32): "f4", (3, 64): "f8"}
DEFLATE, MIN_IS_BLACK = 8, 1
RASTER_TYPE, PIXEL_IS_AREA = 1025, 1        # the GeoKey that says what a pixel stands for

# kind -> (layout, profile) of a kind that is read here, None for one that is left to
# GDAL. A store holds a kind per band type, tile size and CRS: tens, a few hundred with
# every UTM zone. The cap only keeps a process fed with endless different rasters from
# growing without bound.
MAX_KINDS = 4096
_kinds = {}
_NEW = object()             # a kind no tile of which has been seen yet


class _NotPlain(Exception):
    """The blob is not laid out as the encoder writes a tile."""


class _Cut(_NotPlain):
    """The blob ends before what its own header says it holds."""


class BadTile(ValueError):
    """A blob that is not a whole tile as the encoder writes one: `check` says why."""


# The GeoKeys that hold the EPSG code of a projected and of a geographic CRS, and the
# value that stands for a CRS without one.
PROJECTED_CRS, GEOGRAPHIC_CRS, USER_DEFINED = 3072, 2048, 32767
# No tile is larger than this many bytes of pixels. A header that says otherwise is not
# read here: it would be asked for memory before a single strip was looked at.
MAX_PIXEL_BYTES = 1 << 30


def decode(blob):
    """One tile blob -> (array, rasterio profile), as rasterio reads the GeoTIFF: band
    1 as a writable array of the tile's data type, and the profile of the dataset."""
    data = bytes(blob)
    try:
        fields = _fields(data)
    except _NotPlain:
        return _decode_gdal(data)
    kind = tuple(fields.get(tag) for tag in KIND)
    known = _kinds.get(kind, _NEW)
    if known is _NEW:
        return _learn(data, fields, kind)
    if known is None:
        return _decode_gdal(data)
    layout, profile = known
    try:
        return _pixels(data, fields, layout), Profile(profile, transform=_transform(fields))
    except _NotPlain:           # a damaged tile: GDAL reports what is wrong with it
        return _decode_gdal(data)


def check(blob, tile_px=None):
    """One tile blob read without GDAL, for checking a store: a damaged tile is found
    here and never handed on, since what GDAL makes of one depends on its build.

    Returns (array, transform, nodata, epsg): the pixels, where the tile lies, the nodata
    value its header names (None for none) and the EPSG code of its CRS (None for a CRS
    that has no code). Raises BadTile, saying why, for a blob that is not a TIFF, that
    ends before what its header says it holds, that is not laid out as the encoder
    writes a tile, or whose strips do not inflate to the rows they stand for. Every tile
    of a store comes from the encoder, so another layout is a damaged header or a blob
    that something else wrote.

    tile_px: the edge of the tile in pixels, when it is known. A tile of another size
    raises before any of its pixels is read, so a header cannot ask for more memory
    than a tile of that size takes."""
    data = bytes(blob)
    if data[:4] not in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
        raise BadTile("not a TIFF")
    try:
        fields = _fields(data)
        layout = _layout(fields)
        transform = _transform(fields)
        nodata = None
        if GDAL_NODATA in fields:
            nodata = float(fields[GDAL_NODATA][2].rstrip(b"\x00").decode("ascii"))
        keys = _numbers(fields, GEO_KEYS, INTEGERS, ())
    except _Cut:
        raise BadTile("cut short: its header names bytes it does not hold") from None
    except (_NotPlain, ValueError):
        raise BadTile("not laid out as the encoder writes a tile: a damaged header, or a "
                      "blob something else wrote") from None
    _, width, height, _, _ = layout
    if tile_px is not None and (width, height) != (tile_px, tile_px):
        raise BadTile(f"{width} x {height} pixels, not {tile_px} x {tile_px}")
    epsg = None
    for k in range(4, len(keys) - 3, 4):            # (key, where its value is, count, value)
        if keys[k] in (PROJECTED_CRS, GEOGRAPHIC_CRS) and keys[k + 1] == 0:
            if keys[k + 3] != USER_DEFINED and (epsg is None or keys[k] == PROJECTED_CRS):
                epsg = keys[k + 3]
    try:
        return _pixels(data, fields, layout), transform, nodata, epsg
    except _NotPlain:
        raise BadTile("its strips do not hold the rows its size calls for") from None


def _decode_gdal(data):
    with rasterio.MemoryFile(data) as mem:
        with mem.open() as ds:
            return ds.read(1), ds.profile


def _learn(data, fields, kind):
    """Decode the first tile of a kind with GDAL, and note whether the tiles of this
    kind can be read here: only when this one comes out the same both ways."""
    arr, profile = _decode_gdal(data)
    known = None
    try:
        layout = _layout(fields)
        mine = _pixels(data, fields, layout)
        if (mine.dtype == arr.dtype and mine.shape == arr.shape
                and mine.tobytes() == arr.tobytes()
                and _transform(fields) == profile["transform"]):
            known = (layout, dict(profile))
    except _NotPlain:
        pass
    if len(_kinds) >= MAX_KINDS:
        _kinds.clear()
    _kinds[kind] = known
    return arr, profile


def _fields(data) -> dict:
    """tag -> (field type, number of values, their bytes) of a little-endian classic
    TIFF that holds one image and no tag but those of TAGS, each of them once."""
    if data[:4] != b"II*\x00" or sys.byteorder != "little":
        raise _NotPlain
    try:
        (at,) = struct.unpack_from("<I", data, 4)
        (n,) = struct.unpack_from("<H", data, at)
        entries = struct.unpack_from("<" + "HHI4s" * n, data, at + 2)
        (next_image,) = struct.unpack_from("<I", data, at + 2 + 12 * n)
    except struct.error:
        raise _Cut from None
    if next_image:              # an overview or a mask
        raise _NotPlain
    fields = {}
    for k in range(0, 4 * n, 4):
        tag, kind, count, value = entries[k:k + 4]
        if tag not in TAGS or tag in fields or kind not in FIELDS:
            raise _NotPlain
        size = FIELDS[kind][0] * count
        if size > 4:            # the entry holds the place of the values, not the values
            (at,) = struct.unpack("<I", value)
            value = data[at:at + size]
        if len(value) < size:
            raise _Cut
        fields[tag] = (kind, count, value[:size])
    if b"PIXELTYPE" in fields.get(GDAL_METADATA, (2, 0, b""))[2].upper():
        raise _NotPlain
    return fields


def _numbers(fields, tag, kinds, default=None) -> tuple:
    """The values of a field that is stored as one of the field types `kinds`, or
    `default` when the tile does not carry the field."""
    if tag not in fields:
        if default is None:
            raise _NotPlain
        return default
    kind, count, raw = fields[tag]
    if kind not in kinds:
        raise _NotPlain
    return struct.unpack(f"<{count}{FIELDS[kind][1]}", raw)


def _layout(fields) -> tuple:
    """(numpy type, width, height, predictor, rows per strip) of a tile whose pixels
    are read here."""
    def one(tag, default=None):
        values = _numbers(fields, tag, INTEGERS, default)
        if len(values) != 1:
            raise _NotPlain
        return values[0]

    if (one(COMPRESSION), one(PHOTOMETRIC), one(SAMPLES)) != (DEFLATE, MIN_IS_BLACK, 1):
        raise _NotPlain
    # where GDAL places a raster whose pixels stand for points, half a pixel off its
    # tiepoint or not, depends on a configuration option
    keys = _numbers(fields, GEO_KEYS, INTEGERS, ())
    if any(keys[k] == RASTER_TYPE and keys[k + 3] != PIXEL_IS_AREA
           for k in range(4, len(keys) - 3, 4)):
        raise _NotPlain
    dtype = DTYPES.get((one(SAMPLE_FORMAT, (1,)), one(BITS)))
    predictor = one(PREDICTOR, (1,))
    width, height = one(WIDTH), one(HEIGHT)
    rows = one(ROWS_PER_STRIP, (height,))
    if (dtype is None or predictor not in (1, 2, 3) or (predictor == 3 and dtype[0] != "f")
            or min(width, height, rows) < 1
            or width * height * np.dtype(dtype).itemsize > MAX_PIXEL_BYTES):
        raise _NotPlain
    return np.dtype(dtype), width, height, predictor, min(rows, height)


def _pixels(data, fields, layout):
    """The array of a tile: its strips inflated, the predictor undone. Every strip has
    to lie inside the blob and hold its rows, no more and no less, and none is inflated
    past the size its rows call for."""
    dtype, width, height, predictor, rows = layout
    offsets = _numbers(fields, STRIP_OFFSETS, INTEGERS)
    sizes = _numbers(fields, STRIP_SIZES, INTEGERS)
    if not len(offsets) == len(sizes) == -(-height // rows):
        raise _NotPlain
    size = dtype.itemsize
    view, strips, left = memoryview(data), [], height
    for at, n in zip(offsets, sizes):
        want = min(rows, left) * width * size
        left -= rows
        if at + n > len(data):
            raise _NotPlain
        inflate = zlib.decompressobj()
        try:
            strip = inflate.decompress(view[at:at + n], want + 1)
        except zlib.error:
            raise _NotPlain from None
        if len(strip) != want or not inflate.eof or inflate.unused_data:
            raise _NotPlain
        strips.append(strip)
    raw = b"".join(strips)
    if predictor == 1:
        return np.frombuffer(raw, dtype).reshape(height, width).copy()
    if predictor == 2:
        # a row holds the difference of each value to the one before it, taken on the
        # values as unsigned integers of their width, floats too
        steps = np.frombuffer(raw, f"u{size}").reshape(height, width)
        return np.cumsum(steps, axis=1, dtype=steps.dtype).view(dtype)
    # predictor 3: a row holds the bytes of its floats, the most significant byte of
    # every value first, then the next, each byte as the difference to the one before it
    planes = np.cumsum(np.frombuffer(raw, "u1").reshape(height, size * width), axis=1, dtype="u1")
    planes = planes.reshape(height, size, width)[:, ::-1]
    return np.ascontiguousarray(planes.transpose(0, 2, 1)).view(dtype).reshape(height, width)


def _transform(fields) -> Affine:
    """The transform of a north-up tile, from its pixel size and its one tiepoint."""
    scale, tie = _numbers(fields, PIXEL_SCALE, DOUBLES), _numbers(fields, TIEPOINT, DOUBLES)
    if len(scale) != 3 or len(tie) != 6:
        raise _NotPlain
    return Affine(scale[0], 0.0, tie[3] - tie[0] * scale[0], 0.0, -scale[1], tie[4] + tie[1] * scale[1])
