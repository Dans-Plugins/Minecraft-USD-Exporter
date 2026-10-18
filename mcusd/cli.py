"""CLI.

Parses arguments, wires the pipeline, reports progress and a summary::

    mc-usd-export WORLD --bbox X1 Y1 Z1 X2 Y2 Z2 --out DIR
                  [--dimension overworld|the_nether|the_end]
                  [--claims PATH] [--world-uuid UUID]
                  [--block-overrides PATH] [--binary] [--quiet]

Exit status: 0 on success, 2 for invalid input (bad arguments, not a world,
unreadable claims or overrides), 1 when the world data itself cannot be read.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from mcusd import __version__
from mcusd.blocks import BlockCatalogue
from mcusd.cull import cull_region
from mcusd.overlays.claims_input import ClaimsError, load_claims
from mcusd.select import BBox, ChunkSource, iter_region_volumes
from mcusd.usd.claims import claims_in_bbox, write_claims_layer
from mcusd.usd.stage import write_root_layer
from mcusd.usd.terrain import TerrainWriter
from mcusd.world.chunk import ChunkFormatError
from mcusd.world.layout import DIMENSIONS, WorldLayoutError, locate
from mcusd.world.region import RegionError


class InputError(Exception):
    """Invalid user input (exit status 2)."""


@dataclass
class RegionSummary:
    region_x: int
    region_z: int
    chunks: int
    kept: int
    culled: int
    air: int


@dataclass
class ExportResult:
    root: Path
    terrain: Path
    overlays: list[Path]
    layout: str
    instances: int = 0
    culled: int = 0
    air: int = 0
    blocks_scanned: int = 0
    chunks_decoded: int = 0
    chunks_skipped_partial: int = 0
    claims: int = 0
    factions: int = 0
    seconds: float = 0.0
    regions: list[RegionSummary] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def files(self) -> list[Path]:
        return [self.root, *self.overlays, self.terrain]


def export_world(world: str | Path, *, bbox: BBox, out: str | Path, dimension: str = "overworld",
                 claims: str | Path | None = None, world_uuid: str | None = None,
                 block_overrides: str | Path | None = None, binary: bool = False,
                 progress=None) -> ExportResult:
    """Run the whole pipeline. Raises :class:`InputError` for invalid input."""
    started = time.perf_counter()
    world = Path(world)
    out = Path(out)
    try:
        location = locate(world, dimension)
    except WorldLayoutError as exc:
        raise InputError(str(exc)) from None
    if out.resolve().is_relative_to(world.resolve()):
        raise InputError(f"--out must not be inside the world folder ({world}); the exporter never writes there")
    try:
        catalogue = BlockCatalogue.from_file(block_overrides)
    except (OSError, ValueError) as exc:
        raise InputError(f"block overrides {block_overrides}: {exc}") from None
    claims_data = selected_claims = None
    if claims is not None:
        try:
            claims_data = load_claims(claims)
            selected_claims = claims_in_bbox(claims_data.select_world(world_uuid), bbox)
        except ClaimsError as exc:
            raise InputError(str(exc)) from None
    elif world_uuid is not None:
        raise InputError("--world-uuid only applies together with --claims")

    out.mkdir(parents=True, exist_ok=True)
    source = ChunkSource(location.region_dir, location.dimension)
    terrain = TerrainWriter(out, catalogue, source.table, location.dimension)
    result = ExportResult(out, terrain.path, [], location.layout)
    for volume in iter_region_volumes(source, bbox):
        culled = cull_region(volume, catalogue)
        terrain.add_region(culled)
        summary = RegionSummary(volume.region_x, volume.region_z, volume.chunks_present,
                                culled.kept, culled.culled, culled.air)
        result.regions.append(summary)
        result.instances += culled.kept
        result.culled += culled.culled
        result.air += culled.air
        result.blocks_scanned += volume.core.volume
        if progress:
            progress(f"region r.{summary.region_x}.{summary.region_z}: {summary.chunks} chunks, "
                     f"{summary.kept} visible, {summary.culled} hidden")
    terrain.close()
    result.chunks_decoded = source.chunks_decoded
    result.chunks_skipped_partial = source.chunks_skipped_partial

    if claims_data is not None:
        dim = location.dimension
        stats = write_claims_layer(out, claims_data, selected_claims,
                                   min_y=max(bbox.y1, dim.min_y), max_y=min(bbox.y2, dim.max_y), binary=binary)
        result.overlays.append(stats.path)
        result.claims, result.factions = stats.claims, stats.factions
        result.warnings.extend(claims_data.warnings)

    result.root = write_root_layer(out, terrain=terrain.path, overlays=result.overlays,
                                   source_world=world.resolve().name, dimension=location.dimension.namespaced_id,
                                   bbox=bbox.as_list(), binary=binary)
    result.seconds = time.perf_counter() - started
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mc-usd-export",
        description=(
            "Export a bounding box of a Minecraft Java Edition world as a "
            "layered OpenUSD stage. The world is only read; export from a copy "
            "or a stopped server."
        ),
    )
    parser.add_argument("world", type=Path, help="world folder (the one containing level.dat)")
    parser.add_argument("--dimension", default="overworld",
                        choices=[*DIMENSIONS, *(d.namespaced_id for d in DIMENSIONS.values())],
                        help="dimension to export (default: overworld)")
    parser.add_argument("--bbox", type=int, nargs=6, required=True,
                        metavar=("X1", "Y1", "Z1", "X2", "Y2", "Z2"),
                        help="inclusive block-space box, any two opposite corners")
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    parser.add_argument("--claims", type=Path,
                        help="claims input: mcusd-claims JSON, a saved GET /api/claims array, "
                             "or a Medieval Factions JSON-storage directory")
    parser.add_argument("--world-uuid", help="world UUID to take claims for (required when the "
                                             "claims cover more than one world)")
    parser.add_argument("--block-overrides", type=Path,
                        help='JSON mapping block IDs to {"color": "#rrggbb", "opaque": true|false}')
    parser.add_argument("--binary", action="store_true",
                        help="write the root and overlay layers as .usdc (terrain is always .usdc)")
    parser.add_argument("--summary-json", type=Path, help="also write the export summary as JSON")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print errors and warnings")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _size(path: Path) -> str:
    size = path.stat().st_size
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return str(size)


def summary_dict(result: ExportResult) -> dict:
    return {
        "exporterVersion": __version__,
        "layout": result.layout,
        "instances": result.instances,
        "hidden": result.culled,
        "air": result.air,
        "blocksScanned": result.blocks_scanned,
        "chunksDecoded": result.chunks_decoded,
        "chunksSkippedPartial": result.chunks_skipped_partial,
        "claims": result.claims,
        "factions": result.factions,
        "seconds": round(result.seconds, 3),
        "regions": [vars(r) for r in result.regions],
        "files": {p.name: p.stat().st_size for p in result.files()},
        "warnings": result.warnings,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    say = (lambda message: None) if args.quiet else (lambda message: print(message, flush=True))
    try:
        result = export_world(args.world, bbox=BBox.from_corners(*args.bbox), out=args.out,
                              dimension=args.dimension, claims=args.claims, world_uuid=args.world_uuid,
                              block_overrides=args.block_overrides, binary=args.binary, progress=say)
    except InputError as exc:
        print(f"mc-usd-export: error: {exc}", file=sys.stderr)
        return 2
    except (RegionError, ChunkFormatError) as exc:
        print(f"mc-usd-export: error reading world: {exc}", file=sys.stderr)
        return 1
    for warning in result.warnings:
        print(f"mc-usd-export: warning: {warning}", file=sys.stderr)
    rate = result.blocks_scanned / result.seconds if result.seconds else 0.0
    say(f"world layout: {result.layout}")
    say(f"visible blocks (instances): {result.instances}")
    say(f"hidden blocks culled: {result.culled}; air dropped: {result.air}")
    say(f"chunks decoded: {result.chunks_decoded} ({result.chunks_skipped_partial} not fully generated, skipped)")
    if result.overlays:
        say(f"claims: {result.claims} chunks for {result.factions} factions")
    say(f"time: {result.seconds:.2f} s ({rate / 1e6:.2f} M blocks/s scanned)")
    for path in result.files():
        say(f"wrote {path} ({_size(path)})")
    if args.summary_json:
        args.summary_json.write_text(json.dumps(summary_dict(result), indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
