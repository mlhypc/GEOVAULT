"""Native-grid tiling math.

A Grid is a fixed lattice in a dataset's native CRS: pixel size, an anchor, and
square tiles of tile_px pixels. Sentinel products (S2 L2A, S1 RTC) sit on
absolute UTM lattices whose pixel edges are multiples of the resolution, so the
default anchor at the CRS origin is deterministic: the same ground always maps
to the same (x, y) tile index, across runs and across datasets that share a
resolution. (A floating per-run anchor breaks deduplication; learned the hard
way in a previous system.)

Some static sources are not aligned to their CRS origin (Copernicus DEM is
pixel-center registered, so its edges sit half a pixel off the degree lines).
For those the anchor (ox, oy) is derived from the source's own transform,
modulo the tile size, which is still deterministic per source.

Tile index (x, y): x counts east from 0, y counts north from 0.
"""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Grid:
    crs: str          # e.g. "EPSG:32636"
    res: float        # pixel size in CRS units
    tile_px: int      # tile edge in pixels (256)
    ox: float = 0.0   # lattice anchor (defaults to the CRS origin)
    oy: float = 0.0

    @property
    def tile_size(self) -> float:
        return self.res * self.tile_px

    @classmethod
    def from_transform(cls, crs, transform, tile_px):
        """Grid aligned to a source raster's own pixel lattice."""
        res = transform.a
        ts = res * tile_px
        return cls(str(crs), res, tile_px, transform.c % ts, transform.f % ts)

    def tile_of_point(self, cx: float, cy: float) -> tuple:
        """Tile index containing a native-CRS point."""
        return (math.floor((cx - self.ox) / self.tile_size),
                math.floor((cy - self.oy) / self.tile_size))

    def tile_bounds(self, tx: int, ty: int) -> tuple:
        """(west, south, east, north) of a tile in native CRS units."""
        w = self.ox + tx * self.tile_size
        s = self.oy + ty * self.tile_size
        return (w, s, w + self.tile_size, s + self.tile_size)

    def tile_transform(self, tx: int, ty: int) -> tuple:
        """GDAL-style affine (px, 0, ox, 0, -px, oy); origin is the top-left corner."""
        w, s, e, n = self.tile_bounds(tx, ty)
        return (self.res, 0.0, w, 0.0, -self.res, n)

    def tiles_for_bounds(self, w: float, s: float, e: float, n: float):
        """All tile indices intersecting a native-CRS rectangle."""
        x0 = math.floor((w - self.ox) / self.tile_size)
        x1 = math.ceil((e - self.ox) / self.tile_size) - 1
        y0 = math.floor((s - self.oy) / self.tile_size)
        y1 = math.ceil((n - self.oy) / self.tile_size) - 1
        for ty in range(y0, y1 + 1):
            for tx in range(x0, x1 + 1):
                yield (tx, ty)
