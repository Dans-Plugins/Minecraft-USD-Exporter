"""AC6: the terrain layer is valid, region-local, per-instance-ID-carrying instancing."""

import numpy as np
import pytest
from pxr import Gf, Kind, Sdf, Usd, UsdGeom

from mcusd.blocks import BlockCatalogue
from mcusd.cull import cull_region
from mcusd.select import BBox, ChunkSource, iter_region_volumes
from mcusd.usd.terrain import (
    TERRAIN_FILENAME,
    TerrainWriter,
    instance_ids,
    region_prim_name,
)
from mcusd.world.layout import locate
from tests.nbt_builders import random_chunks, write_world


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
