# api/

Credentials for the few sources that are not anonymous. Every file here except the
`*.example` templates is git-ignored, so a clone of the repo never carries a key and
anyone using the vault adds their own by copying the template.

| File | Used by | How to get it |
|---|---|---|
| `cdsapirc` | `era5land`, `era5land_daily`, `agera5` | free Copernicus CDS account, see `cdsapirc.example`; install the extra with `pip install -e .[cds]` |
| `cdse_s3` | `cgls_ndvi300` | free Copernicus Data Space (CDSE) account, then an S3 key pair from the eodata S3 keys manager, see `cdse_s3.example` |

Lookup order for each credential: this folder, then the tool's conventional home
location (for CDS: `~/.cdsapirc`), then environment variables (`CDSAPI_URL`,
`CDSAPI_KEY`; `CDSE_S3_ACCESS_KEY`, `CDSE_S3_SECRET_KEY`). The anonymous adapters (Sentinel, Landsat, DEM, SoilGrids, CHIRPS,
MODIS, ARCO ERA5) never look here.
