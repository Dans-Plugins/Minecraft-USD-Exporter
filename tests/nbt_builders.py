"""Helpers that build synthetic chunk NBT for tests (no game or server needed)."""

from __future__ import annotations

import io

from nbt import nbt

# DataVersion of Java Edition 1.21.11 (any value >= 2844, the 21w43a chunk format, works).
DATA_VERSION = 4671


def chunk_nbt(chunk_x: int, chunk_z: int, *, data_version: int = DATA_VERSION, sections=()) -> nbt.NBTFile:
    """A minimal post-21w43a chunk compound: DataVersion, xPos/zPos, sections."""
    root = nbt.NBTFile()
    root.name = ""
    root.tags.append(nbt.TAG_Int(name="DataVersion", value=data_version))
    root.tags.append(nbt.TAG_Int(name="xPos", value=chunk_x))
    root.tags.append(nbt.TAG_Int(name="zPos", value=chunk_z))
    root.tags.append(nbt.TAG_Int(name="yPos", value=-4))
    root.tags.append(nbt.TAG_String(name="Status", value="minecraft:full"))
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
