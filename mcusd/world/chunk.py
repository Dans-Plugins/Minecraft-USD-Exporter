"""Chunk decoder.

Wraps ``anvil-parser2``'s ``anvil.Chunk`` to turn a chunk's sections into dense
``numpy`` arrays of palette indices plus a per-chunk palette of block IDs.

``anvil.Chunk`` supplies the chunk envelope (``DataVersion``, ``xPos``/``zPos``,
the ``sections`` list). Palettes and packed block-state indices are decoded
here instead, for two reasons:

* speed — the indices are unpacked with vectorised ``numpy`` shifts rather than
  ``anvil``'s per-block Python loop;
* 26.x compatibility — worlds saved by 26.x write each ``block_states.palette``
  entry as a bare string ID (``"minecraft:stone"``) or as a compound with
  lowercase ``id``/``properties`` keys, and a list mixing the two wraps each
  string in a compound under an empty-named tag. 1.21.x writes
  ``{Name, Properties}`` compounds. ``anvil.Chunk.get_palette`` only understands
  the 1.21.x shape (it raises ``TypeError`` on 26.3 chunks), so all three shapes
  are handled here. Observed on worlds saved by vanilla 1.21.11 and 26.3 servers.

Block states (``properties``) are ignored in the MVP: blocks are keyed by
namespaced ID only.
"""

from __future__ import annotations

from dataclasses import dataclass

import anvil
import numpy as np
from anvil.versions import VERSION_20w17a, VERSION_21w43a
from nbt import nbt

AIR = "minecraft:air"
SECTION = 16
FULL_STATUS = "minecraft:full"


class ChunkFormatError(Exception):
    """A chunk's NBT does not have the structure this decoder supports."""


@dataclass
class DecodedChunk:
    chunk_x: int
    chunk_z: int
    min_y: int  # block Y of blocks[0]
    palette: list[str]  # namespaced block IDs; palette[0] is always minecraft:air
    blocks: np.ndarray  # uint16 palette indices, shape (height, 16, 16), indexed [y - min_y, z, x]
    status: str
    data_version: int

    @property
    def max_y(self) -> int:
        return self.min_y + self.blocks.shape[0] - 1

    @property
    def is_full(self) -> bool:
        """True when world generation finished (what the game itself would show)."""
        return self.status in (FULL_STATUS, "full")

    def block_at(self, x: int, y: int, z: int) -> str:
        """Block ID at absolute block coordinates inside this chunk."""
        lx, lz = x - self.chunk_x * SECTION, z - self.chunk_z * SECTION
        if not (0 <= lx < SECTION and 0 <= lz < SECTION and self.min_y <= y <= self.max_y):
            raise ValueError(f"({x}, {y}, {z}) is outside chunk ({self.chunk_x}, {self.chunk_z})")
        return self.palette[self.blocks[y - self.min_y, lz, lx]]


def palette_entry_id(entry) -> str:
    """Namespaced block ID of one ``block_states.palette`` entry (any known shape)."""
    if isinstance(entry, nbt.TAG_String):  # 26.x, property-less state
        return entry.value
    if isinstance(entry, nbt.TAG_Compound):
        for key in ("Name", "id", ""):  # 1.21.x / 26.x with properties / 26.x wrapped string
            if key in entry:
                return entry[key].value
    raise ChunkFormatError(f"unrecognised block palette entry: {entry!r}")


def unpack_indices(longs, bits: int, count: int = SECTION ** 3) -> np.ndarray:
    """Unpack ``count`` ``bits``-wide indices from a packed long array.

    Uses the non-spanning layout of DataVersion >= 2529 (20w17a): each 64-bit long
    holds ``64 // bits`` values from the least-significant end, and values never
    cross a long boundary.
    """
    # Importing anvil patches nbt.TAG_Long_Array to parse *unsigned* longs, so
    # accept either signedness.
    try:
        words = np.asarray(longs, dtype=np.uint64)
    except OverflowError:
        words = np.asarray(longs, dtype=np.int64).view(np.uint64)
    per_long = 64 // bits
    needed = -(-count // per_long)
    if words.size < needed:
        raise ChunkFormatError(
            f"packed block array too short: {words.size} longs for {count} {bits}-bit values"
        )
    shifts = (np.arange(per_long, dtype=np.uint64) * np.uint64(bits))
    mask = np.uint64((1 << bits) - 1)
    values = (words[:needed, None] >> shifts[None, :]) & mask
    return values.reshape(-1)[:count].astype(np.uint16)


def pack_indices(indices: np.ndarray, bits: int) -> list[int]:
    """Inverse of :func:`unpack_indices`.

    Returns unsigned 64-bit values, matching the unsigned ``TAG_Long_Array``
    format that importing ``anvil`` installs on the ``nbt`` library.
    """
    flat = np.asarray(indices, dtype=np.uint64).reshape(-1)
    per_long = 64 // bits
    pad = (-flat.size) % per_long
    flat = np.concatenate([flat, np.zeros(pad, dtype=np.uint64)]).reshape(-1, per_long)
    shifts = np.arange(per_long, dtype=np.uint64) * np.uint64(bits)
    words = np.bitwise_or.reduce(flat << shifts[None, :], axis=1)
    return [int(v) for v in words]


def bits_for_palette(size: int) -> int:
    """Index width the game uses for a block palette of ``size`` entries."""
    return max(4, (size - 1).bit_length())


def decode_chunk(nbt_data: nbt.NBTFile, min_y: int = -64, max_y: int = 319) -> DecodedChunk:
    """Decode a chunk into a dense palette-index volume covering ``min_y..max_y``.

    Sections outside that range (e.g. the lighting-only sections at Y -5 and 20)
    are ignored; sections missing from the chunk are air.
    """
    data_version = nbt_data["DataVersion"].value if "DataVersion" in nbt_data else -1
    if data_version < VERSION_21w43a:
        raise ChunkFormatError(
            f"chunk has DataVersion {data_version}; worlds older than the 1.18 chunk "
            f"format (DataVersion {VERSION_21w43a}) are not supported"
        )
    assert VERSION_21w43a >= VERSION_20w17a  # so values never span longs (unpack_indices)
    chunk = anvil.Chunk(nbt_data)
    if min_y % SECTION or (max_y + 1) % SECTION:
        raise ValueError("min_y and max_y + 1 must be multiples of 16")
    height = max_y - min_y + 1
    palette: list[str] = [AIR]
    lookup: dict[str, int] = {AIR: 0}
    blocks = np.zeros((height, SECTION, SECTION), dtype=np.uint16)

    sections = chunk.data["sections"] if "sections" in chunk.data else []
    for section in sections:
        section_y = section["Y"].value
        base = section_y * SECTION - min_y
        if base < 0 or base + SECTION > height or "block_states" not in section:
            continue
        states = section["block_states"]
        local_ids = [palette_entry_id(entry) for entry in states["palette"]]
        lut = np.empty(len(local_ids), dtype=np.uint16)
        for i, block_id in enumerate(local_ids):
            if block_id not in lookup:
                lookup[block_id] = len(palette)
                palette.append(block_id)
            lut[i] = lookup[block_id]
        if "data" not in states or len(local_ids) == 1:
            blocks[base : base + SECTION] = lut[0]
            continue
        indices = unpack_indices(states["data"].value, bits_for_palette(len(local_ids)))
        if indices.max(initial=0) >= len(local_ids):
            raise ChunkFormatError(
                f"chunk ({chunk.x}, {chunk.z}) section {section_y}: palette index out of range"
            )
        blocks[base : base + SECTION] = lut[indices].reshape(SECTION, SECTION, SECTION)

    status = chunk.data["Status"].value if "Status" in chunk.data else ""
    return DecodedChunk(chunk.x, chunk.z, min_y, palette, blocks, status, chunk.version)
