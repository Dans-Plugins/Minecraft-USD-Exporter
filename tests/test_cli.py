"""CLI behaviour: errors and exit codes (AC2's CLI half), progress and summary output."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from mcusd.cli import main

FIXTURES = Path(__file__).resolve().parent / "fixtures"
WORLD = FIXTURES / "world-26.3"
BBOX = ["-16", "-64", "-16", "15", "319", "15"]


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
