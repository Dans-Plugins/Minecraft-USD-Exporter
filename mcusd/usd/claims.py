"""Claims writer.

Writes the Medieval Factions claims overlay layer: ``overlays/claims.usda``
(``overlays/claims.usdc`` with ``--binary``) defining ``/World/Overlays/Claims``.
Per faction there is one ``Xform`` (faction name and ID in ``customData``,
``primvars:displayColor`` = faction colour) holding a ``Chunks`` PointInstancer
whose single prototype is a translucent 16 × H × 16 box (``displayOpacity``
0.35), with one instance per claimed chunk. H spans the export's Y range and each
box's minimum corner is at block ``(16x, ymin, 16z)``.

Box positions are absolute block coordinates stored as ``point3f``; they are
multiples of 16, which 32-bit floats represent exactly up to 2**28, beyond the
±30,000,000 world border.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from mcusd.overlays.claims_input import Claim, ClaimsData
from mcusd.select import BBox
from mcusd.usd.terrain import WORLD, define_unit_cube, set_stage_metadata

OVERLAYS = WORLD.AppendChild("Overlays")
CLAIMS = OVERLAYS.AppendChild("Claims")
CLAIM_OPACITY = 0.35
CHUNK = 16


def claims_filename(binary: bool) -> str:
    return "claims.usdc" if binary else "claims.usda"


def faction_prim_name(faction_id: str) -> str:
    """``faction_`` + an injective escape of the ID (``[A-Za-z0-9]`` kept, other bytes ``_XX``)."""
    keep = set(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
    return "faction_" + "".join(chr(b) if b in keep else f"_{b:02x}" for b in faction_id.encode("utf-8"))


@dataclass
class ClaimsStats:
    path: Path
    factions: int = 0
    claims: int = 0
    per_faction: dict[str, int] = field(default_factory=dict)


def claims_in_bbox(claims: list[Claim], bbox: BBox | None) -> list[Claim]:
    """Claims whose chunk overlaps the box in X/Z (all claims when ``bbox`` is None)."""
    if bbox is None:
        return list(claims)
    return [c for c in claims
            if c.x * CHUNK <= bbox.x2 and c.x * CHUNK + CHUNK - 1 >= bbox.x1
            and c.z * CHUNK <= bbox.z2 and c.z * CHUNK + CHUNK - 1 >= bbox.z1]


def write_claims_layer(out_dir: str | Path, data: ClaimsData, claims: list[Claim], *,
                       min_y: int, max_y: int, binary: bool = False) -> ClaimsStats:
    """Write the claims overlay for ``claims`` (already filtered to one world)."""
    path = Path(out_dir) / "overlays" / claims_filename(binary)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    layer = Sdf.Layer.CreateNew(str(path))
    stage = Usd.Stage.Open(layer)
    set_stage_metadata(stage)
    world = UsdGeom.Xform.Define(stage, WORLD)
    stage.SetDefaultPrim(world.GetPrim())
    UsdGeom.Scope.Define(stage, OVERLAYS)
    UsdGeom.Xform.Define(stage, CLAIMS)
    height = max_y - min_y + 1
    stats = ClaimsStats(path)

    by_faction: dict[str, list[Claim]] = {}
    for claim in claims:
        by_faction.setdefault(claim.faction_id, []).append(claim)
    for faction_id in sorted(by_faction):
        faction = data.faction(faction_id)
        chunks = sorted({(c.x, c.z) for c in by_faction[faction_id]}, key=lambda p: (p[1], p[0]))
        faction_path = CLAIMS.AppendChild(faction_prim_name(faction_id))
        xform = UsdGeom.Xform.Define(stage, faction_path)
        prim = xform.GetPrim()
        prim.SetCustomDataByKey("mcusd:factionId", faction.id)
        prim.SetCustomDataByKey("mcusd:factionName", faction.name)
        color = Vt.Vec3fArray([Gf.Vec3f(*faction.color)])
        UsdGeom.PrimvarsAPI(prim).CreatePrimvar(
            "displayColor", Sdf.ValueTypeNames.Color3fArray, UsdGeom.Tokens.constant).Set(color)

        instancer = UsdGeom.PointInstancer.Define(stage, faction_path.AppendChild("Chunks"))
        box_path = instancer.GetPath().AppendChild("Prototypes").AppendChild("Box")
        UsdGeom.Scope.Define(stage, box_path.GetParentPath())
        define_unit_cube(stage, box_path, faction.color, size=(CHUNK, height, CHUNK), opacity=CLAIM_OPACITY)
        instancer.CreatePrototypesRel().SetTargets([box_path])
        positions = np.array([[x * CHUNK, min_y, z * CHUNK] for x, z in chunks], dtype=np.float32)
        instancer.CreatePositionsAttr(Vt.Vec3fArray.FromNumpy(positions))
        instancer.CreateProtoIndicesAttr(Vt.IntArray.FromNumpy(np.zeros(len(chunks), dtype=np.int32)))
        lo = positions.min(axis=0)
        hi = positions.max(axis=0) + np.array([CHUNK, height, CHUNK], dtype=np.float32)
        instancer.CreateExtentAttr(Vt.Vec3fArray([Gf.Vec3f(*lo.tolist()), Gf.Vec3f(*hi.tolist())]))
        stats.factions += 1
        stats.claims += len(chunks)
        stats.per_faction[faction_id] = len(chunks)
    layer.Save()
    return stats
