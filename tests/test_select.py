"""AC4: bounding-box selection is exact, across negative coordinates and region boundaries."""

import numpy as np
import pytest

from mcusd.select import (
    BBox,
    ChunkSource,
    chunks_by_region,
    iter_region_volumes,
    region_volume,
)
from mcusd.world.layout import DIMENSIONS, locate
from tests.nbt_builders import random_chunks, write_world

OVERWORLD = DIMENSIONS["overworld"]


def test_bbox_normalises_corners():
    assert BBox.from_corners(5, 70, -3, -2, 60, 4) == BBox(-2, 60, -3, 5, 70, 4)
    with pytest.raises(ValueError):
        BBox(1, 0, 0, 0, 0, 0)


@pytest.mark.parametrize(
    "x, chunk, region",
    [(0, 0, 0), (15, 0, 0), (16, 1, 0), (-1, -1, -1), (-16, -1, -1), (-17, -2, -1),
     (511, 31, 0), (512, 32, 1), (-512, -32, -1), (-513, -33, -2)],
)
def test_floor_division_for_negative_coordinates(x, chunk, region):
    plan = chunks_by_region(BBox(x, 0, 0, x, 0, 0))
    assert list(plan) == [(region, 0)]
    assert plan[(region, 0)] == [(chunk, 0)]


def test_chunks_by_region_spans_four_regions():
    plan = chunks_by_region(BBox(-20, 0, -1, 17, 10, 600))
    assert list(plan) == [(-1, -1), (0, -1), (-1, 0), (0, 0), (-1, 1), (0, 1)]
    assert plan[(-1, -1)] == [(-2, -1), (-1, -1)]
    assert (1, 37) in plan[(0, 1)]


def _world(tmp_path, coords, seed=1, **kwargs):
    chunks = random_chunks(coords, np.random.default_rng(seed))
    root = write_world(tmp_path / "world", chunks, **kwargs)
    loc = locate(root, "overworld")
    return chunks, ChunkSource(loc.region_dir, loc.dimension)


def _expected_blocks(chunks, bbox):
    """Brute-force set of (x, y, z, id) for every non-air block inside bbox."""
    out = set()
    for (cx, cz), volume in chunks.items():
        ys, zs, xs = np.nonzero(volume != "minecraft:air")
        for y, z, x in zip(ys, zs, xs):
            wx, wy, wz = cx * 16 + int(x), int(y) - 64, cz * 16 + int(z)
            if bbox.contains(wx, wy, wz):
                out.add((wx, wy, wz, volume[y, z, x]))
    return out


def _selected_blocks(volumes):
    out = set()
    for vol in volumes:
        interior = vol.interior_ids()
        ox, oy, oz = vol.core.x1, vol.core.y1, vol.core.z1
        ys, zs, xs = np.nonzero(interior)
        for y, z, x in zip(ys, zs, xs):
            out.add((ox + int(x), oy + int(y), oz + int(z), vol.table[int(interior[y, z, x])]))
    return out


@pytest.mark.parametrize("style, layout", [("1.21", "pre-26.1"), ("26", "26.1+")])
@pytest.mark.parametrize(
    "bbox",
    [
        BBox(-16, -64, -16, 15, 319, 15),  # all four origin chunks (four regions)
        BBox(-5, -10, -7, 3, 20, 9),  # small box straddling x=0 and z=0
        BBox(-3, 0, -3, -1, 2, -1),  # entirely negative
        BBox(-40, -100, -40, 40, 400, 40),  # larger than the data and the world height
    ],
)
def test_selection_is_exact(tmp_path, bbox, style, layout):
    coords = [(-1, -1), (-1, 0), (0, -1), (0, 0), (-2, 0)]
    chunks, source = _world(tmp_path, coords, style=style, layout=layout)
    volumes = list(iter_region_volumes(source, bbox))
    selected = _selected_blocks(volumes)
    assert selected == _expected_blocks(chunks, bbox)
    assert all(bbox.contains(x, y, z) for x, y, z, _ in selected)


def test_selection_across_a_region_boundary(tmp_path):
    coords = [(31, 0), (32, 0), (-33, 5), (-32, 5)]
    chunks, source = _world(tmp_path, coords, seed=7)
    for bbox in (BBox(500, -64, 0, 520, 100, 15), BBox(-520, 0, 80, -500, 50, 95)):
        volumes = list(iter_region_volumes(source, bbox))
        assert len(volumes) == 2
        assert _selected_blocks(volumes) == _expected_blocks(chunks, bbox)


def test_halo_reads_neighbours_inside_selection_only(tmp_path):
    chunks, source = _world(tmp_path, [(31, 0), (32, 0)], seed=3)
    bbox = BBox(505, 0, 2, 515, 10, 12)
    vol = region_volume(source, bbox, 0, 0, halo=1)
    assert vol.core == BBox(505, 0, 2, 511, 10, 12)
    # halo cell at x=512 is inside the selection: read from region 1
    assert vol.block_at(512, 5, 5) == chunks[(32, 0)][5 + 64, 5, 0]
    # halo cells outside the selection are air even where the world has blocks
    assert vol.block_at(504, 5, 5) == "minecraft:air"
    assert vol.block_at(506, -1, 5) == "minecraft:air"
    assert vol.ids.shape == (13, 13, 9)


def test_missing_and_partial_chunks_read_as_air(tmp_path):
    chunks = random_chunks([(0, 0), (1, 0)], np.random.default_rng(2), air_fraction=0.0)
    root = write_world(tmp_path / "w", chunks, partial=frozenset({(1, 0)}))
    loc = locate(root, "overworld")
    source = ChunkSource(loc.region_dir, loc.dimension)
    vol = region_volume(source, BBox(0, 0, 0, 47, 10, 15), 0, 0)
    assert vol.chunks_present == 1
    assert vol.ids[vol.interior][:, :, :16].all()
    assert not vol.ids[vol.interior][:, :, 16:].any()  # (1, 0) partial, (2, 0) absent
    assert source.chunks_skipped_partial == 1


def test_selection_outside_dimension_height(tmp_path):
    _, source = _world(tmp_path, [(0, 0)])
    assert region_volume(source, BBox(0, 400, 0, 5, 500, 5), 0, 0) is None
    assert region_volume(source, BBox(0, 0, 0, 5, 5, 5), 3, 3) is None


def test_chunk_cache_avoids_redecoding(tmp_path):
    _, source = _world(tmp_path, [(31, 0), (32, 0)])
    bbox = BBox(500, 0, 0, 520, 5, 5)
    list(iter_region_volumes(source, bbox))
    assert source.chunks_decoded == 2
