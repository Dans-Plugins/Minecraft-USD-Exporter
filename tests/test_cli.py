"""CLI behaviour: errors and exit codes (AC2's CLI half), progress and summary output."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from pxr import Usd, UsdGeom

from mcusd.cli import _size, main

FIXTURES = Path(__file__).resolve().parent / "fixtures"
WORLD = FIXTURES / "world-26.3"
BBOX = ["-16", "-64", "-16", "15", "319", "15"]
SLAB = ["-16", "60", "-16", "15", "70", "15"]  # the same four chunks, eleven layers high


def test_not_a_world_exits_nonzero_naming_missing_path(tmp_path, capsys):
    """AC2 (CLI): non-zero exit and a message naming the missing path."""
    folder = tmp_path / "holiday-photos"
    folder.mkdir()
    code = main([str(folder), "--bbox", *BBOX, "--out", str(tmp_path / "out")])
    err = capsys.readouterr().err
    assert code == 2
    assert str(folder / "level.dat") in err


def test_missing_dimension_exits_nonzero(tmp_path, capsys):
    code = main([str(WORLD), "--dimension", "the_nether", "--bbox", *BBOX, "--out", str(tmp_path / "out")])
    assert code == 2
    assert "dimensions/minecraft/the_nether/region" in capsys.readouterr().err


def test_console_script_reports_progress_and_summary(tmp_path):
    exe = Path(sys.executable).parent / "mc-usd-export"
    result = subprocess.run([str(exe), str(WORLD), "--bbox", *BBOX, "--out", str(tmp_path / "out")],
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr
    assert "region r.0.0:" in result.stdout
    assert "visible blocks (instances):" in result.stdout
    assert "world layout: 26.1+" in result.stdout
    assert "terrain.usdc" in result.stdout


def test_quiet_and_summary_json(tmp_path, capsys):
    summary = tmp_path / "summary.json"
    code = main([str(WORLD), "--bbox", *BBOX, "--out", str(tmp_path / "out"), "--quiet",
                 "--summary-json", str(summary)])
    assert code == 0
    assert capsys.readouterr().out == ""
    data = json.loads(summary.read_text())
    assert set(data["files"]) == {"world.usda", "terrain.usdc"}
    assert data["instances"] == sum(r["kept"] for r in data["regions"])


def test_bbox_corners_in_any_order(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    main([str(WORLD), "--bbox", "-5", "40", "-5", "5", "80", "5", "--out", str(tmp_path / "a"), "-q",
          "--summary-json", str(a)])
    main([str(WORLD), "--bbox", "5", "80", "5", "-5", "40", "-5", "--out", str(tmp_path / "b"), "-q",
          "--summary-json", str(b)])
    assert json.loads(a.read_text())["instances"] == json.loads(b.read_text())["instances"] > 0


def write_claims(tmp_path, worlds):
    claims = [{"worldId": w, "x": 0, "z": 0, "factionId": "f"} for w in worlds]
    path = tmp_path / "claims.json"
    path.write_text(json.dumps(claims))
    return path


def test_multi_world_claims_need_world_uuid(tmp_path, capsys):
    claims = write_claims(tmp_path, ["00000000-0000-4000-8000-000000000001",
                                     "00000000-0000-4000-8000-000000000002"])
    code = main([str(WORLD), "--bbox", *BBOX, "--out", str(tmp_path / "out"), "--claims", str(claims)])
    assert code == 2 and "--world-uuid" in capsys.readouterr().err
    code = main([str(WORLD), "--bbox", *BBOX, "--out", str(tmp_path / "out"), "-q", "--claims", str(claims),
                 "--world-uuid", "00000000-0000-4000-8000-000000000002"])
    assert code == 0


def test_world_uuid_without_claims_is_an_error(tmp_path, capsys):
    code = main([str(WORLD), "--bbox", *BBOX, "--out", str(tmp_path / "out"),
                 "--world-uuid", "00000000-0000-4000-8000-000000000001"])
    assert code == 2 and "--claims" in capsys.readouterr().err


def test_output_inside_world_folder_is_refused(tmp_path, capsys):
    code = main([str(WORLD), "--bbox", *BBOX, "--out", str(WORLD / "export")])
    assert code == 2 and "never writes there" in capsys.readouterr().err
    assert not (WORLD / "export").exists()


def test_bad_block_overrides_are_an_input_error(tmp_path, capsys):
    overrides = tmp_path / "o.json"
    overrides.write_text('{"minecraft:stone": {"colour": "#fff"}}')
    code = main([str(WORLD), "--bbox", *BBOX, "--out", str(tmp_path / "out"),
                 "--block-overrides", str(overrides)])
    assert code == 2 and "unknown keys" in capsys.readouterr().err


@pytest.mark.parametrize(
    "content, message",
    [
        ('{"minecraft:stone": 3}', "expected a JSON object"),
        ('{"minecraft:stone": null}', "expected a JSON object"),
        ('{"minecraft:stone": []}', "expected a JSON object"),
        ('{"minecraft:stone": {"color": 123}}', "must be a '#rrggbb' string"),
        ('{"minecraft:stone": {"color": null}}', "must be a '#rrggbb' string"),
    ],
)
def test_wrongly_typed_block_overrides_are_an_input_error(tmp_path, capsys, content, message):
    """Valid JSON with the wrong shape exits 2 with a message, and nothing is written."""
    overrides = tmp_path / "o.json"
    overrides.write_text(content)
    out = tmp_path / "out"
    code = main([str(WORLD), "--bbox", *SLAB, "--out", str(out), "--block-overrides", str(overrides)])
    err = capsys.readouterr().err
    assert code == 2
    assert "override for minecraft:stone" in err and message in err
    assert not out.exists()


def claim_box(out):
    """The stage, instancer and box mesh of the only faction in an exported claims overlay."""
    stage = Usd.Stage.Open(str(out / "overlays" / "claims.usda"))
    inst = next(UsdGeom.PointInstancer(p) for p in stage.Traverse() if p.IsA(UsdGeom.PointInstancer))
    (box_path,) = inst.GetPrototypesRel().GetTargets()
    return stage, inst, UsdGeom.Mesh(stage.GetPrimAtPath(box_path))


@pytest.mark.parametrize(
    "y1, y2, floor, height",
    [
        (300, 500, 300, 20),  # top clamped to 319
        (-100, -60, -64, 5),  # bottom clamped to -64
        (-100, 500, -64, 384),  # both clamped
    ],
)
def test_claim_boxes_span_the_bbox_clamped_to_the_dimension(tmp_path, y1, y2, floor, height):
    claims = write_claims(tmp_path, ["00000000-0000-4000-8000-000000000001"])
    out = tmp_path / "out"
    code = main([str(WORLD), "--bbox", "-16", str(y1), "-16", "15", str(y2), "15", "--out", str(out), "-q",
                 "--claims", str(claims)])
    assert code == 0
    _stage, inst, box = claim_box(out)
    assert [tuple(p) for p in inst.GetPositionsAttr().Get()] == [(0, floor, 0)]
    assert np.array(box.GetPointsAttr().Get()).max(axis=0).tolist() == [16, height, 16]


def test_bbox_above_the_dimension_writes_inside_out_claim_boxes(tmp_path):
    """Current behaviour, not endorsed: no terrain, yet claims get a negative height and exit is 0."""
    claims = write_claims(tmp_path, ["00000000-0000-4000-8000-000000000001"])
    out, summary = tmp_path / "out", tmp_path / "summary.json"
    code = main([str(WORLD), "--bbox", "-16", "400", "-16", "15", "500", "15", "--out", str(out), "-q",
                 "--claims", str(claims), "--summary-json", str(summary)])
    assert code == 0
    data = json.loads(summary.read_text())
    assert (data["instances"], data["claims"]) == (0, 1)
    _stage, inst, box = claim_box(out)
    assert np.array(box.GetPointsAttr().Get())[:, 1].min() == -80
    lo, hi = inst.GetExtentAttr().Get()
    assert (lo[1], hi[1]) == (400, 320)


def test_out_naming_a_file_escapes_as_an_exception(tmp_path):
    """Current behaviour, not endorsed: the OSError is not turned into exit status 2."""
    out = tmp_path / "out"
    out.write_text("keep me")
    with pytest.raises(FileExistsError):
        main([str(WORLD), "--bbox", *SLAB, "--out", str(out), "-q"])
    assert out.read_text() == "keep me"


def test_summary_json_in_a_missing_directory_escapes_after_the_export(tmp_path):
    """Current behaviour, not endorsed: the stage is complete, but the OSError is not reported."""
    out = tmp_path / "out"
    with pytest.raises(FileNotFoundError):
        main([str(WORLD), "--bbox", *SLAB, "--out", str(out), "-q",
              "--summary-json", str(tmp_path / "missing" / "summary.json")])
    assert (out / "world.usda").is_file()


def test_block_overrides_change_occlusion(tmp_path):
    """Marking water opaque lets the culler hide the sea floor beneath it."""
    base, over = tmp_path / "base.json", tmp_path / "over.json"
    overrides = tmp_path / "o.json"
    overrides.write_text('{"minecraft:water": {"opaque": true}}')
    main([str(WORLD), "--bbox", *BBOX, "--out", str(tmp_path / "a"), "-q", "--summary-json", str(base)])
    main([str(WORLD), "--bbox", *BBOX, "--out", str(tmp_path / "b"), "-q", "--summary-json", str(over),
          "--block-overrides", str(overrides)])
    assert json.loads(over.read_text())["instances"] < json.loads(base.read_text())["instances"]


def test_unreadable_world_data_exits_1(tmp_path, capsys):
    world = tmp_path / "world"
    (world / "region").mkdir(parents=True)
    (world / "level.dat").write_bytes(b"")
    (world / "region" / "r.0.0.mca").write_bytes(b"\0" * 100)  # truncated header
    code = main([str(world), "--bbox", "0", "0", "0", "15", "15", "15", "--out", str(tmp_path / "out")])
    assert code == 1 and "truncated header" in capsys.readouterr().err


def test_bbox_requires_six_integers(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main([str(WORLD), "--bbox", "1", "2", "3", "--out", "x"])
    assert excinfo.value.code == 2


def test_unreadable_claims_are_an_input_error_and_nothing_is_written(tmp_path, capsys):
    claims = tmp_path / "claims.json"
    claims.write_text("{not json")
    out = tmp_path / "out"
    code = main([str(WORLD), "--bbox", *SLAB, "--out", str(out), "--claims", str(claims)])
    assert code == 2 and "invalid JSON" in capsys.readouterr().err
    assert not out.exists()


def test_missing_block_overrides_file_is_an_input_error(tmp_path, capsys):
    overrides = tmp_path / "missing.json"
    code = main([str(WORLD), "--bbox", *SLAB, "--out", str(tmp_path / "out"),
                 "--block-overrides", str(overrides)])
    assert code == 2 and f"block overrides {overrides}:" in capsys.readouterr().err


def test_claims_warnings_and_summary_are_reported(tmp_path, capsys):
    """Claims outside the bbox are dropped; an unknown faction warns on stderr but still exports."""
    world = "00000000-0000-4000-8000-000000000001"
    doc = {"format": "mcusd-claims", "version": 1,
           "factions": [{"id": "north", "name": "Northreach", "color": "#3366cc"}],
           "claims": [{"worldId": world, "x": 0, "z": 0, "factionId": "north"},
                      {"worldId": world, "x": 5, "z": 5, "factionId": "north"},
                      {"worldId": world, "x": -1, "z": -1, "factionId": "ghost"}]}
    claims = tmp_path / "claims.json"
    claims.write_text(json.dumps(doc))
    summary = tmp_path / "summary.json"
    code = main([str(WORLD), "--bbox", *SLAB, "--out", str(tmp_path / "out"), "--claims", str(claims),
                 "--summary-json", str(summary)])
    captured = capsys.readouterr()
    assert code == 0
    assert "mc-usd-export: warning: claims reference unknown faction 'ghost'" in captured.err
    assert "claims: 2 chunks for 2 factions" in captured.out
    data = json.loads(summary.read_text())
    assert (data["claims"], data["factions"]) == (2, 2)
    assert set(data["files"]) == {"world.usda", "claims.usda", "terrain.usdc"}
    assert len(data["warnings"]) == 1


def test_no_claims_summary_line_without_claims(tmp_path, capsys):
    code = main([str(WORLD), "--bbox", *SLAB, "--out", str(tmp_path / "out")])
    assert code == 0 and "claims:" not in capsys.readouterr().out


def test_namespaced_dimension_id_is_accepted(tmp_path):
    short, namespaced = tmp_path / "short.json", tmp_path / "namespaced.json"
    assert main([str(WORLD), "--bbox", *SLAB, "--out", str(tmp_path / "a"), "-q",
                 "--summary-json", str(short)]) == 0
    assert main([str(WORLD), "--dimension", "minecraft:overworld", "--bbox", *SLAB, "--out", str(tmp_path / "b"),
                 "-q", "--summary-json", str(namespaced)]) == 0
    assert json.loads(namespaced.read_text())["instances"] == json.loads(short.read_text())["instances"]


def test_unknown_dimension_is_rejected_by_the_parser(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main([str(WORLD), "--dimension", "minecraft:aether", "--bbox", *SLAB, "--out", str(tmp_path / "out")])
    assert excinfo.value.code == 2 and "invalid choice" in capsys.readouterr().err


@pytest.mark.parametrize(
    "size, text",
    [
        (0, "0 B"),
        (1023, "1023 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (5 * 1024**2, "5.0 MB"),
        (3 * 1024**3, "3.0 GB"),
        (2 * 1024**4, "2048.0 GB"),  # GB is the largest unit
    ],
)
def test_size_is_reported_in_binary_units(size, text):
    path = SimpleNamespace(stat=lambda: SimpleNamespace(st_size=size))
    assert _size(path) == text
