"""MODIS sinusoidal scene math shared by adapters that read whole-scene arrays.

MODIS land products on the sinusoidal tile grid are cut into the same 36 x 18 scenes
(hXXvYY) of 10 degrees, on a sphere of radius 6371007.181 m. A scene is 1200, 2400 or 4800
pixels across depending on the product resolution, so its edges, the store tiles it holds
and each tile's pixel offset inside it are known without opening a file. Adapters whose
tile size divides the scene use this to plan a scene offline and to cut tiles out of one
array read per asset, instead of one remote read per tile.
"""

import numpy as np
from rasterio.windows import Window

CRS = "+proj=sinu +lon_0=0 +x_0=0 +y_0=0 +R=6371007.181 +units=m +no_defs"
# The grid is defined by its published corner, rounded to the millimetre, not by pi * R:
# the two differ by 0.1 mm per scene, and files carry the published values.
WEST_EDGE = -20015109.354                    # left edge of h00, metres
NORTH_EDGE = 10007554.677                    # top edge of v00
SCENE_SIZE = -WEST_EDGE / 18                 # 10 degrees: 1111950.519667 m


def scene_of(item) -> tuple:
    """(h, v) of a STAC item, from its MODIS properties or, failing that, its id."""
    props = item.properties
    if "modis:horizontal-tile" in props and "modis:vertical-tile" in props:
        return int(props["modis:horizontal-tile"]), int(props["modis:vertical-tile"])
    for part in item.id.split("."):
        if len(part) == 6 and part[0] == "h" and part[3] == "v":
            return int(part[1:3]), int(part[4:6])
    raise ValueError(f"no MODIS tile id in {item.id}")


def scene_bounds(h: int, v: int) -> tuple:
    """(west, south, east, north) of scene hXXvYY in sinusoidal metres."""
    west = WEST_EDGE + h * SCENE_SIZE
    north = NORTH_EDGE - v * SCENE_SIZE
    return (west, north - SCENE_SIZE, west + SCENE_SIZE, north)


def scene_px(grid) -> int:
    """Scene edge in pixels at this grid's resolution (1200 at 1 km, 2400 at 500 m)."""
    return round(SCENE_SIZE / grid.res)


def scene_tiles(grid, h: int, v: int) -> list:
    """Store tiles (tx, ty) that make up one scene. The grid must be anchored at the CRS
    origin with a tile size that divides the scene, so no tile straddles two scenes."""
    west, south, _, _ = scene_bounds(h, v)
    per_side = scene_px(grid) // grid.tile_px
    tx0 = round(west / grid.tile_size)
    ty0 = round(south / grid.tile_size)
    return [(tx0 + i, ty0 + j) for j in range(per_side) for i in range(per_side)]


def tile_offset(grid, h: int, v: int, tx: int, ty: int) -> tuple:
    """(row, col) of a tile's top-left pixel inside its scene array."""
    west, _, _, north = scene_bounds(h, v)
    tile_west, _, _, tile_north = grid.tile_bounds(tx, ty)
    return (round((north - tile_north) / grid.res), round((tile_west - west) / grid.res))


def read_block(ds, grid, h: int, v: int, tiles) -> tuple:
    """Read, in one request, the smallest pixel block of an open scene raster that covers
    `tiles`, all bands. Returns (array[band, row, col], row0, col0)."""
    expected = scene_px(grid)
    if ds.width != expected or ds.height != expected:
        raise ValueError(f"scene is {ds.width} x {ds.height} px, expected {expected}")
    offsets = [tile_offset(grid, h, v, tx, ty) for tx, ty in tiles]
    row0 = min(r for r, _ in offsets)
    col0 = min(c for _, c in offsets)
    rows = max(r for r, _ in offsets) + grid.tile_px - row0
    cols = max(c for _, c in offsets) + grid.tile_px - col0
    return ds.read(window=Window(col0, row0, cols, rows)), row0, col0


def cut_tile(block, grid, h: int, v: int, tx: int, ty: int, layer: int = 0):
    """One tile of one band out of a block returned by read_block."""
    array, row0, col0 = block
    row, col = tile_offset(grid, h, v, tx, ty)
    row, col = row - row0, col - col0
    return np.ascontiguousarray(array[layer, row:row + grid.tile_px, col:col + grid.tile_px])


def newest_productions(items) -> list:
    """Keep the latest production of each scene. Reprocessed scenes appear twice in a
    search, with ids that differ only in the trailing production timestamp."""
    newest = {}
    for item in items:
        key = ".".join(item.id.split(".")[:4])
        if key not in newest or item.id > newest[key].id:
            newest[key] = item
    return list(newest.values())
