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
For those the anchor (ox, oy) is the file origin modulo the tile size (nearest
corner), which is the same value for every file of the product once the tile
size divides the file spacing, so the tile lattice is still global: two COG files of the same
product (neighbouring 1-degree cells) yield the same (x, y) for the same ground.
Anchoring at each file's own corner modulo the tile size, as an earlier version
did, made neighbouring files disagree whenever the file size in pixels is not a
multiple of the tile size (3600 px per degree vs 256), which collided keys and
silently dropped tiles. Static sources pick a tile_px that divides their file
size (240) so no tile straddles two files.

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
        """Grid on the source raster's pixel lattice, anchored globally: only the
        sub-pixel shift of the raster origin relative to the CRS origin is kept
        (e.g. the half-pixel of a pixel-centre-registered DEM), so every file of
        the product shares one lattice and one (x, y) numbering."""
        res = transform.a
        ts = res * tile_px
        # the file origin must itself be a tile corner: anchor = origin modulo the tile
        # size, nearest corner. Identical for every file of a product as long as the
        # file spacing is a multiple of the tile size (checked by aligned_with_raster).
        ox = transform.c - ts * math.floor(transform.c / ts + 0.5)
        oy = transform.f - ts * math.floor(transform.f / ts + 0.5)
        return cls(str(crs), res, tile_px, ox, oy)

    def aligned_with_raster(self, transform, width: int, height: int) -> bool:
        """True when the raster edges fall on tile edges of this grid, i.e. no
        tile straddles this raster and its neighbour (needs width % tile_px == 0
        and the raster origin on a tile corner)."""
        if width % self.tile_px or height % self.tile_px:
            return False
        kx = (transform.c - self.ox) / self.tile_size
        ky = (transform.f - self.oy) / self.tile_size
        return abs(kx - round(kx)) < 1e-6 and abs(ky - round(ky)) < 1e-6

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
