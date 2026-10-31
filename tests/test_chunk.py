"""Chunk decoding: packed indices, palette shapes (1.21.x and 26.x), section ranges."""

import numpy as np
import pytest
from nbt import nbt

from mcusd.world.chunk import (
    ChunkFormatError,
    bits_for_palette,
    decode_chunk,
    pack_indices,
    palette_entry_id,
    unpack_indices,
)
from mcusd.world.region import parse_nbt
from tests.nbt_builders import DATA_VERSION, chunk_nbt, section_nbt, to_bytes, volume_chunk_bytes

PALETTE = ["minecraft:air", "minecraft:stone", "minecraft:glow_lichen", "minecraft:dirt"]


@pytest.mark.parametrize("bits", [4, 5, 6, 7, 8, 12, 15])
def test_pack_unpack_round_trip(bits):
    rng = np.random.default_rng(bits)
    values = rng.integers(0, 2**bits, size=4096)
    assert np.array_equal(unpack_indices(pack_indices(values, bits), bits), values)


def test_unpack_accepts_signed_longs():
    # an all-ones long is -1 when read signed and 2**64 - 1 when read unsigned
    assert np.all(unpack_indices([-1] * 256, 4) == 15)
    assert np.all(unpack_indices([2**64 - 1] * 256, 4) == 15)


def test_unpack_rejects_short_array():
    with pytest.raises(ChunkFormatError, match="too short"):
        unpack_indices([0] * 10, 4)


def test_bits_for_palette():
    assert bits_for_palette(2) == 4
    assert bits_for_palette(16) == 4
    assert bits_for_palette(17) == 5
    assert bits_for_palette(300) == 9


def _indices(seed=0):
    return np.random.default_rng(seed).integers(0, len(PALETTE), size=(16, 16, 16))


@pytest.mark.parametrize(
    "style, properties",
    [
        ("1.21", None),
        ("1.21", {2: {"up": "true"}}),
        ("26", None),  # every entry a bare string
        ("26", {2: {"up": "true"}}),  # mixed: {"": id} wrappers plus {id, properties}
    ],
)
def test_palette_shapes_decode_identically(style, properties):
    indices = _indices()
    section = section_nbt(0, PALETTE, indices, style=style, properties=properties)
    decoded = decode_chunk(parse_nbt(to_bytes(chunk_nbt(3, -2, sections=[section]))))
    assert (decoded.chunk_x, decoded.chunk_z) == (3, -2)
    got = np.array(decoded.palette, dtype=object)[decoded.blocks[64:80]]
    expected = np.array(PALETTE, dtype=object)[indices]
    assert np.array_equal(got, expected)


def test_block_at_uses_absolute_coordinates():
    volume = np.full((384, 16, 16), "minecraft:air", dtype=object)
    volume[64 + 5, 7, 2] = "minecraft:gold_block"  # y=5, local z=7, local x=2
    decoded = decode_chunk(parse_nbt(volume_chunk_bytes(-1, -1, volume)))
    assert decoded.block_at(-16 + 2, 5, -16 + 7) == "minecraft:gold_block"
    assert decoded.block_at(-16, 5, -16) == "minecraft:air"
    with pytest.raises(ValueError):
        decoded.block_at(0, 5, 0)


def test_missing_sections_are_air_and_single_palette_has_no_data():
    section = section_nbt(-4, ["minecraft:bedrock"])
    decoded = decode_chunk(parse_nbt(to_bytes(chunk_nbt(0, 0, sections=[section]))))
    assert decoded.blocks.shape == (384, 16, 16)
    assert set(np.array(decoded.palette)[np.unique(decoded.blocks[:16])]) == {"minecraft:bedrock"}
    assert np.all(decoded.blocks[16:] == 0)
    assert decoded.palette[0] == "minecraft:air"


def test_sections_outside_dimension_range_are_ignored():
    sections = [section_nbt(-5, ["minecraft:stone"]), section_nbt(20, ["minecraft:stone"])]
    decoded = decode_chunk(parse_nbt(to_bytes(chunk_nbt(0, 0, sections=sections))))
    assert not decoded.blocks.any()


def test_nether_height_range():
    sections = [section_nbt(0, ["minecraft:netherrack"]), section_nbt(15, ["minecraft:bedrock"])]
    decoded = decode_chunk(parse_nbt(to_bytes(chunk_nbt(0, 0, sections=sections))), 0, 255)
    assert decoded.blocks.shape == (256, 16, 16)
    assert decoded.block_at(0, 0, 0) == "minecraft:netherrack"
    assert decoded.block_at(0, 255, 0) == "minecraft:bedrock"


def test_pre_1_18_chunks_are_rejected():
    with pytest.raises(ChunkFormatError, match="DataVersion 2730"):
        decode_chunk(parse_nbt(to_bytes(chunk_nbt(0, 0, data_version=2730))))


def test_out_of_range_palette_index():
    indices = np.full((16, 16, 16), 3)
    section = section_nbt(0, ["minecraft:air", "minecraft:stone"], indices)
    with pytest.raises(ChunkFormatError, match="palette index out of range"):
        decode_chunk(parse_nbt(to_bytes(chunk_nbt(0, 0, sections=[section]))))


def test_status_reports_partial_chunks():
    full = decode_chunk(parse_nbt(to_bytes(chunk_nbt(0, 0))))
    partial = decode_chunk(parse_nbt(to_bytes(chunk_nbt(0, 0, status="minecraft:noise"))))
    assert full.is_full and not partial.is_full


def test_invalid_y_range():
    with pytest.raises(ValueError):
        decode_chunk(parse_nbt(to_bytes(chunk_nbt(0, 0))), -60, 319)


def _compound(name: str, value: str) -> nbt.TAG_Compound:
    entry = nbt.TAG_Compound()
    entry.tags.append(nbt.TAG_String(name=name, value=value))
    return entry


@pytest.mark.parametrize(
    "entry",
    [
        nbt.TAG_String(value="minecraft:stone"),  # 26.x bare string
        _compound("Name", "minecraft:stone"),  # 1.21.x
        _compound("id", "minecraft:stone"),  # 26.x with properties
        _compound("", "minecraft:stone"),  # 26.x string wrapped in a mixed list
    ],
)
def test_palette_entry_id_shapes(entry):
    assert palette_entry_id(entry) == "minecraft:stone"


@pytest.mark.parametrize("entry", [_compound("Block", "minecraft:stone"), nbt.TAG_Int(value=1)])
def test_palette_entry_id_rejects_unknown_shapes(entry):
    with pytest.raises(ChunkFormatError, match="unrecognised block palette entry"):
        palette_entry_id(entry)


def test_pack_indices_pads_the_last_long():
    # three 4-bit values fill the low 12 bits of one long; the rest is zero padding
    assert pack_indices(np.array([1, 2, 3]), 4) == [0x321]
    assert unpack_indices([0x321], 4, count=3).tolist() == [1, 2, 3]


def test_pack_indices_values_never_span_longs():
    # 5-bit values: 12 per long (60 bits), so the 13th starts the next long
    words = pack_indices(np.array([31] * 12 + [1]), 5)
    assert words == [2**60 - 1, 1]


def test_chunk_without_data_version_is_rejected():
    root = chunk_nbt(0, 0)
    del root["DataVersion"]
    with pytest.raises(ChunkFormatError, match="DataVersion -1"):
        decode_chunk(parse_nbt(to_bytes(root)))


def test_chunk_palette_merges_sections_in_first_seen_order():
    lower = section_nbt(0, ["minecraft:stone", "minecraft:dirt"], np.zeros((16, 16, 16), dtype=int))
    upper_indices = np.ones((16, 16, 16), dtype=int)
    upper_indices[0, 0, 0] = 0
    upper = section_nbt(1, ["minecraft:dirt", "minecraft:air"], upper_indices)
    decoded = decode_chunk(parse_nbt(to_bytes(chunk_nbt(0, 0, sections=[lower, upper]))))
    assert decoded.palette == ["minecraft:air", "minecraft:stone", "minecraft:dirt"]
    assert decoded.block_at(0, 0, 0) == "minecraft:stone"
    assert decoded.block_at(0, 16, 0) == "minecraft:dirt"
    assert decoded.block_at(1, 16, 0) == "minecraft:air"
    assert decoded.blocks[64 + 17, 0, 0] == 0  # a section's local air shares global index 0


def test_section_without_block_states_is_air():
    section = section_nbt(0, ["minecraft:stone"])
    del section["block_states"]
    decoded = decode_chunk(parse_nbt(to_bytes(chunk_nbt(0, 0, sections=[section]))))
    assert decoded.palette == ["minecraft:air"]
    assert not decoded.blocks.any()


def test_decoded_chunk_metadata():
    decoded = decode_chunk(parse_nbt(to_bytes(chunk_nbt(0, 0))))
    assert (decoded.min_y, decoded.max_y) == (-64, 319)
    assert decoded.data_version == DATA_VERSION
    assert decoded.status == "minecraft:full"
    nether = decode_chunk(parse_nbt(to_bytes(chunk_nbt(0, 0))), 0, 255)
    assert (nether.min_y, nether.max_y) == (0, 255)


def test_unprefixed_full_status_counts_as_full():
    assert decode_chunk(parse_nbt(to_bytes(chunk_nbt(0, 0, status="full")))).is_full
    assert not decode_chunk(parse_nbt(to_bytes(chunk_nbt(0, 0, status="")))).is_full


def test_block_at_rejects_y_outside_the_volume():
    decoded = decode_chunk(parse_nbt(to_bytes(chunk_nbt(0, 0))))
    assert decoded.block_at(0, -64, 0) == "minecraft:air"
    assert decoded.block_at(0, 319, 0) == "minecraft:air"
    with pytest.raises(ValueError, match="outside chunk"):
        decoded.block_at(0, -65, 0)
    with pytest.raises(ValueError, match="outside chunk"):
        decoded.block_at(0, 320, 0)
