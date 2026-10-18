"""Selection.

Converts a block-space bounding box into the set of region files and chunks to
read, and clips decoded volumes to the box.

Coordinates are Minecraft block coordinates; boxes are inclusive on both ends.
Chunk and region indices use floor division (``x >> 4``, ``x >> 9``), which is
correct for negative coordinates.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from mcusd.blocks import BlockIdTable
from mcusd.world.chunk import SECTION, decode_chunk
from mcusd.world.layout import Dimension
from mcusd.world.region import CHUNKS_PER_SIDE, RegionFile, region_filename

REGION_BLOCKS = SECTION * CHUNKS_PER_SIDE  # 512


@dataclass(frozen=True)
class BBox:
    """Inclusive block-space box with ``x1 <= x2``, ``y1 <= y2``, ``z1 <= z2``."""

    x1: int
    y1: int
    z1: int
    x2: int
    y2: int
    z2: int

    def __post_init__(self):
        if not (self.x1 <= self.x2 and self.y1 <= self.y2 and self.z1 <= self.z2):
            raise ValueError(f"degenerate bounding box {self}")

    @classmethod
    def from_corners(cls, x1: int, y1: int, z1: int, x2: int, y2: int, z2: int) -> "BBox":
        """Build a box from any two opposite corners."""
        return cls(min(x1, x2), min(y1, y2), min(z1, z2), max(x1, x2), max(y1, y2), max(z1, z2))

    def as_list(self) -> list[int]:
        return [self.x1, self.y1, self.z1, self.x2, self.y2, self.z2]

    def contains(self, x: int, y: int, z: int) -> bool:
        return self.x1 <= x <= self.x2 and self.y1 <= y <= self.y2 and self.z1 <= z <= self.z2

    def intersect(self, other: "BBox") -> "BBox | None":
        lo = (max(self.x1, other.x1), max(self.y1, other.y1), max(self.z1, other.z1))
        hi = (min(self.x2, other.x2), min(self.y2, other.y2), min(self.z2, other.z2))
        if lo[0] > hi[0] or lo[1] > hi[1] or lo[2] > hi[2]:
            return None
        return BBox(*lo, *hi)

    def expand(self, n: int) -> "BBox":
        return BBox(self.x1 - n, self.y1 - n, self.z1 - n, self.x2 + n, self.y2 + n, self.z2 + n)

    @property
    def shape(self) -> tuple[int, int, int]:
        """Array shape ``(Y, Z, X)`` of a dense volume covering the box."""
        return (self.y2 - self.y1 + 1, self.z2 - self.z1 + 1, self.x2 - self.x1 + 1)

    @property
    def volume(self) -> int:
        ny, nz, nx = self.shape
        return nx * ny * nz


def region_bounds(region_x: int, region_z: int, min_y: int, max_y: int) -> BBox:
    x0, z0 = region_x * REGION_BLOCKS, region_z * REGION_BLOCKS
    return BBox(x0, min_y, z0, x0 + REGION_BLOCKS - 1, max_y, z0 + REGION_BLOCKS - 1)


def chunks_by_region(bbox: BBox) -> "OrderedDict[tuple[int, int], list[tuple[int, int]]]":
    """Map each region ``(rx, rz)`` touched by ``bbox`` to its chunks ``(cx, cz)``.

    Regions and chunks are ordered by Z then X.
    """
    out: OrderedDict[tuple[int, int], list[tuple[int, int]]] = OrderedDict()
    for cz in range(bbox.z1 >> 4, (bbox.z2 >> 4) + 1):
        for cx in range(bbox.x1 >> 4, (bbox.x2 >> 4) + 1):
            out.setdefault((cx >> 5, cz >> 5), []).append((cx, cz))
    return OrderedDict(sorted(out.items(), key=lambda kv: (kv[0][1], kv[0][0])))


class ChunkSource:
    """Lazily opens region files and decodes chunks into layer-wide block indices.

    Decoded chunks are kept in a small LRU cache so that the one-block halo read
    from a neighbouring chunk does not decode it again for every region.
    Chunks whose generation is unfinished (``Status`` other than ``full``) are
    treated as not generated, matching what the game shows.
    """

    def __init__(self, region_dir: str | Path, dimension: Dimension, table: BlockIdTable | None = None,
                 *, chunk_cache: int = 256, region_cache: int = 4):
        self.region_dir = Path(region_dir)
        self.dimension = dimension
        self.table = table if table is not None else BlockIdTable()
        self._chunk_cache_size = chunk_cache
        self._region_cache_size = region_cache
        self._chunks: OrderedDict[tuple[int, int], np.ndarray | None] = OrderedDict()
        self._regions: OrderedDict[tuple[int, int], RegionFile | None] = OrderedDict()
        self.chunks_decoded = 0
        self.chunks_skipped_partial = 0

    def _region(self, region_x: int, region_z: int) -> RegionFile | None:
        key = (region_x, region_z)
        if key in self._regions:
            self._regions.move_to_end(key)
            return self._regions[key]
        path = self.region_dir / region_filename(region_x, region_z)
        region = RegionFile(path) if path.is_file() else None
        self._regions[key] = region
        if len(self._regions) > self._region_cache_size:
            self._regions.popitem(last=False)
        return region

    def chunk(self, chunk_x: int, chunk_z: int) -> np.ndarray | None:
        """Block-index volume ``(height, 16, 16)`` of a chunk, or ``None`` if absent."""
        key = (chunk_x, chunk_z)
        if key in self._chunks:
            self._chunks.move_to_end(key)
            return self._chunks[key]
        result = None
        region = self._region(chunk_x >> 5, chunk_z >> 5)
        if region is not None:
            nbt_data = region.read_nbt(chunk_x & 31, chunk_z & 31)
            if nbt_data is not None:
                decoded = decode_chunk(nbt_data, self.dimension.min_y, self.dimension.max_y)
                self.chunks_decoded += 1
                if decoded.is_full:
                    lut = np.array([self.table.index(b) for b in decoded.palette], dtype=np.uint16)
                    result = lut[decoded.blocks]
                else:
                    self.chunks_skipped_partial += 1
        self._chunks[key] = result
        if len(self._chunks) > self._chunk_cache_size:
            self._chunks.popitem(last=False)
        return result

    def fill(self, box: BBox, out: np.ndarray, out_origin: tuple[int, int, int]) -> int:
        """Copy every block of ``box`` into ``out`` (indexed from ``out_origin``).

        Returns the number of chunks that were present. Absent chunks are left as
        whatever ``out`` already holds (air, normally).
        """
        ox, oy, oz = out_origin
        dim_min = self.dimension.min_y
        present = 0
        for cz in range(box.z1 >> 4, (box.z2 >> 4) + 1):
            for cx in range(box.x1 >> 4, (box.x2 >> 4) + 1):
                volume = self.chunk(cx, cz)
                if volume is None:
                    continue
                present += 1
                x1, x2 = max(box.x1, cx * 16), min(box.x2, cx * 16 + 15)
                z1, z2 = max(box.z1, cz * 16), min(box.z2, cz * 16 + 15)
                out[box.y1 - oy : box.y2 - oy + 1, z1 - oz : z2 - oz + 1, x1 - ox : x2 - ox + 1] = volume[
                    box.y1 - dim_min : box.y2 - dim_min + 1,
                    z1 - cz * 16 : z2 - cz * 16 + 1,
                    x1 - cx * 16 : x2 - cx * 16 + 1,
                ]
        return present


@dataclass
class RegionVolume:
    """Dense block-index volume for the part of a selection inside one region.

    ``ids`` covers ``core`` expanded by ``halo`` blocks on every side. Halo cells are
    filled from neighbouring chunks only where they lie inside the selection;
    everything else (outside the selection, outside the dimension, not generated)
    is air (index 0), i.e. non-occluding.
    """

    region_x: int
    region_z: int
    core: BBox  # selection ∩ region ∩ dimension height
    halo: int
    ids: np.ndarray  # uint16, shape core.expand(halo).shape, indexed [y, z, x]
    chunks_present: int = 0
    table: BlockIdTable = field(default_factory=BlockIdTable)

    @property
    def origin(self) -> tuple[int, int, int]:
        """Absolute block coordinates ``(x, y, z)`` of ``ids[0, 0, 0]``."""
        return (self.core.x1 - self.halo, self.core.y1 - self.halo, self.core.z1 - self.halo)

    @property
    def interior(self) -> tuple[slice, slice, slice]:
        h = self.halo
        ny, nz, nx = self.core.shape
        return (slice(h, h + ny), slice(h, h + nz), slice(h, h + nx))

    def interior_ids(self) -> np.ndarray:
        return self.ids[self.interior]

    def block_at(self, x: int, y: int, z: int) -> str:
        ox, oy, oz = self.origin
        return self.table[int(self.ids[y - oy, z - oz, x - ox])]


def clamp_to_dimension(bbox: BBox, dimension: Dimension) -> BBox | None:
    return bbox.intersect(BBox(bbox.x1, dimension.min_y, bbox.z1, bbox.x2, dimension.max_y, bbox.z2))


def region_volume(source: ChunkSource, bbox: BBox, region_x: int, region_z: int, halo: int = 1) -> RegionVolume | None:
    """Read the part of ``bbox`` inside region ``(region_x, region_z)``, plus a halo."""
    dim = source.dimension
    selection = clamp_to_dimension(bbox, dim)
    if selection is None:
        return None
    core = selection.intersect(region_bounds(region_x, region_z, dim.min_y, dim.max_y))
    if core is None:
        return None
    extended = core.expand(halo)
    ids = np.zeros(extended.shape, dtype=np.uint16)
    to_fill = extended.intersect(selection)
    assert to_fill is not None
    present = source.fill(to_fill, ids, (extended.x1, extended.y1, extended.z1))
    return RegionVolume(region_x, region_z, core, halo, ids, present, source.table)


def iter_region_volumes(source: ChunkSource, bbox: BBox, halo: int = 1) -> Iterator[RegionVolume]:
    """Yield one :class:`RegionVolume` per region touched by ``bbox``, in Z-then-X order."""
    for region_x, region_z in chunks_by_region(bbox):
        volume = region_volume(source, bbox, region_x, region_z, halo)
        if volume is not None:
            yield volume
