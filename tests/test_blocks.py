"""Block catalogue: prim-safe names, colours, occlusion flags and overrides."""

import json

import pytest
from pxr import Sdf

from mcusd import blocks
from mcusd.blocks import BlockCatalogue, BlockIdTable, prim_name

TRICKY_IDS = [
    "minecraft:stone",
    "minecraft:oak_log",
    "minecraft:a_b",
    "minecraft:a.b",
    "minecraft:a-b",
    "minecraft:a/b",
    "mymod:stone",
    "my-mod:stone",
    "mymod:Stone",
    "minecraft:mymod__stone",
    "minecraft:mymod__stone_7f308680",  # once collided with mymod:stone (review on #11)
    "minecraft:_mymod_3astone",
    "_mymod_3astone",
    "stone",
    "minecraft:Stone",
    "3rdparty:block",
    "minecraft:9lives",
    "create:andesite_casing",
    "x:y.z/w-v",
]


def all_builtin_ids():
    names = set(blocks._COLORS) | set(blocks._OPAQUE)
    return sorted(f"minecraft:{n}" for n in names)


@pytest.mark.parametrize("block_id", TRICKY_IDS)
def test_prim_names_are_valid_identifiers(block_id):
    assert Sdf.Path.IsValidIdentifier(prim_name(block_id)), prim_name(block_id)


def test_every_builtin_name_is_valid_and_distinct():
    ids = all_builtin_ids() + TRICKY_IDS
    names = [prim_name(b) for b in ids]
    assert all(Sdf.Path.IsValidIdentifier(n) for n in names)
    assert len(set(names)) == len(set(ids))


def test_colliding_pair_from_review_gets_distinct_names():
    cat = BlockCatalogue()
    a = cat.info("mymod:stone").prim_name
    b = cat.info("minecraft:mymod__stone_7f308680").prim_name
    assert a != b
    assert a == "_mymod_3astone"


def test_encoded_names_never_look_bare():
    for block_id in TRICKY_IDS:
        name = prim_name(block_id)
        assert name.startswith("_") or block_id == f"minecraft:{name}"


def test_vanilla_names_are_bare_and_stable():
    assert prim_name("minecraft:grass_block") == "grass_block"
    assert prim_name("mymod:stone") == prim_name("mymod:stone")
    assert prim_name("mymod:stone") != prim_name("minecraft:stone")


def test_colours_are_in_unit_range_and_deterministic():
    cat = BlockCatalogue()
    for block_id in all_builtin_ids() + TRICKY_IDS + ["minecraft:red_wool", "minecraft:spruce_planks"]:
        color = cat.info(block_id).color
        assert len(color) == 3 and all(0.0 <= c <= 1.0 for c in color)
    assert BlockCatalogue().info("mymod:thing").color == BlockCatalogue().info("mymod:thing").color
    assert cat.info("minecraft:light_blue_wool").color != cat.info("minecraft:blue_wool").color


@pytest.mark.parametrize(
    "block_id, opaque",
    [
        ("minecraft:stone", True),
        ("minecraft:grass_block", True),
        ("minecraft:oak_planks", True),
        ("minecraft:deepslate_diamond_ore", True),
        ("minecraft:red_concrete", True),
        ("minecraft:stripped_birch_log", True),
        ("minecraft:waxed_cut_copper", True),
        ("minecraft:infested_stone", True),
        ("minecraft:air", False),
        ("minecraft:cave_air", False),
        ("minecraft:void_air", False),
        ("minecraft:water", False),
        ("minecraft:lava", False),
        ("minecraft:glass", False),
        ("minecraft:oak_leaves", False),
        ("minecraft:oak_slab", False),
        ("minecraft:stone_brick_stairs", False),
        ("minecraft:short_grass", False),
        ("minecraft:pumpkin_stem", False),
        ("minecraft:big_dripleaf_stem", False),
        ("minecraft:crimson_stem", True),
        ("minecraft:chiseled_bookshelf", False),
        ("mymod:stone", False),  # unknown namespaces never occlude
        ("minecraft:some_future_block", False),
    ],
)
def test_opaque_flags(block_id, opaque):
    assert BlockCatalogue().info(block_id).opaque is opaque


def test_overrides_from_file(tmp_path):
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps({
        "minecraft:stone": {"color": "#ff0000"},
        "mymod:solid": {"opaque": True, "color": "00ff00"},
        "minecraft:air": {"opaque": True},
    }))
    cat = BlockCatalogue.from_file(path)
    assert cat.info("minecraft:stone").color == (1.0, 0.0, 0.0)
    assert cat.info("minecraft:stone").opaque is True
    assert cat.info("mymod:solid").opaque is True
    assert cat.info("mymod:solid").color == (0.0, 1.0, 0.0)
    assert cat.info("minecraft:air").opaque is False  # air is never an occluder


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"minecraft:stone": {"colour": "#fff"}}, "unknown keys"),
        ({"minecraft:stone": {"color": "red"}}, "rrggbb"),
        ({"minecraft:stone": {"opaque": "yes"}}, "true or false"),
    ],
)
def test_invalid_overrides(overrides, message):
    with pytest.raises(ValueError, match=message):
        BlockCatalogue(overrides)


def test_override_file_must_be_object(tmp_path):
    path = tmp_path / "o.json"
    path.write_text("[]")
    with pytest.raises(ValueError, match="JSON object"):
        BlockCatalogue.from_file(path)
    assert isinstance(BlockCatalogue.from_file(None), BlockCatalogue)


def test_block_id_table():
    table = BlockIdTable()
    assert table[0] == "minecraft:air"
    assert table.index("minecraft:stone") == 1
    assert table.index("minecraft:dirt") == 2
    assert table.index("minecraft:stone") == 1
    assert len(table) == 3
