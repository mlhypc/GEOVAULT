"""PARDIG-NRT precipitation, 15-minute, 0.1 degree, 2016 to now (archive + live stream).
See pardig.py for the product, the bucket layout and the grid."""

from . import pardig

PRODUCT = "NRT"
DATASET = "pardig_nrt"
RES = pardig.RES
DEFAULT_BANDS = pardig.DEFAULT_BANDS
grid_for = pardig.grid_for
plan_scene = pardig.plan_scene
ingest_scene = pardig.ingest_scene


def search(bbox, start, end, cloud_max=None, limit=None):
    """Archive years and live days in [start, end]. cloud_max is accepted for CLI symmetry."""
    return pardig.search(PRODUCT, DATASET, bbox, start, end, limit)
