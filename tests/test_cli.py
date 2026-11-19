"""Offline tests for the commands of the CLI that need no store and no network."""

import pytest

import geovault
from geovault import cli
from geovault.sources import REGISTRY


def test_version(capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main(["--version"])
    assert stop.value.code == 0
    assert capsys.readouterr().out.strip() == f"geovault {geovault.__version__}"


def test_datasets_lists_the_registry(capsys):
    assert cli.main(["datasets"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in lines] == list(REGISTRY)
    for line, src in zip(lines, REGISTRY.values()):
        name, kind, title = line.split(None, 2)
        assert kind == ("static" if getattr(src, "STATIC", False) else "dated")
        assert len(title) > 20, f"{name} has no description"
    # the first sentence, also where it runs over two lines of the docstring
    assert dict(line.split(None, 2)[::2] for line in lines)["modis_lst"].endswith(
        "via Microsoft Planetary Computer")


def test_every_adapter_says_how_it_is_ingested():
    """What the CLI asks an adapter for: a static one stores an area once (ingest), a dated
    one finds scenes for a date range (search) and stores them one by one (ingest_scene)."""
    for name, src in REGISTRY.items():
        if getattr(src, "STATIC", False):
            assert callable(getattr(src, "ingest", None)) and src.RES_LIST, name
        else:
            assert callable(getattr(src, "search", None)), name
            assert callable(getattr(src, "ingest_scene", None)), name
            assert hasattr(src, "RES") or hasattr(src, "BAND_TO_ASSET"), name
