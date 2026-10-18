"""Helpers that build synthetic chunk NBT and worlds for tests (no game or server needed)."""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from nbt import nbt

from mcusd.world.chunk import bits_for_palette, pack_indices
from mcusd.world.region import write_region

# DataVersion of Java Edition 1.21.11 (any value >= 2844, the 21w43a chunk format, works).
DATA_VERSION = 4671
DATA_VERSION_26 = 5023  # Java Edition 26.3


def section_nbt(section_y: int, palette: list[str], indices: np.ndarray | None = None, *,
                style: str = "1.21", properties: dict[int, dict[str, str]] | None = None) -> nbt.TAG_Compound:
    """One chunk section.

    ``indices`` is a (16, 16, 16) [y, z, x] array into ``palette``; omit it for a
    single-entry palette. ``style`` picks the palette encoding:

    * ``"1.21"`` — ``{Name, Properties}`` compounds;
    * ``"26"`` — bare strings when no entry has properties, otherwise every entry a
      compound: ``{"": id}`` for property-less entries and ``{id, properties}`` else.
    """
    properties = properties or {}
    section = nbt.TAG_Compound()
    section.tags.append(nbt.TAG_Byte(name="Y", value=section_y))
    states = nbt.TAG_Compound(name="block_states")
    if style == "26" and not properties:
        pal = nbt.TAG_List(name="palette", type=nbt.TAG_String)
        for block_id in palette:
            pal.tags.append(nbt.TAG_String(value=block_id))
    else:
        pal = nbt.TAG_List(name="palette", type=nbt.TAG_Compound)
        for i, block_id in enumerate(palette):
            entry = nbt.TAG_Compound()
            props = properties.get(i)
            if style == "26":
                if props:
                    entry.tags.append(nbt.TAG_String(name="id", value=block_id))
                    entry.tags.append(_props("properties", props))
                else:
                    entry.tags.append(nbt.TAG_String(name="", value=block_id))
            else:
                entry.tags.append(nbt.TAG_String(name="Name", value=block_id))
                if props:
                    entry.tags.append(_props("Properties", props))
            pal.tags.append(entry)
    states.tags.append(pal)
    if len(palette) > 1:
        assert indices is not None
        data = nbt.TAG_Long_Array(name="data")
        data.value = pack_indices(np.asarray(indices).reshape(-1), bits_for_palette(len(palette)))
        states.tags.append(data)
    section.tags.append(states)
    return section


def _props(name: str, props: dict[str, str]) -> nbt.TAG_Compound:
    tag = nbt.TAG_Compound(name=name)
    for key, value in props.items():
        tag.tags.append(nbt.TAG_String(name=key, value=value))
    return tag


def chunk_nbt(chunk_x: int, chunk_z: int, *, data_version: int = DATA_VERSION, sections=(),
              status: str = "minecraft:full") -> nbt.NBTFile:
    """A post-21w43a chunk compound: DataVersion, xPos/zPos, Status, sections."""
    root = nbt.NBTFile()
    root.name = ""
    root.tags.append(nbt.TAG_Int(name="DataVersion", value=data_version))
    root.tags.append(nbt.TAG_Int(name="xPos", value=chunk_x))
    root.tags.append(nbt.TAG_Int(name="zPos", value=chunk_z))
    root.tags.append(nbt.TAG_Int(name="yPos", value=-4))
    root.tags.append(nbt.TAG_String(name="Status", value=status))
    section_list = nbt.TAG_List(name="sections", type=nbt.TAG_Compound)
    for section in sections:
        section_list.tags.append(section)
    root.tags.append(section_list)
    root.tags.append(nbt.TAG_List(name="block_entities", type=nbt.TAG_Compound))
    return root


def to_bytes(tag: nbt.NBTFile) -> bytes:
    buffer = io.BytesIO()
    tag.write_file(buffer=buffer)
    return buffer.getvalue()


def chunk_bytes(chunk_x: int, chunk_z: int, **kwargs) -> bytes:
    return to_bytes(chunk_nbt(chunk_x, chunk_z, **kwargs))


def volume_chunk_bytes(chunk_x: int, chunk_z: int, volume: np.ndarray, *, min_y: int = -64,
                       style: str = "1.21", status: str = "minecraft:full") -> bytes:
    """Encode a dense ``(height, 16, 16)`` array of block-ID strings as a chunk.

    All-air sections are omitted (they decode as air), as the game omits empty data.
    """
    sections = []
    for s in range(volume.shape[0] // 16):
        block = volume[s * 16 : (s + 1) * 16]
        palette, inverse = np.unique(block, return_inverse=True)
        palette = [str(p) for p in palette]
        if palette == ["minecraft:air"]:
            continue
        sections.append(section_nbt(min_y // 16 + s, palette, inverse.reshape(16, 16, 16), style=style))
    data_version = DATA_VERSION_26 if style == "26" else DATA_VERSION
    return chunk_bytes(chunk_x, chunk_z, sections=sections, data_version=data_version, status=status)


def write_world(root: Path, chunks: dict[tuple[int, int], np.ndarray], *, layout: str = "pre-26.1",
                style: str = "1.21", min_y: int = -64, partial: frozenset = frozenset()) -> Path:
    """Write an overworld containing ``chunks`` ((cx, cz) -> (height, 16, 16) ID array)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "level.dat").write_bytes(b"")
    region_dir = root / ("region" if layout == "pre-26.1" else "dimensions/minecraft/overworld/region")
    region_dir.mkdir(parents=True, exist_ok=True)
    by_region: dict[tuple[int, int], dict[tuple[int, int], bytes]] = {}
    for (cx, cz), volume in chunks.items():
        status = "minecraft:noise" if (cx, cz) in partial else "minecraft:full"
        by_region.setdefault((cx >> 5, cz >> 5), {})[(cx & 31, cz & 31)] = volume_chunk_bytes(
            cx, cz, volume, min_y=min_y, style=style, status=status)
    for (rx, rz), region_chunks in by_region.items():
        write_region(region_dir / f"r.{rx}.{rz}.mca", region_chunks)
    return root


def random_chunks(chunk_coords, rng: np.random.Generator, *, height: int = 384,
                  ids=("minecraft:stone", "minecraft:dirt", "minecraft:water", "minecraft:oak_log"),
                  air_fraction: float = 0.5) -> dict[tuple[int, int], np.ndarray]:
    """Random block-ID volumes for the given chunks (air everywhere above y=63)."""
    choices = np.array(list(ids) + ["minecraft:air"], dtype=object)
    weights = np.array([(1 - air_fraction) / len(ids)] * len(ids) + [air_fraction])
    out = {}
    for coord in chunk_coords:
        volume = rng.choice(choices, size=(height, 16, 16), p=weights)
        volume[128:] = "minecraft:air"  # keep the upper sections empty, like real terrain
        out[coord] = volume
    return out
