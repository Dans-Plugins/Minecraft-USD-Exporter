"""AC4: bounding-box selection is exact, across negative coordinates and region boundaries.

Also characterizes the box geometry helpers and the chunk source's caches.
"""

import numpy as np
import pytest

from mcusd.select import (
    BBox,
    ChunkSource,
    chunks_by_region,
    clamp_to_dimension,
    iter_region_volumes,
    region_bounds,
    region_volume,
)
from mcusd.world.layout import DIMENSIONS, locate
from tests.nbt_builders import random_chunks, write_world

OVERWORLD = DIMENSIONS["overworld"]


def test_bbox_normalises_corners():
    assert BBox.from_corners(5, 70, -3, -2, 60, 4) == BBox(-2, 60, -3, 5, 70, 4)
    with pytest.raises(ValueError):
        BBox(1, 0, 0, 0, 0, 0)


@pytest.mark.parametrize("corners", [(1, 0, 0, 0, 0, 0), (0, 1, 0, 0, 0, 0), (0, 0, 1, 0, 0, 0)])
def test_bbox_rejects_an_inverted_axis(corners):
    with pytest.raises(ValueError, match="degenerate bounding box"):
        BBox(*corners)


def test_single_block_box_is_valid():
    box = BBox(3, -64, -7, 3, -64, -7)
    assert box.shape == (1, 1, 1)
    assert box.volume == 1
    assert box.contains(3, -64, -7)


def test_bbox_shape_is_y_z_x_and_inclusive():
    box = BBox(-2, 10, -3, 5, 12, 4)
    assert box.shape == (3, 8, 8)
    assert box.volume == 3 * 8 * 8
    assert box.as_list() == [-2, 10, -3, 5, 12, 4]


@pytest.mark.parametrize(
    "point, inside",
    [((-2, 10, -3), True), ((5, 12, 4), True), ((0, 11, 0), True),
     ((-3, 10, -3), False), ((6, 12, 4), False), ((0, 9, 0), False),
     ((0, 13, 0), False), ((0, 11, -4), False), ((0, 11, 5), False)],
)
def test_bbox_contains_is_inclusive_on_both_ends(point, inside):
    assert BBox(-2, 10, -3, 5, 12, 4).contains(*point) is inside


def test_bbox_intersect_overlap_is_symmetric():
    a = BBox(0, 0, 0, 10, 10, 10)
    b = BBox(5, -5, 8, 20, 3, 30)
    assert a.intersect(b) == b.intersect(a) == BBox(5, 0, 8, 10, 3, 10)
    assert a.intersect(a) == a


def test_bbox_intersect_sharing_one_plane_is_one_block_thick():
    a = BBox(0, 0, 0, 10, 10, 10)
    assert a.intersect(BBox(10, 0, 0, 20, 10, 10)) == BBox(10, 0, 0, 10, 10, 10)


@pytest.mark.parametrize(
    "other",
    [BBox(11, 0, 0, 20, 10, 10), BBox(0, 11, 0, 10, 20, 10), BBox(0, 0, 11, 10, 10, 20),
     BBox(-20, 0, 0, -1, 10, 10)],
)
def test_bbox_intersect_disjoint_on_any_axis_is_none(other):
    assert BBox(0, 0, 0, 10, 10, 10).intersect(other) is None


def test_bbox_expand_grows_every_side():
    box = BBox(0, 0, 0, 1, 1, 1)
    assert box.expand(2) == BBox(-2, -2, -2, 3, 3, 3)
    assert box.expand(0) == box
    assert box.expand(1).shape == (4, 4, 4)


def test_bbox_expand_by_a_negative_amount_past_the_centre_raises():
    """Shrinking is allowed only while the box stays non-degenerate."""
    assert BBox(0, 0, 0, 4, 4, 4).expand(-2) == BBox(2, 2, 2, 2, 2, 2)
    with pytest.raises(ValueError):
        BBox(0, 0, 0, 1, 1, 1).expand(-1)


def test_bbox_is_frozen():
    box = BBox(0, 0, 0, 1, 1, 1)
    with pytest.raises(AttributeError):
        box.x1 = 5


@pytest.mark.parametrize(
    "region, expected",
    [((0, 0), BBox(0, -64, 0, 511, 319, 511)),
     ((1, 0), BBox(512, -64, 0, 1023, 319, 511)),
     ((-1, -2), BBox(-512, -64, -1024, -1, 319, -513))],
)
def test_region_bounds_cover_512_blocks_per_side(region, expected):
    bounds = region_bounds(*region, OVERWORLD.min_y, OVERWORLD.max_y)
    assert bounds == expected
    assert bounds.shape == (384, 512, 512)


@pytest.mark.parametrize(
    "dimension, bbox, expected",
    [("overworld", BBox(0, -100, 0, 5, 400, 5), BBox(0, -64, 0, 5, 319, 5)),
     ("the_nether", BBox(0, -10, 0, 5, 300, 5), BBox(0, 0, 0, 5, 255, 5)),
     ("the_end", BBox(-9, 20, -9, 9, 40, 9), BBox(-9, 20, -9, 9, 40, 9)),
     ("overworld", BBox(0, 320, 0, 5, 330, 5), None),
     ("the_nether", BBox(0, -64, 0, 5, -1, 5), None)],
)
def test_clamp_to_dimension_only_trims_y(dimension, bbox, expected):
    assert clamp_to_dimension(bbox, DIMENSIONS[dimension]) == expected


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


def test_huge_selection_streams_without_planning_every_chunk(tmp_path, monkeypatch):
    """A 100,000-block-wide box over a one-chunk world yields one volume, quickly."""
    import mcusd.select as select_module

    _, source = _world(tmp_path, [(0, 0)])

    def fail(*args, **kwargs):  # the iterator must not enumerate every chunk
        raise AssertionError("chunks_by_region called")

    monkeypatch.setattr(select_module, "chunks_by_region", fail)
    bbox = BBox(-50_000, 0, -50_000, 50_000, 3, 50_000)
    volumes = list(iter_region_volumes(source, bbox))
    assert [(v.region_x, v.region_z) for v in volumes] == [(0, 0)]


def test_regions_without_a_file_are_skipped(tmp_path):
    _, source = _world(tmp_path, [(0, 0)])
    assert region_volume(source, BBox(-5, 0, -5, 5, 5, 5), -1, -1) is None
    assert source.has_region(0, 0) and not source.has_region(-1, -1)


def _source(tmp_path, coords, **cache):
    chunks = random_chunks(coords, np.random.default_rng(4))
    loc = locate(write_world(tmp_path / "world", chunks), "overworld")
    return chunks, ChunkSource(loc.region_dir, loc.dimension, **cache)


def test_chunk_maps_palettes_to_layer_wide_indices(tmp_path):
    chunks, source = _source(tmp_path, [(0, 0), (1, 0)])
    volumes = {coord: source.chunk(*coord) for coord in chunks}
    names = np.array(source.table.ids, dtype=object)  # the table grows as chunks are decoded
    for coord, volume in volumes.items():
        assert volume.dtype == np.uint16 and volume.shape == (384, 16, 16)
        assert (names[volume] == chunks[coord]).all()
    assert source.table[0] == "minecraft:air"


def test_absent_chunks_are_none_and_not_counted_as_decoded(tmp_path):
    _, source = _source(tmp_path, [(0, 0)])
    assert source.chunk(1, 0) is None  # region file exists, chunk does not
    assert source.chunk(-1, -1) is None  # no region file at all
    assert source.chunks_decoded == 0 and source.chunks_skipped_partial == 0


def test_chunk_cache_hit_returns_the_same_array(tmp_path):
    _, source = _source(tmp_path, [(0, 0), (1, 0)], chunk_cache=1)
    first = source.chunk(0, 0)
    assert source.chunk(0, 0) is first
    assert source.chunks_decoded == 1
    source.chunk(1, 0)  # evicts (0, 0)
    assert source.chunk(0, 0) is not first
    assert source.chunks_decoded == 3


def test_chunk_cache_evicts_the_least_recently_used_chunk(tmp_path):
    _, source = _source(tmp_path, [(0, 0), (1, 0), (2, 0)], chunk_cache=2)
    for coord in [(0, 0), (1, 0), (0, 0), (2, 0)]:  # touching (0, 0) again makes (1, 0) the oldest
        source.chunk(*coord)
    assert source.chunks_decoded == 3
    source.chunk(0, 0)
    assert source.chunks_decoded == 3  # still cached
    source.chunk(1, 0)
    assert source.chunks_decoded == 4  # evicted, decoded again


@pytest.mark.parametrize("region_cache, opens", [(1, 2), (4, 1)])
def test_region_cache_reopens_only_evicted_files(tmp_path, monkeypatch, region_cache, opens):
    import mcusd.select as select_module

    opened = []
    real = select_module.RegionFile

    def counting(path):
        opened.append(path.name)
        return real(path)

    monkeypatch.setattr(select_module, "RegionFile", counting)
    _, source = _source(tmp_path, [(0, 0)], region_cache=region_cache)
    assert source.has_region(0, 0) and source.has_region(0, 0)
    assert not source.has_region(-1, -1)  # a missing file is cached too, and takes a slot
    assert source.has_region(0, 0)
    assert opened == ["r.0.0.mca"] * opens
