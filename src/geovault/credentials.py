"""Credentials for the few non-anonymous sources. Files live in <repo>/api/ (git-ignored except
the *.example templates); see api/README.md. Anonymous adapters never import this module."""

import os
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[2] / "api"


def _read_rc(path: Path) -> dict:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and ":" in line:
                k, _, v = line.partition(":")
                out[k.strip()] = v.strip()
    return out


def cds() -> tuple:
    """(url, key) for the Copernicus Climate Data Store. Order: api/cdsapirc, ~/.cdsapirc,
    CDSAPI_URL / CDSAPI_KEY. Raises with instructions when nothing is configured."""
    for p in (API_DIR / "cdsapirc", Path.home() / ".cdsapirc"):
        rc = _read_rc(p)
        if rc.get("key") and "paste-your" not in rc["key"]:
            return rc.get("url", "https://cds.climate.copernicus.eu/api"), rc["key"]
    url, key = os.environ.get("CDSAPI_URL"), os.environ.get("CDSAPI_KEY")
    if key:
        return url or "https://cds.climate.copernicus.eu/api", key
    raise RuntimeError(
        f"no CDS credentials: copy {API_DIR / 'cdsapirc.example'} to {API_DIR / 'cdsapirc'} "
        "and paste your Personal Access Token (see api/README.md)")
