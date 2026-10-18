"""AC2: world locator for pre-26.1 and 26.1+ layouts."""

from pathlib import Path

import pytest

from mcusd.world.layout import (
    DIMENSIONS,
    LAYOUT_26_1,
    LAYOUT_PRE_26_1,
    WorldLayoutError,
    get_dimension,
    locate,
)

LEGACY = {"overworld": "region", "the_nether": "DIM-1/region", "the_end": "DIM1/region"}
MODERN = {
    "overworld": "dimensions/minecraft/overworld/region",
    "the_nether": "dimensions/minecraft/the_nether/region",
    "the_end": "dimensions/minecraft/the_end/region",
}


def make_world(root: Path, region_dirs) -> Path:
    root.mkdir(parents=True)
    (root / "level.dat").write_bytes(b"\x1f\x8b")  # content is not read by the locator
    for rel in region_dirs:
        (root / rel).mkdir(parents=True)
    return root


@pytest.mark.parametrize("dimension", list(DIMENSIONS))
def test_pre_26_1_layout(tmp_path, dimension):
    world = make_world(tmp_path / "w", LEGACY.values())
    loc = locate(world, dimension)
    assert loc.region_dir == world / LEGACY[dimension]
    assert loc.layout == LAYOUT_PRE_26_1
    assert loc.dimension.name == dimension


@pytest.mark.parametrize("dimension", list(DIMENSIONS))
def test_26_1_layout(tmp_path, dimension):
    world = make_world(tmp_path / "w", MODERN.values())
    loc = locate(world, dimension)
    assert loc.region_dir == world / MODERN[dimension]
    assert loc.layout == LAYOUT_26_1


def test_namespaced_dimension_name_is_accepted(tmp_path):
    world = make_world(tmp_path / "w", MODERN.values())
    assert locate(world, "minecraft:the_nether").region_dir == world / MODERN["the_nether"]


def test_modern_layout_preferred_when_both_exist(tmp_path):
    world = make_world(tmp_path / "w", list(LEGACY.values()) + list(MODERN.values()))
    loc = locate(world, "overworld")
    assert loc.layout == LAYOUT_26_1
    assert loc.region_dir == world / MODERN["overworld"]


def test_folder_that_is_not_a_world_names_missing_level_dat(tmp_path):
    folder = tmp_path / "photos"
    folder.mkdir()
    with pytest.raises(WorldLayoutError) as excinfo:
        locate(folder, "overworld")
    assert str(folder / "level.dat") in str(excinfo.value)


def test_missing_folder(tmp_path):
    with pytest.raises(WorldLayoutError, match="not found"):
        locate(tmp_path / "nope", "overworld")


def test_missing_dimension_names_both_candidate_paths(tmp_path):
    world = make_world(tmp_path / "w", [MODERN["overworld"]])
    with pytest.raises(WorldLayoutError) as excinfo:
        locate(world, "the_end")
    message = str(excinfo.value)
    assert str(world / MODERN["the_end"]) in message
    assert str(world / LEGACY["the_end"]) in message


def test_unknown_dimension():
    with pytest.raises(WorldLayoutError, match="unknown dimension"):
        get_dimension("the_aether")


def test_dimension_heights():
    assert (DIMENSIONS["overworld"].min_y, DIMENSIONS["overworld"].max_y) == (-64, 319)
    assert DIMENSIONS["the_nether"].min_y == 0
