"""Source adapters. Adding a dataset = one module (search + ingest_scene) plus
one REGISTRY line; the engine (grid/store/catalog/reader) never changes."""

from . import s2_earthsearch, s1_pc, landsat, dem_glo30, dem_glo90, worldcover, soilgrids, chirps, modis_lst, modis_vi, modis_vi_1km, s3_syn_v10, cgls_ndvi300, era5, era5land, era5land_daily, agera5

REGISTRY = {
    "s2": s2_earthsearch,
    "s1": s1_pc,
    "landsat": landsat,
    "glo30": dem_glo30,
    "glo90": dem_glo90,
    "worldcover": worldcover,
    "soilgrids": soilgrids,
    "chirps": chirps,
    "modis_lst": modis_lst,
    "modis_vi": modis_vi,
    "modis_vi_1km": modis_vi_1km,
    "s3_syn_v10": s3_syn_v10,
    "cgls_ndvi300": cgls_ndvi300,
    "era5": era5,
    "era5land": era5land,
    "era5land_daily": era5land_daily,
    "agera5": agera5,
}
