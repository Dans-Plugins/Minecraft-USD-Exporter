"""Chunk decoding: packed indices, palette shapes (1.21.x and 26.x), section ranges."""

import numpy as np
import pytest

from mcusd.world.chunk import (
    ChunkFormatError,
    bits_for_palette,
    decode_chunk,
    pack_indices,
    unpack_indices,
)
from mcusd.world.region import parse_nbt
from tests.nbt_builders import chunk_nbt, section_nbt, to_bytes, volume_chunk_bytes

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
