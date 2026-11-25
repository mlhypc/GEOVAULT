"""Offline tests for `geovault ingest` from the command line down to the store, with
sources that fetch nothing: a dated one (scenes found for a date range and stored one by
one) and a static one. What the command promises is tested here: a second run skips what
is stored, `--dry-run` says what is missing, a scene that fails is reported and retried,
and a finished run leaves sealed months and a catalog that lists them."""

from datetime import date as _date, timedelta
from types import SimpleNamespace

import numpy as np
import pytest

from geovault import catalog, cli, reader, store
from geovault.aoi import tile_filter
from geovault.grid import Grid
from geovault.sources import REGISTRY, common
from geovault.verify import verify

CRS, RES, PX = "EPSG:4326", 0.01, 10
GRID = Grid(CRS, RES, PX)
BBOX = (35.0, 36.0, 35.2, 36.1)             # two tiles wide, one high
AREA = [str(v) for v in BBOX]


def _store_tiles(writer, band, date, value, bbox, skip, aoi, scene):
    """Store the tiles of the test grid over bbox / aoi that are not in `skip`."""
    in_aoi, n = tile_filter(aoi), 0
    for tx, ty in common.candidate_tiles(GRID, bbox, CRS, aoi):
        bounds = common.wgs84_bounds(GRID, tx, ty, CRS)
        if (band, date, CRS, tx, ty) in skip or not in_aoi(bounds):
            continue
        arr = np.full((PX, PX), value, np.int16)
        blob = common.encode_geotiff(arr, GRID, tx, ty, CRS, -1)
        n += writer.add(band, date, tx, ty, CRS, RES, PX, bounds, -1, None, scene, blob, arr.size)
    return n


def dated_source(failing=()):
    """A source with one scene a day. Scenes whose day is in `failing` raise."""
    def search(bbox, start, end, cloud_max=None, limit=None):
        day, items = _date.fromisoformat(start), []
        while day <= _date.fromisoformat(end):
            items.append(SimpleNamespace(id=f"scene-{day}", day=day,
                                         properties={"datetime": f"{day}T00:00:00Z"}))
            day += timedelta(days=1)
        return items[:limit] if limit else items

    def todo(item, bbox, bands, skip_keys, aoi):
        in_aoi = tile_filter(aoi)
        return {band: [t for t in common.candidate_tiles(GRID, bbox, CRS, aoi)
                       if (band, str(item.day), CRS, *t) not in (skip_keys or ())
                       and in_aoi(common.wgs84_bounds(GRID, *t, CRS))]
                for band in (bands or ["v"])}

    def plan_scene(item, bbox, bands=None, skip_keys=None, keep_offzone=False, aoi=None):
        return {band: len(tiles) for band, tiles in todo(item, bbox, bands, skip_keys, aoi).items()
                if tiles}

    def ingest_scene(item, bbox, writer, bands=None, skip_keys=None, log=print,
                     keep_offzone=False, aoi=None):
        if str(item.day) in failing:
            raise OSError("the server did not answer")
        return sum(_store_tiles(writer, band, str(item.day), item.day.day, bbox,
                                skip_keys or (), aoi, item.id) for band in (bands or ["v"]))

    return SimpleNamespace(__doc__="A source of the tests. One scene a day.", RES=RES,
                           search=search, plan_scene=plan_scene, ingest_scene=ingest_scene)


def static_source(unread=0):
    """A static source. `unread` files could not be opened this run."""
    def ingest(bbox, writer, bands=None, skip_keys=None, aoi=None, log=print):
        if unread:
            writer.leave_out(unread)
        return _store_tiles(writer, "h", "", 3, bbox, skip_keys or (), aoi, "file")

    return SimpleNamespace(__doc__="A static source of the tests.", STATIC=True, RES_LIST=[RES],
                           ingest=ingest)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ROOT", tmp_path)            # the run log goes to ROOT/logs
    monkeypatch.setattr(store, "STORE", tmp_path / "store")
    monkeypatch.setattr(catalog, "STORE", tmp_path / "store")
    monkeypatch.setattr(catalog, "CATALOG", tmp_path / "catalog")
    return tmp_path


def run(monkeypatch, source, *args):
    monkeypatch.setitem(REGISTRY, "fake", source)
    try:
        return cli.main(["ingest", "--dataset", "fake", *args])
    finally:
        import logging
        for h in list(logging.getLogger("geovault").handlers):    # the run log is a file in tmp_path
            h.close()
            logging.getLogger("geovault").removeHandler(h)


def stored(band="v"):
    return [(r["date"], r["x"], r["y"]) for r in reader.tiles_for_bbox("fake", (30, 30, 40, 40), band)]


def test_a_dated_ingest_stores_seals_and_lists(vault, monkeypatch, capsys):
    days = ["--start", "2026-01-30", "--end", "2026-02-02"]         # across a month end
    assert run(monkeypatch, dated_source(), "--bbox", *AREA, *days) == 0
    out = capsys.readouterr().out
    assert "4 scenes found" in out and "done: 8 new tiles" in out
    assert len(stored()) == 8 and {d for d, _, _ in stored()} == {
        "2026-01-30", "2026-01-31", "2026-02-01", "2026-02-02"}
    assert store.open_months("fake") == [] and verify("fake", deep=True).ok
    assert len(list((vault / "logs").glob("fake_*.log"))) == 1
    arr, _, _ = reader.mosaic("fake", BBOX, "v", "2026-01-31")
    assert (arr == 31).all()

    # the same command again fetches nothing; a longer range fetches only what is new
    assert run(monkeypatch, dated_source(), "--bbox", *AREA, *days) == 0
    out = capsys.readouterr().out
    assert "8 tiles already cached" in out and "done: 0 new tiles" in out
    assert run(monkeypatch, dated_source(), "--bbox", *AREA, "--start", "2026-01-30",
               "--end", "2026-02-03") == 0
    assert "done: 2 new tiles" in capsys.readouterr().out and len(stored()) == 10


def test_a_dry_run_says_what_is_missing_and_fetches_nothing(vault, monkeypatch, capsys):
    days = ["--start", "2026-01-01", "--end", "2026-01-03"]
    assert run(monkeypatch, dated_source(), "--bbox", *AREA, *days, "--dry-run") == 1
    assert "dry-run: 6 tiles would be fetched across 3 scenes" in capsys.readouterr().out
    assert not (vault / "store").exists()
    assert run(monkeypatch, dated_source(), "--bbox", *AREA, *days) == 0
    assert run(monkeypatch, dated_source(), "--bbox", *AREA, *days, "--dry-run") == 0
    assert "dry-run: 0 tiles would be fetched" in capsys.readouterr().out


def test_a_scene_that_fails_is_reported_and_fetched_by_the_next_run(vault, monkeypatch, capsys):
    days = ["--start", "2026-01-01", "--end", "2026-01-03"]
    assert run(monkeypatch, dated_source(failing=["2026-01-02"]), "--bbox", *AREA, *days) == 2
    out = capsys.readouterr().out
    assert "scene-2026-01-02  FAILED OSError: the server did not answer" in out
    assert "1 scenes FAILED" in out and "done: 4 new tiles" in out and "1 scenes failed" in out
    assert {d for d, _, _ in stored()} == {"2026-01-01", "2026-01-03"}
    assert store.open_months("fake") == [] and verify("fake").ok     # what was fetched is sealed
    assert run(monkeypatch, dated_source(), "--bbox", *AREA, *days) == 0
    assert "done: 2 new tiles" in capsys.readouterr().out and len(stored()) == 6


def test_the_area_is_a_bbox_a_polygon_or_a_point(vault, monkeypatch, capsys):
    days = ["--start", "2026-01-01", "--end", "2026-01-01"]
    aoi = vault / "aoi.geojson"
    aoi.write_text('{"type": "Polygon", "coordinates": [[[35.01, 36.01], [35.04, 36.01], '
                   '[35.04, 36.04], [35.01, 36.04], [35.01, 36.01]]]}', encoding="utf-8")
    assert run(monkeypatch, dated_source(), "--geojson", str(aoi), *days) == 0
    assert stored() == [("2026-01-01", 350, 360)]
    assert run(monkeypatch, dated_source(), "--point", "36.05", "35.15", "--buffer", "100", *days) == 0
    assert stored() == [("2026-01-01", 350, 360), ("2026-01-01", 351, 360)]
    capsys.readouterr()
    for missing in ([*days], ["--bbox", *AREA], ["--bbox", *AREA, "--start", "2026-01-01"]):
        with pytest.raises(SystemExit) as stop:
            run(monkeypatch, dated_source(), *missing)
        assert stop.value.code == 2
    err = capsys.readouterr().err
    assert "ingest needs --bbox, --geojson or --point" in err
    assert "fake is time-varying: --start and --end are required" in err


def test_scenes_and_bands_can_be_limited(vault, monkeypatch, capsys):
    assert run(monkeypatch, dated_source(), "--bbox", *AREA, "--start", "2026-01-01", "--end",
               "2026-01-09", "--max-scenes", "2", "--bands", "a", "b", "--workers", "2") == 0
    assert "2 scenes found" in capsys.readouterr().out
    assert len(stored("a")) == 4 and len(stored("b")) == 4 and stored("v") == []


def test_a_static_ingest(vault, monkeypatch, capsys):
    assert run(monkeypatch, static_source(), "--bbox", *AREA) == 0
    assert "done: 2 new tiles" in capsys.readouterr().out
    assert stored("h") == [("", 350, 360), ("", 351, 360)]
    assert store.open_months("fake") == [] and verify("fake", deep=True).ok
    assert run(monkeypatch, static_source(), "--bbox", *AREA) == 0
    out = capsys.readouterr().out
    assert "2 tiles already cached" in out and "done: 0 new tiles" in out

    # a file that did not open: the run says so and exits with 2
    assert run(monkeypatch, static_source(unread=1), "--bbox", "35.0", "36.0", "35.3", "36.1") == 2
    assert "done: 1 new tiles" in (out := capsys.readouterr().out) and "1 files, cells or tiles left out" in out
    with pytest.raises(SystemExit):
        run(monkeypatch, static_source(), "--bbox", *AREA, "--dry-run")
    assert "fake is static and does not support --dry-run" in capsys.readouterr().err


def test_the_commands_that_look_after_a_store(vault, monkeypatch, capsys):
    days = ["--start", "2026-01-01", "--end", "2026-01-02"]
    assert run(monkeypatch, dated_source(), "--bbox", *AREA, *days) == 0
    capsys.readouterr()

    assert cli.main(["coverage", "fake"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "  2026-01-01  v        2 tiles  cloud=None", "  2026-01-02  v        2 tiles  cloud=None"]
    assert cli.main(["coverage", "fake", "--date", "2026-01-02", "--bbox", *AREA]) == 0
    assert capsys.readouterr().out.count("tiles") == 1
    assert cli.main(["coverage", "fake", "--date", "2031"]) == 0
    assert capsys.readouterr().out.strip() == "no records"
    with pytest.raises(SystemExit):                 # west of east
        cli.main(["coverage", "fake", "--bbox", "36", "36", "35", "37"])
    capsys.readouterr()

    w = store.Writer("fake")                        # an interrupted run: a part and no catalog row
    _store_tiles(w, "v", "2026-01-03", 3, BBOX, (), None, "scene-2026-01-03")
    w.flush()
    assert cli.main(["verify", "fake"]) == 1
    assert "1 open part files" in capsys.readouterr().out
    assert cli.main(["compact", "fake"]) == 0
    assert capsys.readouterr().out.strip() == "compacted 1 (res, month) groups"
    assert cli.main(["verify", "fake", "--deep"]) == 0
    capsys.readouterr()

    catalog.path("fake").unlink()
    assert cli.main(["rebuild-catalog", "fake"]) == 0
    assert capsys.readouterr().out.strip() == "catalog rebuilt: fake (6 rows)"
    assert cli.main(["reencode", "fake", "--workers", "2"]) == 0
    out = capsys.readouterr().out
    assert "[1/1] 2026-01 r0: 6 tiles" in out and "reencoded 6 tiles" in out
    assert cli.main(["verify", "fake", "--deep"]) == 0 and len(stored()) == 6
