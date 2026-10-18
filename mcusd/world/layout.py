"""World locator.

Given a world folder and a dimension, finds the region directory. Handles the
pre-26.1 layout (``region/``, ``DIM-1/region/``, ``DIM1/region/``) and the 26.1+
layout (``dimensions/minecraft/<dimension>/region/``), per the
`Java Edition level format <https://minecraft.wiki/w/Java_Edition_level_format>`_
article. The 26.1+ layout was confirmed against a world saved by a vanilla 26.3
server.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

LAYOUT_PRE_26_1 = "pre-26.1"
LAYOUT_26_1 = "26.1+"


@dataclass(frozen=True)
class Dimension:
    """A vanilla dimension and where each layout keeps its region files."""

    name: str  # short name accepted on the command line, e.g. "overworld"
    namespaced_id: str  # e.g. "minecraft:overworld"
    legacy_region_dir: str  # relative to the world folder, pre-26.1
    min_y: int  # lowest block Y of the dimension
    max_y: int  # highest block Y of the dimension (inclusive)

    @property
    def modern_region_dir(self) -> str:
        namespace, path = self.namespaced_id.split(":", 1)
        return f"dimensions/{namespace}/{path}/region"


DIMENSIONS: dict[str, Dimension] = {
    "overworld": Dimension("overworld", "minecraft:overworld", "region", -64, 319),
    "the_nether": Dimension("the_nether", "minecraft:the_nether", "DIM-1/region", 0, 255),
    "the_end": Dimension("the_end", "minecraft:the_end", "DIM1/region", 0, 255),
}


class WorldLayoutError(Exception):
    """The folder is not a world, or the requested dimension has no region files."""


@dataclass(frozen=True)
class WorldLocation:
    world_dir: Path
    dimension: Dimension
    region_dir: Path
    layout: str  # LAYOUT_PRE_26_1 or LAYOUT_26_1


def get_dimension(name: str) -> Dimension:
    """Look up a dimension by short name (``overworld``) or namespaced ID."""
    key = name.removeprefix("minecraft:")
    try:
        return DIMENSIONS[key]
    except KeyError:
        choices = ", ".join(DIMENSIONS)
        raise WorldLayoutError(
            f"unknown dimension {name!r}; expected one of: {choices}"
        ) from None


def locate(world_dir: str | Path, dimension: str = "overworld") -> WorldLocation:
    """Find the region directory for ``dimension`` in ``world_dir``.

    The 26.1+ location is preferred when both exist (a world upgraded from an
    older version may keep a stale legacy folder).

    Raises :class:`WorldLayoutError` naming the missing path(s) when the folder is
    not a world (no ``level.dat``) or the dimension has no region directory.
    """
    world = Path(world_dir)
    dim = get_dimension(dimension)
    if not world.is_dir():
        raise WorldLayoutError(f"world folder not found: {world}")
    level_dat = world / "level.dat"
    if not level_dat.is_file():
        raise WorldLayoutError(
            f"not a Minecraft world folder: {level_dat} does not exist"
        )
    modern = world / dim.modern_region_dir
    legacy = world / dim.legacy_region_dir
    if modern.is_dir():
        return WorldLocation(world, dim, modern, LAYOUT_26_1)
    if legacy.is_dir():
        return WorldLocation(world, dim, legacy, LAYOUT_PRE_26_1)
    raise WorldLayoutError(
        f"no region directory for {dim.namespaced_id} in {world}: "
        f"looked for {modern} and {legacy}"
    )
