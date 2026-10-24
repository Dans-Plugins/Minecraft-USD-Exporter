"""The pure-Python parts of scripts/render_strata_preview.py (headless Strata previews).

Nothing here loads Strata, usd-web or Pillow: meshing, claim tint and borders, the
pinned-library checks, the blank-image check and the framing are tested on their
own. The CI job "Strata previews" renders real images.
"""

import hashlib
import importlib.util
import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest
from pxr import Usd

from mcusd.cli import main as export_main

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("render_strata_preview", ROOT / "scripts" / "render_strata_preview.py")
rsp = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rsp  # dataclasses look their module up
SPEC.loader.exec_module(rsp)

HOLE = np.iinfo(np.int32).min


def quad(d, x, z, y0, du, dv, color=0):
    return np.array([[d, x, z, y0, du, dv, color]], dtype=np.int64)


# ------------------------------------------------------------------ meshing


def test_top_quad_becomes_two_upward_triangles_in_world_space():
    pos, nor, col, idx = rsp.quads_to_mesh(quad(0, 1, 2, 64, 3, 2), 100, -50, np.array([[0.1, 0.2, 0.3]]))
    assert pos.shape == (4, 3) and idx.tolist() == [0, 1, 2, 0, 2, 3]
    assert set(map(tuple, pos[:, [0, 2]].tolist())) == {(101, -48), (101, -46), (104, -46), (104, -48)}
    assert (pos[:, 1] == 64).all()
    assert (nor == [0, 1, 0]).all()
    assert np.allclose(col, [[0.1, 0.2, 0.3]] * 4)
    for t in idx.reshape(-1, 3):
        a, b, c = pos[t]
        assert np.cross(b - a, c - a)[1] > 0  # counter-clockwise seen from above


@pytest.mark.parametrize("d, normal", [(1, (1, 0, 0)), (2, (-1, 0, 0)), (3, (0, 0, 1)), (4, (0, 0, -1))])
def test_side_quads_wind_counter_clockwise_from_outside(d, normal):
    pos, nor, _, idx = rsp.quads_to_mesh(quad(d, 4, 4, 60, 2, 3), 0, 0, np.zeros((1, 3)))
    assert (nor == normal).all()
    for t in idx.reshape(-1, 3):
        a, b, c = pos[t]
        assert np.dot(np.cross(b - a, c - a), normal) > 0
    # a side face spans du along its plane and dv up
    assert pos[:, 1].min() == 60 and pos[:, 1].max() == 63


def test_quad_cells_put_plus_faces_in_the_column_behind_the_plane():
    q = np.concatenate([quad(0, 5, 5, 0, 1, 1), quad(1, 16, 5, 0, 1, 1), quad(2, 16, 5, 0, 1, 1),
                        quad(3, 5, 16, 0, 1, 1), quad(4, 5, 16, 0, 1, 1)])
    cx, cz = rsp.quad_cells(q, 0, 0)
    assert cx.tolist() == [5, 15, 16, 5, 5]
    assert cz.tolist() == [5, 5, 5, 15, 16]


def test_align_to_chunks_pads_the_front_with_holes():
    h = np.full((2, 3), 70, dtype=np.int64)
    c = np.ones((2, 3), dtype=np.int64)
    h2, c2, s2, origin = rsp.align_to_chunks(h, c, c.copy(), (-5, 3), HOLE)
    assert origin == (-16, 0)
    assert h2.shape == (5, 14)
    assert (h2[3:, 11:] == 70).all() and (h2[:3] == HOLE).all() and (h2[:, :11] == HOLE).all()
    assert rsp.align_to_chunks(h, c, c, (32, -16), HOLE)[3] == (32, -16)


# ------------------------------------------------------------------ claims


def factions():
    return [rsp.Faction("North", (1.0, 0.0, 0.0), {(0, 0), (1, 0)}),
            rsp.Faction("South", (0.0, 0.0, 1.0), {(0, 1)})]


def test_tint_moves_only_claimed_blocks_toward_the_faction_colour():
    colors = np.array([[0.5, 0.5, 0.5]] * 3, dtype=np.float32)
    out = rsp.tint_colors(colors, np.array([0, 0, 5]), np.array([0, 1, 5]), factions(), amount=0.5)
    assert np.allclose(out[0], [0.75, 0.25, 0.25])
    assert np.allclose(out[1], [0.25, 0.25, 0.75])
    assert np.allclose(out[2], colors[2])
    assert np.allclose(rsp.tint_colors(colors, np.zeros(3), np.zeros(3), []), colors)


def test_border_edges_skip_edges_shared_with_the_same_faction():
    f = factions()
    owner = rsp.chunk_owner(f)
    north = rsp.border_edges(f[0].chunks, owner, 0)
    assert len(north) == 6  # two chunks side by side: 8 edges minus the shared one, counted twice
    assert (16, 0, 16, 16) not in north
    assert (0, 16, 16, 16) in north  # bordering South is still an outer edge
    assert (0, 16, 16, 16) in rsp.border_edges(f[1].chunks, owner, 1)


def test_border_polyline_follows_the_terrain_with_risers_and_gaps():
    flat = rsp.border_polyline((0, 0, 16, 0), lambda x, z: 70.0, lift=0.5)
    assert len(flat) == 17 and {p[1] for p in flat} == {70.5}
    assert flat[0] == (0, 70.5, 0) and flat[-1] == (16, 70.5, 0)

    stepped = rsp.border_polyline((0, 0, 0, 4), lambda x, z: 70.0 if z < 2 else 72.0, lift=0.0)
    assert (0, 70.0, 2) in stepped and (0, 72.0, 2) in stepped  # the riser
    segs = rsp.polyline_segments(stepped)
    assert ((0, 70.0, 2), (0, 72.0, 2)) in segs

    holes = rsp.border_polyline((0, 0, 4, 0), lambda x, z: None if x == 1 else 64.0, lift=0.0)
    assert None in holes
    assert all(a[0] != 1 or b[0] != 2 for a, b in rsp.polyline_segments(holes))


def test_claim_lines_are_line_vertex_records_in_faction_colour():
    lines = rsp.claim_lines(factions(), lambda x, z: 64.0, layers=(1.0,))
    assert lines.dtype == rsp.LINE_VERTEX and lines.dtype.itemsize == 28
    assert len(lines) % 2 == 0 and len(lines) == 2 * 16 * (6 + 4)
    assert {tuple(c) for c in lines["color"].tolist()} == {(1.0, 0.0, 0.0, 1.0), (0.0, 0.0, 1.0, 1.0)}
    assert (lines["position"][:, 1] == 65.0).all()


def test_read_claims_from_an_exported_overlay(tmp_path):
    out = tmp_path / "out"
    assert export_main([str(ROOT / "tests/fixtures/world-26.3"),
                        "--bbox", "-16", "-64", "-16", "15", "319", "15", "--claims", str(ROOT / "examples/claims.json"),
                        "--out", str(out), "--quiet"]) == 0
    found = {f.name: f for f in rsp.read_claims(Usd.Stage.Open(str(out / "world.usda")))}
    data = json.loads((ROOT / "examples/claims.json").read_text())
    names = {f["id"]: f["name"] for f in data["factions"]}
    assert set(found) == set(names.values())
    for f in data["factions"]:
        rgb = tuple(int(f["color"][i:i + 2], 16) / 255 for i in (1, 3, 5))
        assert np.allclose(found[f["name"]].color, rgb, atol=1e-6)
        assert found[f["name"]].chunks == {(c["x"], c["z"]) for c in data["claims"] if c["factionId"] == f["id"]}
    assert rsp.read_claims(Usd.Stage.Open(str(out / "terrain.usdc"))) == []


# ------------------------------------------------------------------ the pinned library


def test_the_lock_pins_one_release_asset():
    lock = rsp.read_lock()
    assert lock["url"].endswith(f"/v{lock['version']}/{lock['asset']}")
    assert len(lock["sha256"]) == 64 and int(lock["sha256"], 16) >= 0


def test_verify_sha256_accepts_a_match_and_deletes_a_mismatch(tmp_path):
    f = tmp_path / "lib.so"
    f.write_bytes(b"strata")
    rsp.verify_sha256(f, hashlib.sha256(b"strata").hexdigest())
    with pytest.raises(rsp.PreviewError) as err:
        rsp.verify_sha256(f, "0" * 64)
    assert err.value.code == rsp.EXIT_STRATA and not f.exists()


def test_resolve_library_downloads_once_into_the_cache_and_checks_it(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.delenv("MCUSD_STRATA_LIB", raising=False)
    body = b"not really a library"
    lock = {"version": "9.9.9", "asset": "libstrata.so", "url": "https://example.invalid/libstrata.so",
            "sha256": hashlib.sha256(body).hexdigest()}
    calls = []

    def download(url, dest):
        calls.append(url)
        Path(dest).write_bytes(body)

    path = rsp.resolve_library(None, lock, download=download)
    assert path == tmp_path / "mcusd" / "strata" / "9.9.9" / "libstrata.so" and path.read_bytes() == body
    assert rsp.resolve_library(None, lock, download=download) == path and len(calls) == 1

    path.write_bytes(b"tampered")
    with pytest.raises(rsp.PreviewError):
        rsp.resolve_library(None, lock, download=download)
    assert not path.exists()

    bad = dict(lock, version="1.0.0", sha256="0" * 64)
    with pytest.raises(rsp.PreviewError):
        rsp.resolve_library(None, bad, download=download)
    assert not list((tmp_path / "mcusd" / "strata" / "1.0.0").iterdir())


def test_an_explicit_library_path_must_exist(tmp_path, monkeypatch):
    monkeypatch.delenv("MCUSD_STRATA_LIB", raising=False)
    with pytest.raises(rsp.PreviewError) as err:
        rsp.resolve_library(str(tmp_path / "missing.so"), rsp.read_lock())
    assert err.value.code == rsp.EXIT_STRATA
    monkeypatch.setenv("MCUSD_STRATA_LIB", str(tmp_path / "env.so"))
    with pytest.raises(rsp.PreviewError):
        rsp.resolve_library(None, rsp.read_lock())


def test_struct_layouts_match_the_header_comments():
    # strata.h: StrataRenderItem 44 bytes, StrataLineVertex 28, StrataSolarPosition 32
    assert rsp.STRUCTS[0] == 44 and rsp.STRUCTS[10] == 28 and rsp.STRUCTS[13] == 32


# ------------------------------------------------------------------ blank check, framing, sun


def test_coverage_measures_how_much_differs_from_an_empty_frame():
    empty = rsp.flat_image((10, 10, 4), (0.5, 0.5, 0.5))
    assert empty[0, 0, :3].tolist() == [128, 128, 128]
    assert rsp.coverage(empty, empty) == 0.0
    img = empty.copy()
    img[:3, :] = 0
    img[5, 5, 0] = 133  # within the threshold: noise, not drawing
    assert rsp.coverage(img, empty) == pytest.approx(0.3)


def test_camera_is_fixed_and_looks_from_the_requested_side():
    bounds = (-128, 40, -128, 32, 120, 32)
    a = rsp.camera_pose(bounds, 70, 135, 38, 40, 16 / 9)
    b = rsp.camera_pose(bounds, 70, 135, 38, 40, 16 / 9)
    assert np.array_equal(a[0], b[0])
    eye, target, dist = a
    assert eye[0] > target[0] and eye[2] > target[2]  # south-east: +X east, +Z south
    assert math.degrees(math.asin((eye[1] - target[1]) / dist)) == pytest.approx(38)
    north = rsp.camera_pose(bounds, 70, 0, 38, 40, 16 / 9)[0]
    assert north[2] < target[2] and north[0] == pytest.approx(target[0])


def test_daylight_times_span_the_hours_the_sun_is_up():
    noon = 1782057600.0 - 1782057600.0 % 86400 + 43200 + 7 * 3600  # 105 W: local noon at 19:00 UTC

    def elevation(t):
        return 60 * math.cos((t - noon) / 43200 * math.pi) - 10

    times = rsp.daylight_times(1782057600.0, -105.0, elevation, 5, min_elevation=4.0)
    assert len(times) == 5 and times == sorted(times)
    assert all(elevation(t) >= 4.0 for t in times)
    assert times[2] == pytest.approx(noon, abs=60)
    with pytest.raises(rsp.PreviewError):
        rsp.daylight_times(1782057600.0, -105.0, lambda t: -5.0, 3)


def test_backdrop_warms_toward_the_horizon():
    low, high = rsp.backdrop_for(0), rsp.backdrop_for(60)
    assert low[0] > high[0] and low[2] < high[2]
    assert all(0 <= v <= 1 for v in low + high)


def test_parse_time_reads_utc():
    assert rsp.parse_time("2026-06-21T16:00:00Z") == 1782057600.0
    assert rsp.parse_time("2026-06-21T16:00:00") == 1782057600.0


# ------------------------------------------------------------------ command line


def test_sequence_needs_a_gif(capsys):
    with pytest.raises(SystemExit):
        rsp.parse_args(["world.usda", "out.png", "--sequence", "4"])


def test_missing_stage_exits_2(tmp_path, capsys):
    assert rsp.main([str(tmp_path / "nope.usda"), str(tmp_path / "out.png")]) == rsp.EXIT_INPUT
    assert "no such stage" in capsys.readouterr().err


def test_unloadable_strata_exits_3_before_reading_terrain(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("MCUSD_STRATA_LIB", raising=False)
    fake = tmp_path / "libstrata.so"
    fake.write_bytes(b"not an ELF file")
    stage = tmp_path / "s.usda"
    stage.write_text("#usda 1.0\n")
    assert rsp.main([str(stage), str(tmp_path / "out.png"), "--strata", str(fake)]) == rsp.EXIT_STRATA
    assert "cannot load" in capsys.readouterr().err
