"""Stage assembler.

Writes the root layer that sublayers terrain and overlays and sets stage
metadata: ``world.usda`` by default, ``world.usdc`` with ``--binary``. Overlays
are listed before terrain so their opinions are stronger, and sublayer paths
use the extensions actually written.
"""

from __future__ import annotations

import os
from pathlib import Path

from pxr import Sdf, Usd, UsdGeom, Vt

from mcusd import __version__


def root_filename(binary: bool) -> str:
    return "world.usdc" if binary else "world.usda"


def _relative(path: Path, base: Path) -> str:
    return "./" + Path(os.path.relpath(path, base)).as_posix()


def write_root_layer(out_dir: str | Path, *, terrain: Path, overlays: list[Path], source_world: str,
                     dimension: str, bbox: list[int], binary: bool = False) -> Path:
    """Write the root layer composing ``overlays`` (strongest first) over ``terrain``."""
    out_dir = Path(out_dir)
    path = out_dir / root_filename(binary)
    if path.exists():
        path.unlink()
    layer = Sdf.Layer.CreateNew(str(path))
    layer.subLayerPaths = [_relative(p, out_dir) for p in [*overlays, terrain]]
    stage = Usd.Stage.Open(layer)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    layer.defaultPrim = "World"
    layer.customLayerData = {
        "mcusd:sourceWorld": source_world,
        "mcusd:dimension": dimension,
        "mcusd:bbox": Vt.IntArray([int(v) for v in bbox]),
        "mcusd:exporterVersion": __version__,
    }
    layer.Save()
    return path
