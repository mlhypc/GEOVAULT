"""Source adapters. Adding a dataset = one module (search + ingest_scene) plus
one REGISTRY line; the engine (grid/store/catalog/reader) never changes."""

from . import s2_earthsearch, s1_pc, landsat, dem_glo30, worldcover, soilgrids, chirps, modis_lst, era5, era5land, era5land_daily, agera5

REGISTRY = {
    "s2": s2_earthsearch,
    "s1": s1_pc,
    "landsat": landsat,
    "glo30": dem_glo30,
    "worldcover": worldcover,
    "soilgrids": soilgrids,
    "chirps": chirps,
    "modis_lst": modis_lst,
    "era5": era5,
    "era5land": era5land,
    "era5land_daily": era5land_daily,
    "agera5": agera5,
}
