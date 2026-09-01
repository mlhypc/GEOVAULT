"""List catalog rows over a bbox and mosaic one band/date into a numpy array."""

from geovault.reader import tiles_for_bbox, mosaic

bbox = (35.30, 36.71, 35.33, 36.73)

rows = tiles_for_bbox("s2", bbox, band="B4")
print(f"catalog: {len(rows)} B4 tiles")
for r in rows[:3]:
    print(f"  {r['date']} x={r['x']} y={r['y']} cloud={r['cloud_pct']}")

arr, transform, crs = mosaic("s2", bbox, "B4", "2024-06-03")
valid = arr[arr > 0]
print(f"mosaic: {arr.shape} {arr.dtype} {crs}, value range {valid.min()}-{valid.max()}")
