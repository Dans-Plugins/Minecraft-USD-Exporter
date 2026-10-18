"""Terrain writer.

Writes the base terrain layer (always ``terrain.usdc``): the prototype library
and one ``UsdGeomPointInstancer`` per region. Each finished region is saved and
the layer reloaded, so a region's instance arrays leave memory once written.

Layout (see ARCHITECTURE.md § Stage / prim layout)::

    /World                              Xform, kind = assembly
      BlockLibrary                      class (abstract), mcusd:blockIds = [...]
        stone                           Mesh unit cube, displayColor, mcusd:namespacedId
      Terrain                           Xform, kind = group
        r_<x>_<z>                       Xform, kind = group, translate = region origin
          Blocks                        PointInstancer (region-local positions)
            Prototypes                  Scope
              stone                     inherits </World/BlockLibrary/stone>

Negative region coordinates are written with an ``n`` prefix (``r_n1_0``) because
``-`` is not valid in a USD prim name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from pxr import Gf, Kind, Sdf, Usd, UsdGeom, Vt

from mcusd.blocks import BlockCatalogue, BlockIdTable
from mcusd.cull import CullResult
from mcusd.select import REGION_BLOCKS
from mcusd.world.layout import Dimension

TERRAIN_FILENAME = "terrain.usdc"
WORLD = Sdf.Path("/World")
LIBRARY = WORLD.AppendChild("BlockLibrary")
TERRAIN = WORLD.AppendChild("Terrain")

# Unit cube with its minimum corner at the origin; faces wound counter-clockwise
# seen from outside (right-handed, outward normals).
CUBE_POINTS = [
    (0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
    (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1),
]
CUBE_FACE_COUNTS = [4] * 6
CUBE_FACE_INDICES = [
    0, 3, 2, 1,  # -Z
    4, 5, 6, 7,  # +Z
    0, 1, 5, 4,  # -Y
    3, 7, 6, 2,  # +Y
    0, 4, 7, 3,  # -X
    1, 2, 6, 5,  # +X
]


def region_prim_name(region_x: int, region_z: int) -> str:
    def part(v: int) -> str:
        return f"n{-v}" if v < 0 else str(v)

    return f"r_{part(region_x)}_{part(region_z)}"


def instance_ids(local_positions: np.ndarray, min_y: int) -> np.ndarray:
    """Stable per-instance IDs: ``(y - min_y) * 512 * 512 + z * 512 + x`` (region-local x, z)."""
    x = local_positions[:, 0].astype(np.int64)
    y = local_positions[:, 1].astype(np.int64)
    z = local_positions[:, 2].astype(np.int64)
    return (y - min_y) * REGION_BLOCKS * REGION_BLOCKS + z * REGION_BLOCKS + x


def define_unit_cube(stage: Usd.Stage, path: Sdf.Path, color, *, size=(1.0, 1.0, 1.0),
                     opacity: float | None = None) -> UsdGeom.Mesh:
    """A cube mesh spanning ``[0, size]`` on each axis with a constant displayColor."""
    sx, sy, sz = size
    mesh = UsdGeom.Mesh.Define(stage, path)
    points = [Gf.Vec3f(x * sx, y * sy, z * sz) for x, y, z in CUBE_POINTS]
    mesh.CreatePointsAttr(Vt.Vec3fArray(points))
    mesh.CreateFaceVertexCountsAttr(Vt.IntArray(CUBE_FACE_COUNTS))
    mesh.CreateFaceVertexIndicesAttr(Vt.IntArray(CUBE_FACE_INDICES))
    mesh.CreateExtentAttr(Vt.Vec3fArray([Gf.Vec3f(0, 0, 0), Gf.Vec3f(sx, sy, sz)]))
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
    mesh.CreateDisplayColorPrimvar(UsdGeom.Tokens.constant).Set(Vt.Vec3fArray([Gf.Vec3f(*color)]))
    if opacity is not None:
        mesh.CreateDisplayOpacityPrimvar(UsdGeom.Tokens.constant).Set(Vt.FloatArray([opacity]))
    return mesh


def set_stage_metadata(stage: Usd.Stage) -> None:
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)


@dataclass
class TerrainStats:
    path: Path
    regions: int = 0
    instances: int = 0
    prototypes: int = 0
    per_region: dict[tuple[int, int], int] = field(default_factory=dict)


class TerrainWriter:
    """Streams culled regions into ``terrain.usdc``; call :meth:`close` to save."""

    def __init__(self, out_dir: str | Path, catalogue: BlockCatalogue, table: BlockIdTable,
                 dimension: Dimension):
        self.path = Path(out_dir) / TERRAIN_FILENAME
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.path.unlink()
        self.catalogue = catalogue
        self.table = table
        self.dimension = dimension
        self.layer = Sdf.Layer.CreateNew(str(self.path))
        self.stage = Usd.Stage.Open(self.layer)
        set_stage_metadata(self.stage)
        world = UsdGeom.Xform.Define(self.stage, WORLD)
        Usd.ModelAPI(world).SetKind(Kind.Tokens.assembly)
        self.stage.SetDefaultPrim(world.GetPrim())
        self.stage.CreateClassPrim(LIBRARY)
        terrain = UsdGeom.Xform.Define(self.stage, TERRAIN)
        Usd.ModelAPI(terrain).SetKind(Kind.Tokens.group)
        self._library_entries: set[int] = set()
        self.stats = TerrainStats(self.path)

    def _library_path(self, table_index: int) -> Sdf.Path:
        info = self.catalogue.info(self.table[table_index])
        path = LIBRARY.AppendChild(info.prim_name)
        if table_index not in self._library_entries:
            mesh = define_unit_cube(self.stage, path, info.color)
            mesh.GetPrim().CreateAttribute("mcusd:namespacedId", Sdf.ValueTypeNames.String).Set(info.block_id)
            self._library_entries.add(table_index)
        return path

    def add_region(self, result: CullResult) -> None:
        if result.kept == 0:
            return
        rx, rz = result.region_x, result.region_z
        origin = np.array([rx * REGION_BLOCKS, 0, rz * REGION_BLOCKS], dtype=np.int64)
        local = result.positions.astype(np.int64) - origin
        xz = local[:, [0, 2]]
        if xz.min(initial=0) < 0 or xz.max(initial=0) >= REGION_BLOCKS:
            raise ValueError(f"region ({rx}, {rz}) has positions outside the region")

        region_path = TERRAIN.AppendChild(region_prim_name(rx, rz))
        region = UsdGeom.Xform.Define(self.stage, region_path)
        Usd.ModelAPI(region).SetKind(Kind.Tokens.group)
        region.AddTranslateOp(UsdGeom.XformOp.PrecisionDouble).Set(
            Gf.Vec3d(float(origin[0]), 0.0, float(origin[2])))

        instancer = UsdGeom.PointInstancer.Define(self.stage, region_path.AppendChild("Blocks"))
        prototypes_scope = UsdGeom.Scope.Define(self.stage, instancer.GetPath().AppendChild("Prototypes"))
        used = np.unique(result.block_ids)
        targets = []
        for table_index in used.tolist():
            library_path = self._library_path(table_index)
            proto = self.stage.DefinePrim(prototypes_scope.GetPath().AppendChild(library_path.name))
            proto.GetInherits().AddInherit(library_path)
            targets.append(proto.GetPath())
        instancer.CreatePrototypesRel().SetTargets(targets)

        proto_indices = np.searchsorted(used, result.block_ids).astype(np.int32)
        positions = local.astype(np.float32)
        instancer.CreateProtoIndicesAttr(Vt.IntArray.FromNumpy(proto_indices))
        instancer.CreatePositionsAttr(Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(positions)))
        instancer.CreateIdsAttr(Vt.Int64Array.FromNumpy(instance_ids(local, self.dimension.min_y)))
        primvar = UsdGeom.PrimvarsAPI(instancer).CreatePrimvar(
            "mcusd:blockId", Sdf.ValueTypeNames.IntArray, UsdGeom.Tokens.vertex)
        primvar.Set(Vt.IntArray.FromNumpy(result.block_ids.astype(np.int32)))
        lo = positions.min(axis=0)
        hi = positions.max(axis=0) + 1.0
        instancer.CreateExtentAttr(Vt.Vec3fArray([Gf.Vec3f(*lo.tolist()), Gf.Vec3f(*hi.tolist())]))

        self.stats.regions += 1
        self.stats.instances += result.kept
        self.stats.per_region[(rx, rz)] = result.kept
        self._flush()

    def _flush(self) -> None:
        """Persist what has been authored and release its in-memory array storage.

        Saving a crate layer appends the new data to the file, and reloading
        replaces the in-memory values with lazy, file-backed ones, so memory stays
        bounded by one region instead of growing with the whole export.
        """
        self.layer.Save()
        self.layer.Reload()

    def close(self) -> TerrainStats:
        self.stage.GetPrimAtPath(LIBRARY).CreateAttribute("mcusd:blockIds", Sdf.ValueTypeNames.StringArray).Set(
            Vt.StringArray(list(self.table.ids)))
        self.stats.prototypes = len(self._library_entries)
        self.layer.Save()
        return self.stats
