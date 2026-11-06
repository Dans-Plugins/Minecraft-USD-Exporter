"""Block catalogue: prim-safe names, colours, occlusion flags and overrides."""

import colorsys
import json

import pytest
from pxr import Sdf

from mcusd import blocks
from mcusd.blocks import BlockCatalogue, BlockIdTable, prim_name, split_id

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


def test_leaf_litter_has_a_builtin_brown_not_a_hashed_colour():
    # Leaf litter carpets forest floors in 1.21.5+ worlds; without a built-in colour it
    # got a hashed (often blue) one, which read as water in the preview renders.
    color = BlockCatalogue().info("minecraft:leaf_litter").color
    assert color == blocks.parse_color("#9a7440")
    assert color != blocks.hashed_color("minecraft:leaf_litter")
    red, green, blue = color
    assert red > green > blue  # brown: warm, not blue
    assert BlockCatalogue().info("minecraft:leaf_litter").opaque is False

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


def test_block_id_table_starts_with_air_and_rejects_unknown_indices():
    table = BlockIdTable()
    assert table.index("minecraft:air") == 0
    assert table.ids == ["minecraft:air"] and len(table) == 1
    with pytest.raises(IndexError):
        table[1]


@pytest.mark.parametrize(
    "block_id, expected",
    [
        ("minecraft:stone", ("minecraft", "stone")),
        ("mymod:stone", ("mymod", "stone")),
        ("stone", ("minecraft", "stone")),  # no namespace means minecraft
        ("a:b:c", ("a", "b:c")),  # only the first colon separates
    ],
)
def test_split_id(block_id, expected):
    assert split_id(block_id) == expected


@pytest.mark.parametrize(
    "block_id, expected",
    [
        ("stone", "_stone"),  # a bare path is encoded even though its namespace defaults to minecraft
        ("minecraft:Stone", "_minecraft_3a_53tone"),  # uppercase is outside the bare form
        ("minecraft:9lives", "_minecraft_3a9lives"),  # so is a leading digit
        ("a_b:c", "_a_5fb_3ac"),  # "_" is escaped too, which keeps the code prefix-free
        ("mymod:é", "_mymod_3a_c3_a9"),  # escaping works on UTF-8 bytes
    ],
)
def test_encoded_prim_names_are_exact(block_id, expected):
    assert prim_name(block_id) == expected


@pytest.mark.parametrize("value", ["#336699", "336699", "  #336699\n", "##336699"])
def test_parse_color_accepts_surrounding_noise(value):
    assert blocks.parse_color(value) == (0x33 / 255, 0x66 / 255, 0x99 / 255)


def test_parse_color_is_case_insensitive():
    assert blocks.parse_color("#ABCDEF") == blocks.parse_color("#abcdef")


@pytest.mark.parametrize("value", ["", "#fff", "#12345", "#1234567", "#gggggg", "#12 456"])
def test_parse_color_rejects_anything_but_six_hex_digits(value):
    with pytest.raises(ValueError, match="rrggbb"):
        blocks.parse_color(value)


@pytest.mark.parametrize("key", ["", "mymod:stone", "minecraft:some_future_block", "1b7f0000-aaaa"])
def test_hashed_colours_stay_in_their_saturation_and_value_bands(key):
    _, saturation, value = colorsys.rgb_to_hsv(*blocks.hashed_color(key))
    assert 0.45 - 1e-9 <= saturation <= 0.80 + 1e-9
    assert 0.55 - 1e-9 <= value <= 0.90 + 1e-9


def test_hashed_colour_is_pinned():
    # The colour is derived from SHA-1, not hash(), so it is the same in every process.
    expected = (0.3553043829296425, 0.7339215686274511, 0.7294672487957121)
    assert blocks.hashed_color("mymod:stone") == pytest.approx(expected)


@pytest.mark.parametrize(
    "block_id, expected",
    [
        ("minecraft:red_sand", "#be6621"),  # the table wins over the red_ dye prefix
        ("minecraft:blue_ice", "#74a8fd"),  # likewise blue_
        ("minecraft:oak_leaves", "#4a7a24"),
        ("stone", "#7f7f7f"),  # no namespace means minecraft
        ("minecraft:blue_wool", "#35399d"),
        ("minecraft:light_blue_wool", "#3aafd9"),
        ("minecraft:light_gray_wool", "#8e8e86"),
        ("minecraft:red_nether_bricks", "#a12722"),  # a dye prefix wins over the _bricks suffix
        ("minecraft:oak_planks", "#a2834f"),
        ("minecraft:dark_oak_planks", "#432b14"),
        ("minecraft:pale_oak_planks", "#e3d9d6"),
        ("minecraft:pale_oak_leaves", "#4a7a24"),  # leaves are leaf green whatever the wood
        ("minecraft:diamond_ore", "#7f7f7f"),
        ("minecraft:flowering_azalea_leaves", "#4a7a24"),
        ("minecraft:nether_bricks", "#7a7a7a"),
        ("minecraft:cut_copper", "#c06c50"),
        ("minecraft:future_sapling", "#4a7a24"),
        ("minecraft:chorus_flower", "#c4271b"),
    ],
)
def test_builtin_colour_rules(block_id, expected):
    assert BlockCatalogue().info(block_id).color == blocks.parse_color(expected)


@pytest.mark.parametrize(
    "block_id",
    [
        "minecraft:some_future_block",
        "minecraft:blackstone_slab",  # a dye prefix needs its underscore
        "mymod:stone",  # the built-in tables apply to the minecraft namespace only
        "mymod:red_wool",
    ],
)
def test_ids_outside_the_builtin_rules_get_a_hashed_colour(block_id):
    assert BlockCatalogue().info(block_id).color == blocks.hashed_color(block_id)


def test_every_listed_opaque_block_is_opaque():
    cat = BlockCatalogue()
    assert all(cat.info(f"minecraft:{name}").opaque for name in blocks._OPAQUE)


def test_every_exception_to_the_suffix_rules_is_not_opaque():
    cat = BlockCatalogue()
    assert not any(cat.info(f"minecraft:{name}").opaque for name in blocks._NOT_OPAQUE)


@pytest.mark.parametrize(
    "block_id, opaque",
    [
        ("minecraft:deepslate_tiles", True),
        ("minecraft:white_glazed_terracotta", True),
        ("minecraft:red_concrete_powder", True),
        ("minecraft:white_wool", True),
        ("minecraft:warped_hyphae", True),
        ("minecraft:pale_oak_wood", True),
        ("minecraft:cut_copper", True),
        ("minecraft:waxed_copper_block", True),  # the prefix is dropped, then the allow-list applies
        ("minecraft:waxed_weathered_cut_copper", True),
        ("minecraft:infested_cobblestone", True),
        ("minecraft:infested_chiseled_stone_bricks", True),
        ("minecraft:waxed_oak_slab", False),
        ("minecraft:infested_pumpkin_stem", False),  # the exceptions apply after the prefix too
        ("stone", True),  # no namespace means minecraft
        ("mymod:oak_planks", False),  # suffix rules apply to the minecraft namespace only
        ("mymod:waxed_copper_block", False),
    ],
)
def test_suffix_and_prefix_opacity_rules(block_id, opaque):
    assert BlockCatalogue().info(block_id).opaque is opaque


def test_info_fields_and_cache():
    cat = BlockCatalogue()
    info = cat.info("minecraft:stone")
    assert (info.block_id, info.prim_name) == ("minecraft:stone", "stone")
    assert info.color == blocks.parse_color("#7f7f7f") and info.opaque is True
    assert cat.info("minecraft:stone") is info


def test_is_air_is_an_exact_match_on_namespaced_ids():
    cat = BlockCatalogue()
    assert all(cat.is_air(block_id) for block_id in blocks.AIR_IDS)
    assert not cat.is_air("minecraft:stone")
    assert not cat.is_air("air")  # unlike info(), no default namespace is applied
    assert not cat.is_air("mymod:air")


def test_opaque_override_can_turn_a_builtin_block_off_and_keeps_its_colour():
    info = BlockCatalogue({"minecraft:stone": {"opaque": False}}).info("minecraft:stone")
    assert info.opaque is False
    assert info.color == blocks.parse_color("#7f7f7f")


def test_colour_override_applies_to_air_but_opacity_does_not():
    info = BlockCatalogue({"minecraft:air": {"color": "#102030", "opaque": True}}).info("minecraft:air")
    assert info.color == blocks.parse_color("#102030")
    assert info.opaque is False


def test_override_keys_match_the_exact_id_string():
    cat = BlockCatalogue({"stone": {"color": "#ff0000"}})
    assert cat.info("stone").color == (1.0, 0.0, 0.0)
    assert cat.info("minecraft:stone").color == blocks.parse_color("#7f7f7f")


def test_empty_override_entry_changes_nothing():
    assert BlockCatalogue({"minecraft:stone": {}}).info("minecraft:stone") == BlockCatalogue().info("minecraft:stone")


def test_override_file_is_read_as_utf8(tmp_path):
    path = tmp_path / "o.json"
    path.write_text('{"mymod:é": {"color": "#ff0000"}}', encoding="utf-8")
    assert BlockCatalogue.from_file(path).info("mymod:é").color == (1.0, 0.0, 0.0)


def test_override_file_read_failures_are_oserror_and_valueerror(tmp_path):
    # The two families export_world turns into an input error.
    with pytest.raises(OSError):
        BlockCatalogue.from_file(tmp_path / "missing.json")
    path = tmp_path / "o.json"
    path.write_text("{not json")
    with pytest.raises(ValueError):
        BlockCatalogue.from_file(path)


@pytest.mark.parametrize("entry", [3, None, True, "red", "", [], ["color"]])
def test_non_object_override_entries_name_the_block_and_the_expected_shape(entry):
    with pytest.raises(ValueError, match="override for minecraft:stone: expected a JSON object"):
        BlockCatalogue({"minecraft:stone": entry})


@pytest.mark.parametrize("color", [123, None, True, ["#ff0000"], {}])
def test_non_string_override_colours_name_the_block(color):
    with pytest.raises(ValueError, match="override for minecraft:stone: 'color' must be a '#rrggbb' string"):
        BlockCatalogue({"minecraft:stone": {"color": color}})
