"""AC7, AC9, AC10 (and AC6's CLI count): end-to-end exports of the committed fixture worlds."""

import gzip
import json
from pathlib import Path

import anvil
import numpy as np
import pytest
from pxr import Sdf, Usd, UsdGeom

from mcusd import __version__
from mcusd.cli import main
from mcusd.world.layout import locate
from mcusd.world.region import RegionFile, parse_nbt

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RECORD = json.loads((FIXTURES / "fixtures.json").read_text())["fixtures"]
VERSIONS = sorted(RECORD)
EXAMPLE_CLAIMS = Path(__file__).resolve().parent.parent / "examples" / "claims.json"


def run_export(tmp_path, version, *extra, out="out"):
    record = RECORD[version]
    out_dir = tmp_path / out
    summary = tmp_path / f"{out}-summary.json"
    code = main([str(FIXTURES / record["folder"]), "--bbox", *map(str, record["counts"]["bbox"]),
                 "--out", str(out_dir), "--summary-json", str(summary), "--quiet", *extra])
    assert code == 0
    return out_dir, json.loads(summary.read_text())


def instancers(stage):
    return [UsdGeom.PointInstancer(p) for p in stage.Traverse() if p.IsA(UsdGeom.PointInstancer)
            and p.GetPath().HasPrefix(Sdf.Path("/World/Terrain"))]


def test_both_fixture_versions_are_present():
    assert VERSIONS == ["1.21.11", "26.3"]
    assert {r["layout"] for r in RECORD.values()} == {"pre-26.1", "26.1+"}


@pytest.mark.parametrize("version", VERSIONS)
def test_fixture_contains_only_kept_chunks_and_no_player_data(version):
    world = FIXTURES / RECORD[version]["folder"]
    files = sorted(p.relative_to(world).as_posix() for p in world.rglob("*") if p.is_file())
    region_dir = locate(world, "overworld").region_dir.relative_to(world).as_posix()
    assert files == sorted(["level.dat"] + [f"{region_dir}/r.{rx}.{rz}.mca" for rx in (-1, 0) for rz in (-1, 0)])
    level = parse_nbt(gzip.decompress((world / "level.dat").read_bytes()))
    assert "Player" not in level["Data"]
    chunks = []
    for path in sorted((world / region_dir).glob("*.mca")):
        region = RegionFile(path)
        chunks += [region.chunk_coords(*c) for c in region.present_chunks()]
    assert sorted(chunks) == sorted(tuple(c) for c in RECORD[version]["keptChunks"])


@pytest.mark.parametrize("version", VERSIONS)
def test_fixture_export_matches_recorded_counts(tmp_path, version):
    """AC10: real 1.21.x and 26.x worlds export and match the recorded block counts."""
    out, summary = run_export(tmp_path, version)
    counts = RECORD[version]["counts"]
    assert summary["layout"] == RECORD[version]["layout"]
    assert summary["instances"] == counts["visible"]
    per_region = {f"r.{r['region_x']}.{r['region_z']}": r["kept"] for r in summary["regions"]}
    assert per_region == {k: v["visible"] for k, v in counts["regions"].items()}
    assert summary["chunksDecoded"] == 4 and summary["chunksSkippedPartial"] == 0

    stage = Usd.Stage.Open(str(out / "world.usda"))
    table = list(stage.GetPrimAtPath("/World/BlockLibrary").GetAttribute("mcusd:blockIds").Get())
    placed = {}
    total = 0
    for inst in instancers(stage):
        translate = np.array(UsdGeom.Xformable(inst.GetPrim().GetParent()).GetOrderedXformOps()[0].Get())
        positions = np.array(inst.GetPositionsAttr().Get()) + translate
        block_ids = UsdGeom.PrimvarsAPI(inst).GetPrimvar("mcusd:blockId").Get()
        for p, b in zip(positions.astype(int).tolist(), block_ids):
            placed[tuple(p)] = table[b]
        total += len(positions)
    assert total == summary["instances"]  # AC6: instances == count reported by the CLI
    for sample in counts["samples"]:
        key = (sample["x"], sample["y"], sample["z"])
        if sample["y"] > 0:  # the recorded surface block is always visible
            assert placed[key] == sample["block"]


def test_1_21_samples_agree_with_anvil_parser(tmp_path):
    """Independent oracle: anvil-parser2's own block lookup (1.21.x palettes only)."""
    record = RECORD["1.21.11"]
    region_dir = locate(FIXTURES / record["folder"], "overworld").region_dir
    for sample in record["counts"]["samples"]:
        x, y, z = sample["x"], sample["y"], sample["z"]
        region = RegionFile(region_dir / f"r.{x >> 9}.{z >> 9}.mca")
        chunk = anvil.Chunk(region.read_nbt((x >> 4) & 31, (z >> 4) & 31))
        block = chunk.get_block(x & 15, y, z & 15)
        assert f"{block.namespace}:{block.id}" == sample["block"]


@pytest.mark.parametrize("version", VERSIONS)
def test_root_layer_metadata_and_sublayers(tmp_path, version):
    """AC7: root metadata, sublayer order and provenance."""
    out, _ = run_export(tmp_path, version, "--claims", str(EXAMPLE_CLAIMS))
    layer = Sdf.Layer.FindOrOpen(str(out / "world.usda"))
    assert layer.defaultPrim == "World"
    stage = Usd.Stage.Open(layer)
    assert UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.y
    assert UsdGeom.GetStageMetersPerUnit(stage) == 1.0
    assert list(layer.subLayerPaths) == ["./overlays/claims.usda", "./terrain.usdc"]
    data = layer.customLayerData
    assert data["mcusd:sourceWorld"] == RECORD[version]["folder"]
    assert data["mcusd:dimension"] == "minecraft:overworld"
    assert list(data["mcusd:bbox"]) == RECORD[version]["counts"]["bbox"]
    assert data["mcusd:exporterVersion"] == __version__
    assert (out / "world.usda").read_text().startswith("#usda 1.0")


def test_binary_flag_switches_root_and_overlay_only(tmp_path):
    """AC7 (format): --binary writes world.usdc + overlays/claims.usdc; terrain is .usdc either way."""
    ascii_out, _ = run_export(tmp_path, "26.3", "--claims", str(EXAMPLE_CLAIMS), out="ascii")
    binary_out, _ = run_export(tmp_path, "26.3", "--claims", str(EXAMPLE_CLAIMS), "--binary", out="bin")
    assert sorted(p.relative_to(ascii_out).as_posix() for p in ascii_out.rglob("*") if p.is_file()) == \
        ["overlays/claims.usda", "terrain.usdc", "world.usda"]
    assert sorted(p.relative_to(binary_out).as_posix() for p in binary_out.rglob("*") if p.is_file()) == \
        ["overlays/claims.usdc", "terrain.usdc", "world.usdc"]
    for path in (binary_out / "world.usdc", binary_out / "overlays/claims.usdc",
                 binary_out / "terrain.usdc", ascii_out / "terrain.usdc"):
        assert path.read_bytes()[:8] == b"PXR-USDC"
    layer = Sdf.Layer.FindOrOpen(str(binary_out / "world.usdc"))
    assert list(layer.subLayerPaths) == ["./overlays/claims.usdc", "./terrain.usdc"]
    assert Usd.Stage.Open(layer).GetPrimAtPath("/World/Overlays/Claims").IsValid()


def test_no_claims_means_terrain_only_sublayer(tmp_path):
    out, summary = run_export(tmp_path, "1.21.11")
    layer = Sdf.Layer.FindOrOpen(str(out / "world.usda"))
    assert list(layer.subLayerPaths) == ["./terrain.usdc"]
    assert not (out / "overlays").exists()
    assert summary["claims"] == 0


def test_muting_the_overlay_removes_claims_and_keeps_terrain(tmp_path):
    """AC9: composition — the overlay shows, and muting its layer removes it cleanly."""
    out, _ = run_export(tmp_path, "26.3", "--claims", str(EXAMPLE_CLAIMS))
    stage = Usd.Stage.Open(str(out / "world.usda"))

    def terrain_state():
        return {str(i.GetPath()): len(i.GetPositionsAttr().Get()) for i in instancers(stage)}

    before = terrain_state()
    claims = stage.GetPrimAtPath("/World/Overlays/Claims")
    assert claims.IsValid() and len(claims.GetChildren()) == 2
    overlay = Sdf.Layer.FindOrOpen(str(out / "overlays" / "claims.usda"))
    stage.MuteLayer(overlay.identifier)
    assert not stage.GetPrimAtPath("/World/Overlays/Claims").IsValid()
    assert not stage.GetPrimAtPath("/World/Overlays").IsValid()
    assert terrain_state() == before and before
    stage.UnmuteLayer(overlay.identifier)
    assert stage.GetPrimAtPath("/World/Overlays/Claims").IsValid()


def test_claims_overlay_boxes_line_up_with_fixture_chunks(tmp_path):
    out, summary = run_export(tmp_path, "1.21.11", "--claims", str(EXAMPLE_CLAIMS))
    assert summary["claims"] == 4 and summary["factions"] == 2
    stage = Usd.Stage.Open(str(out / "world.usda"))
    corners = set()
    for faction in stage.GetPrimAtPath("/World/Overlays/Claims").GetChildren():
        inst = UsdGeom.PointInstancer(faction.GetChild("Chunks"))
        corners |= {tuple(int(v) for v in p) for p in inst.GetPositionsAttr().Get()}
    assert corners == {(-16, -64, -16), (0, -64, -16), (-16, -64, 0), (0, -64, 0)}
