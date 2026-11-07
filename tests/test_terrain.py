"""AC6: the terrain layer is valid, region-local, per-instance-ID-carrying instancing."""

import numpy as np
import pytest
from pxr import Gf, Kind, Sdf, Usd, UsdGeom

from mcusd.blocks import BlockCatalogue, BlockIdTable
from mcusd.cull import CullResult, cull_region
from mcusd.select import BBox, ChunkSource, iter_region_volumes
from mcusd.usd.terrain import (
    TERRAIN_FILENAME,
    TerrainWriter,
    define_unit_cube,
    instance_ids,
    region_prim_name,
)
from mcusd.world.layout import DIMENSIONS, locate
from tests.nbt_builders import random_chunks, write_world

STONE = "minecraft:stone"
DIRT = "minecraft:dirt"
GOLD = "minecraft:gold_block"


def export_terrain(tmp_path, chunks, bbox, catalogue=None, **world_kwargs):
    root = write_world(tmp_path / "world", chunks, **world_kwargs)
    loc = locate(root, "overworld")
    source = ChunkSource(loc.region_dir, loc.dimension)
    catalogue = catalogue or BlockCatalogue()
    writer = TerrainWriter(tmp_path / "out", catalogue, source.table, loc.dimension)
    results = []
    for volume in iter_region_volumes(source, bbox):
        result = cull_region(volume, catalogue)
        writer.add_region(result)
        results.append(result)
    stats = writer.close()
    return stats, results, Usd.Stage.Open(str(stats.path))


@pytest.fixture
def exported(tmp_path):
    coords = [(-1, -1), (-1, 0), (0, -1), (0, 0), (32, 0)]
    chunks = random_chunks(coords, np.random.default_rng(11), air_fraction=0.3)
    bbox = BBox(-16, -64, -16, 527, 70, 15)
    return export_terrain(tmp_path, chunks, bbox)


def instancers(stage):
    return [UsdGeom.PointInstancer(p) for p in stage.Traverse() if p.IsA(UsdGeom.PointInstancer)]


def test_every_instancer_satisfies_ac6(exported):
    stats, results, stage = exported
    block_ids_table = list(stage.GetPrimAtPath("/World/BlockLibrary").GetAttribute("mcusd:blockIds").Get())
    total = 0
    found = instancers(stage)
    assert len(found) == 5  # four origin regions plus region (1, 0)
    for inst in found:
        proto_indices = np.array(inst.GetProtoIndicesAttr().Get())
        positions = np.array(inst.GetPositionsAttr().Get())
        ids = np.array(inst.GetIdsAttr().Get())
        block_ids = np.array(UsdGeom.PrimvarsAPI(inst).GetPrimvar("mcusd:blockId").Get())
        assert len(proto_indices) == len(positions) == len(ids) == len(block_ids) > 0
        targets = inst.GetPrototypesRel().GetTargets()
        assert proto_indices.min() >= 0 and proto_indices.max() < len(targets)
        proto_ns_ids = [stage.GetPrimAtPath(t).GetAttribute("mcusd:namespacedId").Get() for t in targets]
        for proto_index, block_id in zip(proto_indices.tolist(), block_ids.tolist()):
            assert block_ids_table[block_id] == proto_ns_ids[proto_index]
        assert len(np.unique(ids)) == len(ids)
        lo, hi = (np.array(v) for v in inst.GetExtentAttr().Get())
        assert np.all(positions >= lo) and np.all(positions + 1 <= hi)
        total += len(positions)
    assert total == stats.instances == sum(r.kept for r in results)


def test_block_id_primvar_is_per_instance(exported):
    _, _, stage = exported
    primvar = UsdGeom.PrimvarsAPI(instancers(stage)[0]).GetPrimvar("mcusd:blockId")
    assert primvar.GetInterpolation() == UsdGeom.Tokens.vertex
    assert primvar.GetTypeName() == Sdf.ValueTypeNames.IntArray
    assert instancers(stage)[0].GetIdsAttr().GetTypeName() == Sdf.ValueTypeNames.Int64Array


def test_world_positions_round_trip(exported):
    """translate + region-local position == the culled block's absolute position."""
    _, results, stage = exported
    expected = {(r.region_x, r.region_z): {tuple(p) for p in r.positions.tolist()} for r in results}
    for inst in instancers(stage):
        region = inst.GetPrim().GetParent()
        rx, rz = next(k for k in expected if region_prim_name(*k) == region.GetName())
        ops = UsdGeom.Xformable(region).GetOrderedXformOps()
        assert len(ops) == 1 and ops[0].GetPrecision() == UsdGeom.XformOp.PrecisionDouble
        translate = ops[0].Get()
        assert translate == Gf.Vec3d(rx * 512, 0, rz * 512)
        local = np.array(inst.GetPositionsAttr().Get())
        assert local[:, [0, 2]].min() >= 0 and local[:, [0, 2]].max() <= 511
        absolute = {tuple(int(v) for v in p) for p in local + np.array(translate)}
        assert absolute == expected[(rx, rz)]


def test_instance_ids_formula(exported):
    _, _, stage = exported
    inst = instancers(stage)[0]
    positions = np.array(inst.GetPositionsAttr().Get()).astype(np.int64)
    ids = np.array(inst.GetIdsAttr().Get())
    x, y, z = positions[:, 0], positions[:, 1], positions[:, 2]
    assert np.array_equal(ids, (y + 64) * 512 * 512 + z * 512 + x)
    assert instance_ids(np.array([[511, 319, 511]]), -64)[0] == 383 * 512 * 512 + 511 * 512 + 511


def test_block_library_is_abstract_and_never_traversed(exported):
    _, _, stage = exported
    library = stage.GetPrimAtPath("/World/BlockLibrary")
    assert library.IsAbstract() and library.GetSpecifier() == Sdf.SpecifierClass
    traversed = {p.GetPath() for p in stage.Traverse()}
    assert not any(p.HasPrefix(library.GetPath()) for p in traversed)
    stone = stage.GetPrimAtPath("/World/BlockLibrary/stone")
    assert stone.IsA(UsdGeom.Mesh) and stone.IsAbstract()


def test_prototypes_inherit_library_meshes(exported):
    _, _, stage = exported
    catalogue = BlockCatalogue()
    for inst in instancers(stage):
        for target in inst.GetPrototypesRel().GetTargets():
            proto = stage.GetPrimAtPath(target)
            assert proto.GetParent().GetName() == "Prototypes"
            assert proto.GetParent().GetParent() == inst.GetPrim()
            assert not proto.IsAbstract() and proto.IsA(UsdGeom.Mesh)
            library_path = Sdf.Path("/World/BlockLibrary").AppendChild(proto.GetName())
            assert library_path in proto.GetInherits().GetAllDirectInherits()
            ns_id = proto.GetAttribute("mcusd:namespacedId").Get()
            color = UsdGeom.Mesh(proto).GetDisplayColorPrimvar().Get()[0]
            assert tuple(round(c, 5) for c in color) == tuple(round(c, 5) for c in catalogue.info(ns_id).color)
            assert len(UsdGeom.Mesh(proto).GetPointsAttr().Get()) == 8


def test_stage_metadata_and_kinds(exported):
    _, _, stage = exported
    assert stage.GetDefaultPrim().GetPath() == Sdf.Path("/World")
    assert UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.y
    assert UsdGeom.GetStageMetersPerUnit(stage) == 1.0
    assert Usd.ModelAPI(stage.GetPrimAtPath("/World")).GetKind() == Kind.Tokens.assembly
    assert Usd.ModelAPI(stage.GetPrimAtPath("/World/Terrain")).GetKind() == Kind.Tokens.group
    names = sorted(p.GetName() for p in stage.GetPrimAtPath("/World/Terrain").GetChildren())
    assert names == ["r_0_0", "r_0_n1", "r_1_0", "r_n1_0", "r_n1_n1"]


def test_terrain_is_always_a_crate_file(exported):
    stats, _, _ = exported
    assert stats.path.name == TERRAIN_FILENAME == "terrain.usdc"
    assert stats.path.read_bytes()[:8] == b"PXR-USDC"
    assert Sdf.Layer.FindOrOpen(str(stats.path)).GetFileFormat().formatId == "usdc"


def test_region_prim_names_are_valid():
    for rx, rz in [(0, 0), (-1, 0), (-58594, 58593)]:
        assert Sdf.Path.IsValidIdentifier(region_prim_name(rx, rz))
    assert region_prim_name(-1, 2) == "r_n1_2"


def test_cube_mesh_is_closed_and_outward_facing(exported):
    """Each face normal (from its winding) points away from the cube centre."""
    _, _, stage = exported
    mesh = UsdGeom.Mesh(stage.GetPrimAtPath("/World/BlockLibrary/stone"))
    points = np.array(mesh.GetPointsAttr().Get())
    indices = np.array(mesh.GetFaceVertexIndicesAttr().Get()).reshape(6, 4)
    for face in indices:
        a, b, c = points[face[0]], points[face[1]], points[face[2]]
        normal = np.cross(b - a, c - a)
        assert np.dot(normal, points[face].mean(axis=0) - 0.5) > 0
    assert np.array_equal(np.sort(np.unique(indices, return_counts=True)[1]), [3] * 8)


def test_empty_regions_are_not_written(tmp_path):
    chunks = {(0, 0): np.full((384, 16, 16), "minecraft:air", dtype=object)}
    stats, results, stage = export_terrain(tmp_path, chunks, BBox(0, 0, 0, 15, 15, 15))
    assert stats.instances == 0 and stats.regions == 0
    assert not stage.GetPrimAtPath("/World/Terrain").GetChildren()


def test_shared_block_id_across_regions(exported):
    """primvars:mcusd:blockId means the same block in every region, unlike protoIndices."""
    _, _, stage = exported
    table = list(stage.GetPrimAtPath("/World/BlockLibrary").GetAttribute("mcusd:blockIds").Get())
    stone = table.index("minecraft:stone")
    for inst in instancers(stage):
        block_ids = np.array(UsdGeom.PrimvarsAPI(inst).GetPrimvar("mcusd:blockId").Get())
        protos = [stage.GetPrimAtPath(t).GetAttribute("mcusd:namespacedId").Get()
                  for t in inst.GetPrototypesRel().GetTargets()]
        proto_indices = np.array(inst.GetProtoIndicesAttr().Get())
        mask = block_ids == stone
        assert mask.any()
        assert {protos[i] for i in proto_indices[mask]} == {"minecraft:stone"}


def test_each_region_is_persisted_as_it_is_written(tmp_path):
    """Completed regions are on disk (and the layer clean) before close()."""
    coords = [(0, 0), (32, 0)]
    chunks = random_chunks(coords, np.random.default_rng(5), air_fraction=0.3)
    root = write_world(tmp_path / "world", chunks)
    loc = locate(root, "overworld")
    source = ChunkSource(loc.region_dir, loc.dimension)
    catalogue = BlockCatalogue()
    writer = TerrainWriter(tmp_path / "out", catalogue, source.table, loc.dimension)
    volumes = iter_region_volumes(source, BBox(0, -64, 0, 527, 70, 15))
    first = cull_region(next(volumes), catalogue)
    writer.add_region(first)
    assert not writer.layer.dirty
    on_disk = Sdf.Layer.OpenAsAnonymous(str(writer.path))
    positions = on_disk.GetAttributeAtPath("/World/Terrain/r_0_0/Blocks.positions").default
    assert len(positions) == first.kept
    writer.add_region(cull_region(next(volumes), catalogue))
    stats = writer.close()
    stage = Usd.Stage.Open(str(stats.path))
    assert sum(len(i.GetPositionsAttr().Get()) for i in instancers(stage)) == stats.instances
    assert stage.GetPrimAtPath("/World/BlockLibrary").GetAttribute("mcusd:blockIds").Get()


def direct_writer(tmp_path, *block_ids, dimension="overworld"):
    """A writer for hand-built cull results, so a case needs no world on disk."""
    table = BlockIdTable()
    for block_id in block_ids:
        table.index(block_id)
    return TerrainWriter(tmp_path / "out", BlockCatalogue(), table, DIMENSIONS[dimension]), table


def cull_result(table, region_x, region_z, cells):
    """A :class:`CullResult` from ``[((x, y, z), block_id), ...]`` in absolute coordinates."""
    positions = np.array([p for p, _ in cells], dtype=np.int32).reshape(-1, 3)
    block_ids = np.array([table.index(b) for _, b in cells], dtype=np.uint16)
    return CullResult(region_x, region_z, positions, block_ids, culled=0, air=0)


@pytest.mark.parametrize(
    "region, position",
    [
        ((0, 0), (512, 0, 0)),
        ((0, 0), (-1, 0, 0)),
        ((0, 0), (0, 0, 512)),
        ((0, 0), (0, 0, -1)),
        ((-1, 0), (0, 0, 0)),
    ],
)
def test_positions_outside_the_region_are_rejected_before_anything_is_authored(tmp_path, region, position):
    writer, table = direct_writer(tmp_path, STONE)
    with pytest.raises(ValueError, match="outside the region"):
        writer.add_region(cull_result(table, *region, [(position, STONE)]))
    assert writer.stats.regions == writer.stats.instances == 0
    assert not writer.stage.GetPrimAtPath("/World/Terrain").GetChildren()


def test_negative_region_in_the_nether_is_exactly_region_local(tmp_path):
    """Positions, IDs (with the Nether's min_y of 0), extent and translate, value by value."""
    writer, table = direct_writer(tmp_path, STONE, dimension="the_nether")
    writer.add_region(cull_result(table, -1, -1, [((-511, 5, -510), STONE), ((-509, 7, -510), STONE)]))
    stage = Usd.Stage.Open(str(writer.close().path))
    region = stage.GetPrimAtPath("/World/Terrain/r_n1_n1")
    assert UsdGeom.Xformable(region).GetOrderedXformOps()[0].Get() == Gf.Vec3d(-512, 0, -512)
    inst = UsdGeom.PointInstancer(region.GetChild("Blocks"))
    assert [tuple(p) for p in inst.GetPositionsAttr().Get()] == [(1, 5, 2), (3, 7, 2)]
    assert list(inst.GetIdsAttr().Get()) == [5 * 512 * 512 + 2 * 512 + 1, 7 * 512 * 512 + 2 * 512 + 3]
    assert [tuple(v) for v in inst.GetExtentAttr().Get()] == [(1, 5, 2), (4, 8, 3)]


def test_block_y_stays_absolute_because_the_region_translate_has_no_y(tmp_path):
    writer, table = direct_writer(tmp_path, STONE)
    writer.add_region(cull_result(table, 0, 0, [((0, -64, 0), STONE)]))
    stage = Usd.Stage.Open(str(writer.close().path))
    inst = UsdGeom.PointInstancer(stage.GetPrimAtPath("/World/Terrain/r_0_0/Blocks"))
    assert [tuple(p) for p in inst.GetPositionsAttr().Get()] == [(0, -64, 0)]
    assert list(inst.GetIdsAttr().Get()) == [0]


def test_prototypes_follow_table_order_not_first_appearance(tmp_path):
    writer, table = direct_writer(tmp_path, STONE, DIRT)
    writer.add_region(cull_result(table, 0, 0, [((0, 0, 0), DIRT), ((1, 0, 0), STONE), ((2, 0, 0), DIRT)]))
    stage = Usd.Stage.Open(str(writer.close().path))
    inst = UsdGeom.PointInstancer(stage.GetPrimAtPath("/World/Terrain/r_0_0/Blocks"))
    assert [t.name for t in inst.GetPrototypesRel().GetTargets()] == ["stone", "dirt"]
    assert list(inst.GetProtoIndicesAttr().Get()) == [1, 0, 1]
    assert list(UsdGeom.PrimvarsAPI(inst).GetPrimvar("mcusd:blockId").Get()) == [2, 1, 2]


def test_modded_block_keeps_its_encoded_prim_name_and_namespaced_id(tmp_path):
    writer, table = direct_writer(tmp_path, "mymod:stone")
    writer.add_region(cull_result(table, 0, 0, [((0, 0, 0), "mymod:stone")]))
    stage = Usd.Stage.Open(str(writer.close().path))
    proto = stage.GetPrimAtPath("/World/Terrain/r_0_0/Blocks/Prototypes/_mymod_3astone")
    assert proto.GetAttribute("mcusd:namespacedId").Get() == "mymod:stone"
    assert Sdf.Path("/World/BlockLibrary/_mymod_3astone") in proto.GetInherits().GetAllDirectInherits()


def test_stats_count_regions_instances_and_shared_prototypes(tmp_path):
    writer, table = direct_writer(tmp_path, STONE, DIRT, GOLD)
    writer.add_region(cull_result(table, 0, 0, [((0, 0, 0), STONE), ((1, 0, 0), DIRT)]))
    writer.add_region(cull_result(table, -1, 0, [((-1, 0, 0), STONE), ((-2, 0, 0), GOLD), ((-3, 0, 0), GOLD)]))
    stats = writer.close()
    assert stats.path == tmp_path / "out" / TERRAIN_FILENAME
    assert (stats.regions, stats.instances, stats.prototypes) == (2, 5, 3)
    assert stats.per_region == {(0, 0): 2, (-1, 0): 3}
    stage = Usd.Stage.Open(str(stats.path))
    library = stage.GetPrimAtPath("/World/BlockLibrary")
    assert sorted(p.GetName() for p in library.GetAllChildren()) == ["dirt", "gold_block", "stone"]


def test_block_id_table_lists_unused_entries_but_the_library_does_not(tmp_path):
    """Indices stay valid for every region: the table is whole, the library only what was used."""
    writer, table = direct_writer(tmp_path, STONE, DIRT)
    writer.add_region(cull_result(table, 0, 0, [((0, 0, 0), DIRT)]))
    stats = writer.close()
    stage = Usd.Stage.Open(str(stats.path))
    library = stage.GetPrimAtPath("/World/BlockLibrary")
    assert list(library.GetAttribute("mcusd:blockIds").Get()) == ["minecraft:air", STONE, DIRT]
    assert [p.GetName() for p in library.GetAllChildren()] == ["dirt"]
    assert stats.prototypes == 1


def test_empty_result_changes_no_stats(tmp_path):
    writer, table = direct_writer(tmp_path, STONE)
    writer.add_region(cull_result(table, 3, 4, []))
    stats = writer.close()
    assert stats.regions == stats.instances == stats.prototypes == 0
    assert stats.per_region == {}


def test_rewriting_replaces_the_previous_terrain_file(tmp_path):
    def write(region_x):
        writer, table = direct_writer(tmp_path, STONE)
        writer.add_region(cull_result(table, region_x, 0, [((region_x * 512, 0, 0), STONE)]))
        return writer.close()

    write(0)
    stats = write(1)
    assert stats.regions == 1
    stage = Usd.Stage.Open(str(stats.path))
    assert [p.GetName() for p in stage.GetPrimAtPath("/World/Terrain").GetChildren()] == ["r_1_0"]


def test_region_prim_names_for_each_sign_combination():
    assert region_prim_name(0, 0) == "r_0_0"
    assert region_prim_name(3, 4) == "r_3_4"
    assert region_prim_name(0, -1) == "r_0_n1"
    assert region_prim_name(-3, -4) == "r_n3_n4"


@pytest.mark.parametrize(
    "position, min_y, expected",
    [
        ([0, -64, 0], -64, 0),
        ([1, 0, 1], 0, 513),
        ([1, 2, 3], 0, 2 * 512 * 512 + 3 * 512 + 1),
    ],
)
def test_instance_ids_subtract_the_dimension_floor(position, min_y, expected):
    assert instance_ids(np.array([position]), min_y).tolist() == [expected]


def test_instance_ids_are_int64_for_any_input_dtype():
    assert instance_ids(np.array([[1, 2, 3]], dtype=np.float32), 0).dtype == np.int64


def test_unit_cube_scales_and_authors_opacity_only_on_request():
    stage = Usd.Stage.CreateInMemory()
    box = define_unit_cube(stage, Sdf.Path("/Box"), (0.1, 0.2, 0.3), size=(2.0, 3.0, 4.0), opacity=0.5)
    points = np.array(box.GetPointsAttr().Get())
    assert points.min(axis=0).tolist() == [0, 0, 0] and points.max(axis=0).tolist() == [2, 3, 4]
    assert [tuple(v) for v in box.GetExtentAttr().Get()] == [(0, 0, 0), (2, 3, 4)]
    assert box.GetSubdivisionSchemeAttr().Get() == UsdGeom.Tokens.none
    assert box.GetDisplayColorPrimvar().GetInterpolation() == UsdGeom.Tokens.constant
    assert list(box.GetDisplayColorPrimvar().Get()[0]) == pytest.approx([0.1, 0.2, 0.3])
    assert box.GetDisplayOpacityPrimvar().GetInterpolation() == UsdGeom.Tokens.constant
    assert list(box.GetDisplayOpacityPrimvar().Get()) == pytest.approx([0.5])

    plain = define_unit_cube(stage, Sdf.Path("/Plain"), (0.1, 0.2, 0.3))
    assert np.array(plain.GetPointsAttr().Get()).max(axis=0).tolist() == [1, 1, 1]
    assert not plain.GetDisplayOpacityPrimvar().HasAuthoredValue()
