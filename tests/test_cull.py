"""AC5: culling is safe and effective."""

import numpy as np
import pytest

from mcusd.blocks import AIR_IDS, BlockCatalogue, BlockIdTable
from mcusd.cull import SLAB, block_luts, cull_region, visible_mask
from mcusd.select import BBox, ChunkSource, RegionVolume, iter_region_volumes
from mcusd.world.layout import locate
from tests.nbt_builders import write_world

STONE = "minecraft:stone"
AIR = "minecraft:air"


def world_with(tmp_path, cells, *, style="1.21", layout="pre-26.1"):
    """Write a world from {(x, y, z): block_id}; every other block in touched chunks is air."""
    chunks = {}
    for (x, y, z), block_id in cells.items():
        key = (x >> 4, z >> 4)
        if key not in chunks:
            chunks[key] = np.full((384, 16, 16), AIR, dtype=object)
        volume = chunks[key]
        volume[y + 64, z & 15, x & 15] = block_id
    root = write_world(tmp_path / "world", chunks, style=style, layout=layout)
    loc = locate(root, "overworld")
    return ChunkSource(loc.region_dir, loc.dimension)


def cube(x0, y0, z0, size, block_id=STONE):
    return {
        (x, y, z): block_id
        for x in range(x0, x0 + size)
        for y in range(y0, y0 + size)
        for z in range(z0, z0 + size)
    }


def export(source, bbox, catalogue=None):
    catalogue = catalogue or BlockCatalogue()
    results = [cull_region(v, catalogue) for v in iter_region_volumes(source, bbox)]
    positions = set()
    for r in results:
        positions |= {tuple(p) for p in r.positions.tolist()}
    return results, positions


def shell(x0, y0, z0, size):
    return {
        p for p in cube(x0, y0, z0, size)
        if min(p[0] - x0, p[1] - y0, p[2] - z0) == 0 or max(p[0] - x0, p[1] - y0, p[2] - z0) == size - 1
    }


def test_solid_cube_keeps_exactly_its_shell(tmp_path):
    source = world_with(tmp_path, cube(0, 0, 0, 16))
    results, kept = export(source, BBox(-8, -8, -8, 40, 40, 40))
    assert sum(r.kept for r in results) == 16**3 - 14**3 == 1352
    assert kept == shell(0, 0, 0, 16)
    assert sum(r.culled for r in results) == 14**3


def test_air_pocket_exposes_its_six_neighbours(tmp_path):
    cells = cube(0, 0, 0, 16)
    cells[(8, 8, 8)] = AIR
    _, kept = export(world_with(tmp_path, cells), BBox(-1, -1, -1, 16, 16, 16))
    neighbours = {(7, 8, 8), (9, 8, 8), (8, 7, 8), (8, 9, 8), (8, 8, 7), (8, 8, 9)}
    assert kept == shell(0, 0, 0, 16) | neighbours
    assert (8, 8, 8) not in kept  # air is never exported


@pytest.mark.parametrize("filler", ["minecraft:cave_air", "minecraft:void_air"])
def test_air_variants_are_dropped_and_expose_neighbours(tmp_path, filler):
    cells = cube(0, 0, 0, 16)
    cells[(8, 8, 8)] = filler
    results, kept = export(world_with(tmp_path, cells), BBox(0, 0, 0, 15, 15, 15))
    assert len(kept) == 1352 + 6
    assert (8, 8, 8) not in kept


@pytest.mark.parametrize("filler", ["minecraft:water", "mymod:mystery_block", "minecraft:glass"])
def test_blocks_next_to_water_or_unknown_blocks_are_never_culled(tmp_path, filler):
    cells = cube(0, 0, 0, 16)
    cells[(8, 8, 8)] = filler
    _, kept = export(world_with(tmp_path, cells), BBox(0, 0, 0, 15, 15, 15))
    neighbours = {(7, 8, 8), (9, 8, 8), (8, 7, 8), (8, 9, 8), (8, 8, 7), (8, 8, 9)}
    assert neighbours <= kept
    assert (8, 8, 8) in kept  # non-air, non-opaque blocks are themselves kept
    assert len(kept) == 1352 + 6 + 1


@pytest.mark.parametrize(
    "origin",
    [
        (8, 0, 8),  # straddles four chunks
        (-8, 0, -8),  # straddles x=0 / z=0 and four regions
        (504, 0, 2),  # straddles the region boundary at x=512
    ],
)
def test_cube_across_chunk_and_region_boundaries_matches_single_chunk(tmp_path, origin):
    source = world_with(tmp_path, cube(*origin, 16))
    x0, y0, z0 = origin
    results, kept = export(source, BBox(x0 - 2, y0 - 2, z0 - 2, x0 + 17, y0 + 17, z0 + 17))
    assert kept == shell(*origin, 16)
    assert len({(r.region_x, r.region_z) for r in results}) >= 1


def test_selection_edge_is_exposed(tmp_path):
    """Neighbours outside the selection are non-occluding, even when the world has blocks there."""
    source = world_with(tmp_path, cube(0, 0, 0, 32))
    _, kept = export(source, BBox(8, 8, 8, 23, 23, 23))
    assert kept == shell(8, 8, 8, 16)


def test_ungenerated_neighbour_chunk_is_non_occluding(tmp_path):
    # solid column filling chunk (0, 0) from y=0..15; chunk (1, 0) is never written
    source = world_with(tmp_path, cube(0, 0, 0, 16))
    _, kept = export(source, BBox(0, 0, 0, 31, 15, 15))
    assert {(15, 8, 8), (15, 1, 1)} <= kept  # the x=15 face borders the missing chunk


def test_both_palette_encodings_cull_identically(tmp_path):
    a, _ = export(world_with(tmp_path / "a", cube(-3, 5, -3, 10)), BBox(-5, 0, -5, 10, 20, 10))
    b, _ = export(world_with(tmp_path / "b", cube(-3, 5, -3, 10), style="26", layout="26.1+"),
                  BBox(-5, 0, -5, 10, 20, 10))
    assert sum(r.kept for r in a) == sum(r.kept for r in b) == 10**3 - 8**3


def test_positions_and_ids_are_aligned(tmp_path):
    cells = cube(0, 0, 0, 4)
    cells[(0, 0, 0)] = "minecraft:gold_block"
    cells[(3, 3, 3)] = "minecraft:dirt"
    source = world_with(tmp_path, cells)
    results, _ = export(source, BBox(0, 0, 0, 3, 3, 3))
    (result,) = results
    assert result.positions.dtype == np.int32 and result.block_ids.dtype == np.uint16
    got = {tuple(p): source.table[i] for p, i in zip(result.positions.tolist(), result.block_ids.tolist())}
    assert len(got) == result.kept == 4**3 - 2**3
    assert got == {p: cells[p] for p in got}


def test_visible_mask_on_hand_built_volume():
    # 3x3x3 opaque block of index 1 inside a 5x5x5 volume with air halo
    ids = np.zeros((5, 5, 5), dtype=np.uint16)
    ids[1:4, 1:4, 1:4] = 1
    opaque = np.array([False, True])
    air = np.array([True, False])
    keep, hidden = visible_mask(ids, opaque, air)
    assert keep.shape == (3, 3, 3)
    assert hidden.sum() == 1 and hidden[1, 1, 1]  # only the centre is enclosed
    assert keep.sum() == 26


def test_halo_is_required():
    vol = RegionVolume(0, 0, BBox(0, 0, 0, 1, 1, 1), 0, np.zeros((2, 2, 2), dtype=np.uint16))
    with pytest.raises(ValueError, match="halo"):
        cull_region(vol, BlockCatalogue())


def test_tall_volume_is_processed_in_slabs(tmp_path):
    """A column taller than one slab (64 layers) keeps exactly its surface."""
    cells = {(x, y, z): STONE for x in range(3) for z in range(3) for y in range(-64, 100)}
    results, kept = export(world_with(tmp_path, cells), BBox(0, -64, 0, 2, 319, 2))
    assert (1, 50, 1) not in kept and (1, 0, 1) not in kept  # interior column culled
    assert len(kept) == 3 * 3 * 164 - 164 + 2  # all but the core column; its two caps stay


SIX = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]


def hand_volume(core, halo, cells, region=(0, 0)):
    """A region volume built without a world: ``cells`` maps absolute ``(x, y, z)`` to a block ID; the rest is air."""
    table = BlockIdTable()
    extended = core.expand(halo)
    ids = np.zeros(extended.shape, dtype=np.uint16)
    for (x, y, z), block_id in cells.items():
        ids[y - extended.y1, z - extended.z1, x - extended.x1] = table.index(block_id)
    return RegionVolume(*region, core, halo, ids, 0, table)


def box_cells(box, block_id=STONE):
    return {(x, y, z): block_id for x in range(box.x1, box.x2 + 1)
            for y in range(box.y1, box.y2 + 1) for z in range(box.z1, box.z2 + 1)}


def brute_force(volume, catalogue):
    """Per-block reference culler: visible blocks in Y, Z, X order, plus the hidden and air counts."""
    opaque = [catalogue.info(b).opaque for b in volume.table.ids]
    ox, oy, oz = volume.origin

    def at(x, y, z):
        return int(volume.ids[y - oy, z - oz, x - ox])

    core = volume.core
    kept, hidden, air = [], 0, 0
    for y in range(core.y1, core.y2 + 1):
        for z in range(core.z1, core.z2 + 1):
            for x in range(core.x1, core.x2 + 1):
                block = at(x, y, z)
                if opaque[block] and all(opaque[at(x + dx, y + dy, z + dz)] for dx, dy, dz in SIX):
                    hidden += 1
                elif volume.table[block] in AIR_IDS:
                    air += 1
                else:
                    kept.append(((x, y, z), block))
    return kept, hidden, air


def test_all_air_volume_yields_typed_empty_arrays():
    core = BBox(0, 0, 0, 2, 2, 2)
    result = cull_region(hand_volume(core, 1, {}), BlockCatalogue())
    assert result.positions.shape == (0, 3) and result.positions.dtype == np.int32
    assert result.block_ids.shape == (0,) and result.block_ids.dtype == np.uint16
    assert (result.kept, result.culled, result.air) == (0, 0, core.volume)


@pytest.mark.parametrize(
    "inner, outer, kept, culled",
    [
        (STONE, AIR, 0, 27),  # the inner halo layer encloses every core block
        (AIR, STONE, 26, 1),  # the outer halo layer is ignored: only the core's centre is enclosed
    ],
)
def test_a_wider_halo_uses_only_its_innermost_layer(inner, outer, kept, culled):
    core = BBox(0, 0, 0, 2, 2, 2)
    cells = box_cells(core.expand(2), outer)
    cells.update(box_cells(core.expand(1), inner))
    cells.update(box_cells(core))
    result = cull_region(hand_volume(core, 2, cells), BlockCatalogue())
    assert (result.kept, result.culled) == (kept, culled)


def test_positions_are_absolute_and_ordered_y_then_z_then_x():
    core = BBox(-3, -64, -5, -2, -63, -4)
    result = cull_region(hand_volume(core, 1, box_cells(core), region=(-1, -1)), BlockCatalogue())
    assert (result.region_x, result.region_z) == (-1, -1)
    assert result.positions.tolist() == [[x, y, z] for y in (-64, -63) for z in (-5, -4) for x in (-3, -2)]


@pytest.mark.parametrize("halo", [1, 2])
def test_random_volume_matches_a_per_block_reference(halo):
    """Mixed opaque, transparent and air blocks over a core taller than one slab."""
    core = BBox(-2, -64, 510, 1, -64 + SLAB + 5, 513)
    extended = core.expand(halo)
    volume = hand_volume(core, halo, {})
    for block_id in (STONE, "minecraft:glass", "minecraft:cave_air"):
        volume.table.index(block_id)
    rng = np.random.default_rng(11)
    volume.ids = rng.choice(4, size=extended.shape, p=[0.2, 0.6, 0.1, 0.1]).astype(np.uint16)
    catalogue = BlockCatalogue()
    result = cull_region(volume, catalogue)
    kept, hidden, air = brute_force(volume, catalogue)
    assert hidden > 0 and air > 0 and kept  # every branch of the reference is exercised
    assert result.positions.tolist() == [list(p) for p, _ in kept]
    assert result.block_ids.tolist() == [b for _, b in kept]
    assert (result.culled, result.air) == (hidden, air)
    assert result.kept + result.culled + result.air == core.volume


def test_opacity_override_disables_culling():
    core = BBox(0, 0, 0, 2, 2, 2)
    volume = hand_volume(core, 1, box_cells(core.expand(1)))
    assert cull_region(volume, BlockCatalogue()).culled == 27
    result = cull_region(volume, BlockCatalogue({STONE: {"opaque": False}}))
    assert (result.kept, result.culled, result.air) == (27, 0, 0)


def test_block_luts_follow_table_order():
    table = BlockIdTable()
    for block_id in (STONE, "minecraft:cave_air", "minecraft:glass", "mymod:mystery_block", "minecraft:void_air"):
        table.index(block_id)
    opaque, air = block_luts(table, BlockCatalogue())
    assert opaque.dtype == bool and air.dtype == bool
    assert opaque.tolist() == [False, True, False, False, False, False]
    assert air.tolist() == [True, False, True, False, False, True]


def test_block_luts_apply_overrides_except_to_air():
    table = BlockIdTable()
    table.index("minecraft:glass")
    catalogue = BlockCatalogue({"minecraft:glass": {"opaque": True}, AIR: {"opaque": True}})
    opaque, air = block_luts(table, catalogue)
    assert opaque.tolist() == [False, True]  # air is never opaque, even when overridden
    assert air.tolist() == [True, False]
