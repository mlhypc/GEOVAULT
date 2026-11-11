"""Offline checks for the MODIS fire adapters: scene math, block reads, daily layers, band
routing, the months of a scene and one scene ingested end to end (no network)."""

from contextlib import contextmanager
from types import SimpleNamespace

import numpy as np
import pytest

from geovault import cli
from geovault.sources import modis_burned, modis_fire, modis_lst, modis_sinu


class Item:
    def __init__(self, item_id, assets, **properties):
        self.id = item_id
        self.assets = {key: SimpleNamespace(href=key) for key in assets}
        self.properties = properties


class Raster:
    """Stand-in for an open rasterio dataset holding one whole scene."""

    def __init__(self, array, dates=None):
        self.array = array
        self.count, self.height, self.width = array.shape
        self.dates = dates

    def tags(self):
        return {"Dates": " ".join(self.dates)} if self.dates else {}

    def read(self, window):
        rows = slice(window.row_off, window.row_off + window.height)
        cols = slice(window.col_off, window.col_off + window.width)
        return self.array[:, rows, cols]


class Writer:
    """Records what an adapter hands to the store."""

    def __init__(self):
        self.rows = []

    def add(self, band, date, x, y, crs, res, tile_px, bounds, nodata, cloud, scene_id,
            blob, valid_px):
        self.rows.append(SimpleNamespace(band=band, date=date, x=x, y=y, nodata=nodata,
                                         cloud=cloud, scene_id=scene_id, valid_px=valid_px))
        return True


def test_scene_bounds_match_a_published_file():
    # h19v04 files carry the origin (1111950.519667, 5559752.598333)
    west, south, east, north = modis_sinu.scene_bounds(19, 4)
    assert abs(west - 1111950.519667) < 1e-3
    assert abs(north - 5559752.598333) < 1e-3
    assert abs((east - west) - 1200 * modis_fire.RES) < 1e-6
    assert abs((north - south) - 2400 * modis_burned.RES) < 1e-6


def test_scene_is_whole_tiles_at_both_resolutions():
    # north-east, south-west and far-east scenes: the math must not depend on the hemisphere
    for h, v in ((20, 5), (12, 10), (35, 8), (0, 9)):
        for source, per_side in ((modis_fire, 5), (modis_burned, 10)):
            grid = source.grid_for()
            tiles = modis_sinu.scene_tiles(grid, h, v)
            assert len(tiles) == per_side * per_side == len(set(tiles))
            west, south, east, north = modis_sinu.scene_bounds(h, v)
            bounds = [grid.tile_bounds(tx, ty) for tx, ty in tiles]
            assert abs(min(b[0] for b in bounds) - west) < 1e-3
            assert abs(max(b[3] for b in bounds) - north) < 1e-3
            assert abs(max(b[2] for b in bounds) - east) < 1e-3
            assert abs(min(b[1] for b in bounds) - south) < 1e-3


def test_fire_tiles_are_the_lst_tiles():
    assert modis_fire.grid_for() == modis_lst.grid_for()


def test_neighbouring_scenes_share_no_tile():
    grid = modis_fire.grid_for()
    a = set(modis_sinu.scene_tiles(grid, 20, 4))
    assert not a & set(modis_sinu.scene_tiles(grid, 21, 4))
    assert not a & set(modis_sinu.scene_tiles(grid, 20, 5))


def test_scene_of_reads_properties_then_id():
    with_props = Item("x", [], **{"modis:horizontal-tile": 21, "modis:vertical-tile": 5})
    assert modis_sinu.scene_of(with_props) == (21, 5)
    from_id = Item("MCD64A1.A2026182.h19v04.061.2026254013029", [])
    assert modis_sinu.scene_of(from_id) == (19, 4)


def test_block_read_cuts_the_right_pixels():
    grid = modis_fire.grid_for()
    px = modis_sinu.scene_px(grid)
    west, _, _, north = modis_sinu.scene_bounds(12, 10)
    # every pixel holds its own position: band 0 the row, band 1 the column
    rows, cols = np.indices((px, px))
    scene = np.stack([rows, cols])
    tiles = modis_sinu.scene_tiles(grid, 12, 10)
    wanted = [tiles[7], tiles[18]]            # two tiles that do not touch
    block = modis_sinu.read_block(Raster(scene), grid, 12, 10, wanted)
    assert block[0].shape[1] < px or block[0].shape[2] < px     # a part of the scene only
    for tx, ty in wanted:
        tile_west, tile_south, tile_east, tile_north = grid.tile_bounds(tx, ty)
        tile_rows = modis_sinu.cut_tile(block, grid, 12, 10, tx, ty, 0)
        tile_cols = modis_sinu.cut_tile(block, grid, 12, 10, tx, ty, 1)
        assert tile_rows.shape == tile_cols.shape == (grid.tile_px, grid.tile_px)
        # the ground position of the tile's corner pixels, from the scene's own geometry
        assert abs(west + tile_cols[0, 0] * grid.res - tile_west) < 1e-3
        assert abs(north - tile_rows[0, 0] * grid.res - tile_north) < 1e-3
        assert abs(west + (tile_cols[-1, -1] + 1) * grid.res - tile_east) < 1e-3
        assert abs(north - (tile_rows[-1, -1] + 1) * grid.res - tile_south) < 1e-3
    # the first tile listed is the scene's south-west corner
    assert modis_sinu.tile_offset(grid, 12, 10, *tiles[0]) == (px - grid.tile_px, 0)


def test_block_read_rejects_a_scene_of_the_wrong_size():
    grid = modis_fire.grid_for()
    tiles = modis_sinu.scene_tiles(grid, 20, 5)
    with pytest.raises(ValueError):
        modis_sinu.read_block(Raster(np.zeros((1, 2400, 2400))), grid, 20, 5, tiles[:1])


def test_newest_production_wins():
    old = Item("MOD14A1.A2026201.h20v05.061.2026210032326", [])
    new = Item("MOD14A1.A2026201.h20v05.061.2026250000000", [])
    other = Item("MYD14A1.A2026201.h20v05.061.2026210032326", [])
    kept = modis_sinu.newest_productions([old, other, new])
    assert sorted(i.id for i in kept) == sorted([new.id, other.id])


def test_fire_layer_dates():
    full = Item("MOD14A1.A2026201.h20v05.061.x", [],
                start_datetime="2026-07-20T00:00:00Z", end_datetime="2026-07-27T23:59:59Z")
    assert modis_fire.layer_dates(full) == [f"2026-07-{d}" for d in range(20, 28)]
    # the last file of a year holds 8 days too and runs into January
    year_end = Item("MOD14A1.A2023361.h20v05.061.x", [],
                    start_datetime="2023-12-27T00:00:00Z", end_datetime="2024-01-03T23:59:59Z")
    assert modis_fire.layer_dates(year_end) == [
        "2023-12-27", "2023-12-28", "2023-12-29", "2023-12-30", "2023-12-31",
        "2024-01-01", "2024-01-02", "2024-01-03"]


def test_scene_months_cover_the_whole_interval():
    def months(**properties):
        return cli.scene_months(Item("x", [], **properties))

    assert months(datetime="2026-07-20T10:00:00Z") == ["2026-07"]
    assert months(datetime="2026-07-28T00:00:00Z", start_datetime="2026-07-28T00:00:00Z",
                  end_datetime="2026-08-04T23:59:59Z") == ["2026-07", "2026-08"]
    assert months(datetime="2023-12-27T00:00:00Z", start_datetime="2023-12-27T00:00:00Z",
                  end_datetime="2024-01-03T23:59:59Z") == ["2023-12", "2024-01"]
    assert months(datetime="2026-07-01T00:00:00Z", start_datetime="2026-07-01T00:00:00Z",
                  end_datetime="2026-07-31T23:59:59Z") == ["2026-07"]
    assert months(datetime="2026-07-20T10:00:00Z", start_datetime=None,
                  end_datetime=None) == ["2026-07"]


def test_fire_band_routing():
    item = Item("MYD14A1.A2026201.h20v05.061.x", ["FireMask", "MaxFRP"], platform="aqua")
    assert modis_fire._suffixes_of(item, modis_fire.DEFAULT_BANDS) == ["fire_mask", "max_frp"]
    # items from December 2024 on carry no platform: the id prefix decides
    no_platform = Item("MOD14A1.A2026201.h20v05.061.x", ["QA"])
    assert modis_fire._suffixes_of(no_platform, modis_fire.DEFAULT_BANDS) == ["qa"]
    assert modis_fire.DEFAULT_BANDS == modis_fire.ALL_BANDS
    assert len(modis_fire.ALL_BANDS) == 8


def test_fire_processed_and_unobserved():
    def mask_of(value):
        return np.full((10, 10), value, dtype=np.uint8)

    assert not modis_fire.is_processed(mask_of(0))
    assert not modis_fire.is_processed(mask_of(2))
    assert modis_fire.is_processed(mask_of(3))            # all water is still processed
    mask = mask_of(5)                                     # land without fire
    mask[0, :] = 4                                        # one row of cloud
    mask[1, :5] = 0                                       # half a row not processed
    mask[2, :5] = 6                                       # half a row unknown
    mask[3, 0] = 9                                        # one fire pixel
    assert modis_fire.is_processed(mask)
    assert modis_fire.unobserved_pct(mask) == 20.0


def test_burned_band_routing_and_unmapped_share():
    item = Item("MCD64A1.A2026182.h20v05.061.x", ["Burn_Date", "QA"])
    assert modis_burned._bands_of(item, modis_burned.DEFAULT_BANDS) == ["burn_date", "qa"]
    burn = np.zeros((10, 10), dtype=np.int16)             # unburned land
    burn[:, :5] = -2                                      # half water
    burn[0, 5:] = modis_burned.UNMAPPED                   # 5 pixels unmapped
    burn[1, 5] = 203                                      # one burned pixel
    assert modis_burned.unmapped_pct(burn) == 5.0


# ---- one fire scene end to end ------------------------------------------------------------

BBOX = (27.0, 37.0, 28.0, 38.0)                           # inside scene h20v05


def fire_item():
    return Item("MOD14A1.A2026209.h20v05.061.2026218123452",
                ["FireMask", "MaxFRP", "QA", "sample"],
                start_datetime="2026-07-28T00:00:00Z", end_datetime="2026-08-04T23:59:59Z",
                **{"modis:horizontal-tile": 20, "modis:vertical-tile": 5})


def fire_scene(days=8):
    """Arrays of one scene: land everywhere, day 3 not processed, one fire on day 1."""
    mask = np.full((days, 1200, 1200), 5, dtype=np.uint8)
    mask[2] = 0
    frp = np.zeros((days, 1200, 1200), dtype=np.int32)
    return {"FireMask": mask, "MaxFRP": frp,
            "QA": np.ones((days, 1200, 1200), dtype=np.uint8),
            "sample": np.zeros((days, 1200, 1200), dtype=np.uint16)}


@pytest.fixture
def opened(monkeypatch):
    """Serve fire_scene() arrays in place of the remote files; returns the keys opened."""
    state = SimpleNamespace(keys=[], scene=fire_scene(), dates=None)

    @contextmanager
    def fake_open(href):
        state.keys.append(href)
        yield Raster(state.scene[href], state.dates)

    monkeypatch.setattr(modis_fire.rasterio, "open", fake_open)
    return state


def test_fire_ingest_stores_processed_days_with_all_bands(opened):
    writer = Writer()
    written = modis_fire.ingest_scene(fire_item(), BBOX, writer)
    tiles = {(row.x, row.y) for row in writer.rows}
    days = {row.date for row in writer.rows}
    assert written == len(writer.rows) == 8 // 2 * 7 * len(tiles)     # 4 bands, 7 days
    assert "2026-07-30" not in days and len(days) == 7                 # day 3 not processed
    assert {"2026-07-28", "2026-08-04"} <= days                        # crosses the month end
    assert {row.band for row in writer.rows} == {
        "terra_fire_mask", "terra_max_frp", "terra_qa", "terra_sample"}
    for row in writer.rows:
        if row.band == "terra_fire_mask":
            assert row.nodata == 0 and row.cloud == 0.0
        else:
            assert row.nodata is None and row.cloud is None
            assert row.valid_px == modis_fire.TILE_PX ** 2             # zeros are values


def test_fire_ingest_reads_the_mask_even_when_it_is_not_requested(opened):
    writer = Writer()
    modis_fire.ingest_scene(fire_item(), BBOX, writer, bands=["terra_max_frp"])
    assert sorted(opened.keys) == ["FireMask", "MaxFRP"]
    assert {row.band for row in writer.rows} == {"terra_max_frp"}
    assert "2026-07-30" not in {row.date for row in writer.rows}


def test_fire_ingest_skips_what_the_store_holds(opened):
    writer = Writer()
    modis_fire.ingest_scene(fire_item(), BBOX, writer)
    skip = {(row.band, row.date, modis_fire.CRS, row.x, row.y) for row in writer.rows}
    plan = modis_fire.plan_scene(fire_item(), BBOX, skip_keys=skip)
    tiles = {(row.x, row.y) for row in writer.rows}
    # only the day that was not processed is left, for every band
    assert plan == {band: len(tiles) for band in modis_fire.ALL_BANDS if band.startswith("terra")}
    again = Writer()
    assert modis_fire.ingest_scene(fire_item(), BBOX, again, skip_keys=skip) == 0


def test_fire_ingest_dates_a_short_file_from_its_tag(opened):
    # a file written during an outage: 5 bands, the days named by the Dates tag
    opened.scene.update(fire_scene(days=5))
    opened.dates = ["2026-07-29", "2026-07-30", "2026-08-02", "2026-08-03", "2026-08-04"]
    writer = Writer()
    modis_fire.ingest_scene(fire_item(), BBOX, writer, bands=["terra_fire_mask"])
    # the third band (2 August) is the one not processed in fire_scene()
    assert sorted({row.date for row in writer.rows}) == [
        "2026-07-29", "2026-07-30", "2026-08-03", "2026-08-04"]


def test_fire_ingest_fails_when_bands_and_days_disagree(opened):
    opened.scene.update(fire_scene(days=5))          # 5 bands, no Dates tag, 8 days planned
    with pytest.raises(ValueError):
        modis_fire.ingest_scene(fire_item(), BBOX, Writer())
    opened.dates = ["2026-07-29", "2026-07-30"]       # 5 bands, 2 dates
    with pytest.raises(ValueError):
        modis_fire.ingest_scene(fire_item(), BBOX, Writer())
