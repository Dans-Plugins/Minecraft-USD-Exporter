"""AC2: world locator for pre-26.1 and 26.1+ layouts.

Also characterizes dimension lookup, the dimension table and the locator's error paths.
"""

import dataclasses
from pathlib import Path

import pytest

from mcusd.world.layout import (
    DIMENSIONS,
    LAYOUT_26_1,
    LAYOUT_PRE_26_1,
    Dimension,
    WorldLayoutError,
    WorldLocation,
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


# --- characterization of paths the tests above do not exercise ---


@pytest.mark.parametrize("name", list(DIMENSIONS))
def test_get_dimension_accepts_short_and_namespaced_names(name):
    assert get_dimension(name) is DIMENSIONS[name]
    assert get_dimension(f"minecraft:{name}") is DIMENSIONS[name]
    assert DIMENSIONS[name].namespaced_id == f"minecraft:{name}"


@pytest.mark.parametrize(
    "name", ["Overworld", "OVERWORLD", "other:overworld", "minecraft:minecraft:overworld", "", "minecraft:"]
)
def test_get_dimension_is_exact_and_strips_only_one_minecraft_prefix(name):
    with pytest.raises(WorldLayoutError, match="unknown dimension"):
        get_dimension(name)


def test_unknown_dimension_message_lists_every_choice_and_hides_the_key_error():
    with pytest.raises(WorldLayoutError) as excinfo:
        get_dimension("the_aether")
    assert str(excinfo.value) == (
        "unknown dimension 'the_aether'; expected one of: overworld, the_nether, the_end"
    )
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True


def test_dimension_table_heights_and_legacy_dirs():
    assert [(d.name, d.legacy_region_dir, d.min_y, d.max_y) for d in DIMENSIONS.values()] == [
        ("overworld", "region", -64, 319),
        ("the_nether", "DIM-1/region", 0, 255),
        ("the_end", "DIM1/region", 0, 255),
    ]


@pytest.mark.parametrize("name", list(DIMENSIONS))
def test_modern_region_dir_is_built_from_the_namespaced_id(name):
    assert DIMENSIONS[name].modern_region_dir == MODERN[name]


def test_modern_region_dir_splits_on_the_first_colon_only():
    assert Dimension("x", "mod:deep/caves", "x/region", 0, 15).modern_region_dir == "dimensions/mod/deep/caves/region"
    assert Dimension("y", "a:b:c", "y/region", 0, 15).modern_region_dir == "dimensions/a/b:c/region"


def test_dimension_and_location_are_frozen(tmp_path):
    world = make_world(tmp_path / "w", [LEGACY["overworld"]])
    loc = locate(world)
    with pytest.raises(dataclasses.FrozenInstanceError):
        loc.layout = LAYOUT_26_1
    with pytest.raises(dataclasses.FrozenInstanceError):
        loc.dimension.min_y = 0


def test_locate_defaults_to_the_overworld_and_accepts_a_string_path(tmp_path):
    world = make_world(tmp_path / "w", LEGACY.values())
    loc = locate(str(world))
    assert loc == WorldLocation(world, DIMENSIONS["overworld"], world / "region", LAYOUT_PRE_26_1)
    assert isinstance(loc.world_dir, Path) and isinstance(loc.region_dir, Path)


def test_unknown_dimension_is_reported_before_the_folder_is_checked(tmp_path):
    with pytest.raises(WorldLayoutError, match="unknown dimension"):
        locate(tmp_path / "nope", "the_aether")


def test_missing_folder_message_names_the_path(tmp_path):
    missing = tmp_path / "nope"
    with pytest.raises(WorldLayoutError) as excinfo:
        locate(missing)
    assert str(excinfo.value) == f"world folder not found: {missing}"


def test_a_file_given_as_the_world_is_reported_as_not_found(tmp_path):
    not_a_folder = tmp_path / "level.dat"
    not_a_folder.write_bytes(b"\x1f\x8b")
    with pytest.raises(WorldLayoutError, match="world folder not found"):
        locate(not_a_folder)


def test_level_dat_that_is_a_directory_is_not_a_world(tmp_path):
    world = tmp_path / "w"
    (world / "level.dat").mkdir(parents=True)
    (world / "region").mkdir()
    with pytest.raises(WorldLayoutError) as excinfo:
        locate(world)
    assert str(excinfo.value) == f"not a Minecraft world folder: {world / 'level.dat'} does not exist"


def test_a_file_at_the_modern_region_path_falls_back_to_the_legacy_dir(tmp_path):
    world = make_world(tmp_path / "w", [LEGACY["overworld"], "dimensions/minecraft/overworld"])
    (world / MODERN["overworld"]).write_bytes(b"")
    loc = locate(world)
    assert loc.layout == LAYOUT_PRE_26_1
    assert loc.region_dir == world / LEGACY["overworld"]


def test_region_paths_that_are_files_are_not_region_directories(tmp_path):
    world = make_world(tmp_path / "w", ["DIM1", "dimensions/minecraft/the_end"])
    (world / LEGACY["the_end"]).write_bytes(b"")
    (world / MODERN["the_end"]).write_bytes(b"")
    with pytest.raises(WorldLayoutError) as excinfo:
        locate(world, "the_end")
    assert str(excinfo.value) == (
        f"no region directory for minecraft:the_end in {world}: "
        f"looked for {world / MODERN['the_end']} and {world / LEGACY['the_end']}"
    )


def test_an_empty_region_directory_is_located(tmp_path):
    """Only the directory's existence is checked; region files are not looked for."""
    world = make_world(tmp_path / "w", [MODERN["the_nether"]])
    loc = locate(world, "the_nether")
    assert loc.region_dir == world / MODERN["the_nether"]
    assert list(loc.region_dir.iterdir()) == []


def test_another_dimensions_region_dir_does_not_satisfy_the_lookup(tmp_path):
    world = make_world(tmp_path / "w", [LEGACY["overworld"], MODERN["the_end"]])
    with pytest.raises(WorldLayoutError, match="no region directory for minecraft:the_nether"):
        locate(world, "the_nether")
