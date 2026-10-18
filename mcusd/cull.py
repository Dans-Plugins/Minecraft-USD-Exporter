"""Culler.

Removes air and blocks whose six neighbours are all opaque full cubes.

Works on a :class:`mcusd.select.RegionVolume`, whose one-block halo already holds
the neighbouring blocks that lie inside the selection (across chunk and region
boundaries) and air everywhere else, so neighbours outside the selection or not
generated count as non-occluding. The test is six shifted ``numpy`` boolean
comparisons, done in Y slabs to bound the size of temporary arrays; there is no
per-block Python loop.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from mcusd.blocks import AIR_IDS, BlockCatalogue, BlockIdTable
from mcusd.select import RegionVolume

SLAB = 64  # Y layers processed at a time


@dataclass
class CullResult:
    """Visible blocks of one region volume.

    ``positions`` are absolute block coordinates ``(x, y, z)`` (int32, shape (N, 3))
    ordered by Y, then Z, then X; ``block_ids`` are layer-wide :class:`BlockIdTable`
    indices (uint16, shape (N,)).
    """

    region_x: int
    region_z: int
    positions: np.ndarray
    block_ids: np.ndarray
    culled: int  # non-air blocks dropped because all six neighbours are opaque
    air: int  # air blocks dropped

    @property
    def kept(self) -> int:
        return int(self.block_ids.size)


def block_luts(table: BlockIdTable, catalogue: BlockCatalogue) -> tuple[np.ndarray, np.ndarray]:
    """Boolean lookup tables ``(opaque, air)`` indexed by table index."""
    opaque = np.array([catalogue.info(b).opaque for b in table.ids], dtype=bool)
    air = np.array([b in AIR_IDS for b in table.ids], dtype=bool)
    return opaque, air


def visible_mask(ids: np.ndarray, opaque_lut: np.ndarray, air_lut: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """For a volume with a one-block halo, return ``(keep, hidden)`` for its interior."""
    opaque = opaque_lut[ids]
    centre = opaque[1:-1, 1:-1, 1:-1]
    hidden = (
        centre
        & opaque[:-2, 1:-1, 1:-1]
        & opaque[2:, 1:-1, 1:-1]
        & opaque[1:-1, :-2, 1:-1]
        & opaque[1:-1, 2:, 1:-1]
        & opaque[1:-1, 1:-1, :-2]
        & opaque[1:-1, 1:-1, 2:]
    )
    air = air_lut[ids[1:-1, 1:-1, 1:-1]]
    return ~air & ~hidden, hidden


def cull_region(volume: RegionVolume, catalogue: BlockCatalogue) -> CullResult:
    """Drop air and fully enclosed opaque blocks from one region volume."""
    if volume.halo < 1:
        raise ValueError("culling needs a region volume with a halo of at least 1")
    opaque_lut, air_lut = block_luts(volume.table, catalogue)
    h = volume.halo - 1  # use exactly one halo layer
    ids = volume.ids[h : volume.ids.shape[0] - h, h : volume.ids.shape[1] - h, h : volume.ids.shape[2] - h]
    ny = ids.shape[0] - 2
    ox, oy, oz = volume.core.x1, volume.core.y1, volume.core.z1
    positions, block_ids = [], []
    culled = air = 0
    for y0 in range(0, ny, SLAB):
        y1 = min(ny, y0 + SLAB)
        slab = ids[y0 : y1 + 2]
        keep, hidden = visible_mask(slab, opaque_lut, air_lut)
        culled += int(np.count_nonzero(hidden))
        air += int(np.count_nonzero(~keep & ~hidden))
        ys, zs, xs = np.nonzero(keep)
        if ys.size:
            positions.append(np.stack([xs + ox, ys + (oy + y0), zs + oz], axis=1).astype(np.int32))
            block_ids.append(slab[1:-1, 1:-1, 1:-1][ys, zs, xs])
    if positions:
        pos = np.concatenate(positions)
        bid = np.concatenate(block_ids).astype(np.uint16)
    else:
        pos = np.zeros((0, 3), dtype=np.int32)
        bid = np.zeros(0, dtype=np.uint16)
    return CullResult(volume.region_x, volume.region_z, pos, bid, culled, air)
