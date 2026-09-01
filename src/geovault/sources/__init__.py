"""Source adapters. Adding a dataset = one module (search + ingest_scene) plus
one REGISTRY line; the engine (grid/store/catalog/reader) never changes."""

from . import s2_earthsearch, s1_pc, dem_glo30, worldcover, soilgrids

REGISTRY = {
    "s2": s2_earthsearch,
    "s1": s1_pc,
    "glo30": dem_glo30,
    "worldcover": worldcover,
    "soilgrids": soilgrids,
}
