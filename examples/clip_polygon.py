"""Clip a stored mosaic to a polygon (pixels outside become nodata)."""

from pathlib import Path

from geovault.reader import clip

parcel = Path(__file__).parent / "parcel.geojson"
arr, transform, crs = clip("s2", str(parcel), "B4", "2024-06-03")

inside = (arr > 0).sum()
print(f"clip: {arr.shape} {crs}, pixels inside polygon: {inside} ({inside / arr.size * 100:.0f}%)")
print(f"value range inside: {arr[arr > 0].min()}-{arr[arr > 0].max()}")
