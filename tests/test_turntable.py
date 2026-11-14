"""mcusd.turntable: the camera loop and image packing of the thumbnail turntable.

Strata is replaced by a fake here (the CI job "Strata previews" renders a real
turntable); the packing tests need Pillow and are skipped without it.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import mcusd.preview as pv
import mcusd.turntable as tt


class FakeStrata:
    """Records calls; draws a block of colour in each frame unless told to draw nothing."""

    version = "0.0.0-fake"
    gpu = "fake"

    def __init__(self, blank_from: int | None = None):
        self.calls = []
        self.frames = 0
        self.blank_from = blank_from
        self.destroyed = False
        self.backdrop = None

    def create(self, w, h):
        self.size = (w, h)
        self.calls.append(("create", w, h))

    def lighting(self, shadow_distance):
        self.calls.append(("lighting", shadow_distance))

    def solar(self, unix, lat, lon):
        return SimpleNamespace(to_sun=(0.0, 1.0, 0.0), elevation_degrees=45.0)

    def sun(self, to_sun):
        self.calls.append(("sun", to_sun))

    def add_mesh(self, *mesh):
        self.calls.append(("add_mesh", len(mesh)))

    def camera(self, eye, target, fov, near, far):
        self.calls.append(("camera", tuple(np.round(eye, 6)), tuple(np.round(target, 6))))

    def frame(self, cubes, lines, backdrop):
        w, h = self.size
        img = pv.flat_image((h, w, 4), backdrop)
        if self.blank_from is None or self.frames < self.blank_from:
            img[: h // 2, : w // 2, :3] = (200, 30 + 20 * (self.frames % 8), 30)  # each frame differs
        self.frames += 1
        return img

    def destroy(self):
        self.destroyed = True


def scene():
    return pv.Scene(meshes=[("pos", "nor", "col", "idx")], bounds=(0.0, 50.0, 0.0, 128.0, 90.0, 128.0), focus_y=70.0)


def test_azimuths_make_one_full_turn_without_repeating_the_first_frame():
    a = tt.azimuths(12)
    assert len(a) == 12 and a[0] == tt.DEFAULT_AZIMUTH
    steps = np.diff(np.unwrap(np.radians(a + [a[0]])))
    assert np.allclose(np.degrees(steps), 30.0)
    assert len(set(a)) == 12


def test_azimuths_reject_zero_frames():
    with pytest.raises(ValueError):
        tt.azimuths(0)


def test_frames_circle_the_scene_at_a_fixed_distance_and_destroy_the_engine():
    fake = FakeStrata()
    timings = {}
    frames = tt.turntable_frames(fake, scene(), frames=8, size=(64, 40), timings=timings)
    assert len(frames) == 8 and frames[0].shape == (40, 64, 4)
    assert fake.frames == 9  # one warm-up draw
    cams = [c for c in fake.calls if c[0] == "camera"]
    targets = {c[2] for c in cams}
    assert len(targets) == 1  # always looking at the same point
    target = np.array(next(iter(targets)))
    dists = [np.linalg.norm(np.array(c[1]) - target) for c in cams]
    assert np.allclose(dists, dists[0])
    assert len({c[1] for c in cams[1:]}) == 8  # eight distinct eye positions after the warm-up
    assert fake.destroyed
    assert timings["strata"] == "0.0.0-fake" and len(timings["s_frames"]) == 9


def test_a_blank_frame_fails_the_run_and_still_destroys_the_engine():
    fake = FakeStrata(blank_from=3)
    with pytest.raises(pv.PreviewError, match="blank"):
        tt.turntable_frames(fake, scene(), frames=6, size=(32, 32))
    assert fake.destroyed


def test_args_have_the_rfc_defaults_and_reject_silly_values():
    a = tt.parse_args(["world.usda", "out"])
    assert (a.frames, a.size, a.ms) == (12, [200, 132], 250)
    for bad in (["--frames", "0"], ["--size", "8", "8"], ["--ms", "5"], ["--quality", "101"]):
        with pytest.raises(SystemExit):
            tt.parse_args(["world.usda", "out", *bad])


def test_a_missing_stage_is_an_input_error(tmp_path, capsys):
    assert tt.main([str(tmp_path / "nope.usda"), str(tmp_path / "out")]) == pv.EXIT_INPUT
    assert "no such stage" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_packing_writes_a_looping_webp_and_a_poster(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    frames = tt.turntable_frames(FakeStrata(), scene(), frames=4, size=(48, 32))
    sizes = tt.write_turntable(frames, tmp_path / "t", ms=250)
    assert set(sizes) == {"thumb.webp", "poster.png"}
    assert sorted(p.name for p in (tmp_path / "t").iterdir()) == ["poster.png", "thumb.webp"]  # no .part left
    with Image.open(tmp_path / "t" / "thumb.webp") as im:
        assert im.format == "WEBP" and im.n_frames == 4 and im.size == (48, 32)
        assert im.info.get("loop") == 0
    with Image.open(tmp_path / "t" / "poster.png") as im:
        assert im.size == (48, 32)
        assert np.array_equal(np.asarray(im.convert("RGB")), frames[0][:, :, :3])


def test_packing_refuses_no_frames(tmp_path):
    with pytest.raises(ValueError):
        tt.write_turntable([], tmp_path)


def test_the_lock_ships_inside_the_package():
    assert pv.LOCK.parent.name == "mcusd" and pv.LOCK.is_file()
    assert json.loads(pv.LOCK.read_text())["asset"].startswith("libstrata-")


# --- azimuths: start angle, wrap-around and the smallest loop ---


def test_azimuths_wrap_past_360_from_a_custom_start():
    assert tt.azimuths(4, start=300.0) == [300.0, 30.0, 120.0, 210.0]


def test_a_single_frame_turntable_is_just_the_start_angle():
    assert tt.azimuths(1) == [tt.DEFAULT_AZIMUTH]


def test_azimuths_reject_negative_frames():
    with pytest.raises(ValueError, match="at least 1"):
        tt.azimuths(-3)


# --- turntable_frames: engine set-up, the blank check and teardown ---


def test_the_engine_is_created_at_the_requested_size_and_every_mesh_is_added_before_the_first_camera():
    fake = FakeStrata()
    s = pv.Scene(meshes=[("a",) * 4, ("b",) * 4, ("c",) * 4], bounds=(0.0, 0.0, 0.0, 10.0, 100.0, 20.0), focus_y=50.0)
    tt.turntable_frames(fake, s, frames=2, size=(40, 24))
    assert fake.calls[0] == ("create", 40, 24)
    kinds = [c[0] for c in fake.calls]
    assert kinds.count("add_mesh") == 3
    assert max(i for i, k in enumerate(kinds) if k == "add_mesh") < kinds.index("camera")


def test_shadow_distance_is_two_and_a_half_times_the_largest_extent():
    fake = FakeStrata()
    s = pv.Scene(meshes=[("a",) * 4], bounds=(0.0, 0.0, 0.0, 10.0, 100.0, 20.0), focus_y=50.0)
    tt.turntable_frames(fake, s, frames=2, size=(32, 32))
    assert ("lighting", 250.0) in fake.calls  # the 100-block height wins over 10 x 20


def test_a_blank_frame_error_names_the_frame_and_its_azimuth_and_is_a_render_error():
    fake = FakeStrata(blank_from=3)  # draws 0-2 have content; draw 3 is frame 3 (draw 0 is the warm-up)
    with pytest.raises(pv.PreviewError, match=r"frame 3 at azimuth 255 is blank \(0\.0% drawn\)") as exc:
        tt.turntable_frames(fake, scene(), frames=6, size=(32, 32))
    assert exc.value.code == pv.EXIT_RENDER


def test_a_blank_warm_up_draw_is_not_checked():
    class BlankFirst(FakeStrata):
        def frame(self, cubes, lines, backdrop):
            img = super().frame(cubes, lines, backdrop)
            if self.frames == 1:  # the warm-up draw only
                img[:] = pv.flat_image(img.shape, backdrop)
            return img

    frames = tt.turntable_frames(BlankFirst(), scene(), frames=3, size=(32, 32))
    assert len(frames) == 3


def test_min_coverage_is_a_strict_lower_bound():
    frames = tt.turntable_frames(FakeStrata(blank_from=0), scene(), frames=4, size=(32, 32), min_coverage=0.0)
    assert len(frames) == 4  # 0% drawn is not below 0%


def test_an_engine_that_fails_to_start_is_still_destroyed():
    class NoEngine(FakeStrata):
        def create(self, w, h):
            raise pv.PreviewError("no EGL", pv.EXIT_STRATA)

    fake = NoEngine()
    with pytest.raises(pv.PreviewError, match="no EGL"):
        tt.turntable_frames(fake, scene(), frames=2, size=(32, 32))
    assert fake.destroyed
    assert fake.calls == []  # nothing else was attempted


def test_timings_record_engine_start_frame_times_gpu_and_version():
    timings = {}
    tt.turntable_frames(FakeStrata(), scene(), frames=2, size=(32, 32), timings=timings)
    assert set(timings) == {"s_strata_create", "s_frames", "gpu", "strata"}
    assert timings["gpu"] == "fake" and len(timings["s_frames"]) == 3


# --- parse_args: defaults and the edges of each range ---


def test_args_default_the_camera_quality_and_coverage_and_leave_optional_paths_unset():
    a = tt.parse_args(["world.usda", "out"])
    assert (a.elevation, a.fov, a.quality, a.min_coverage) == (35.0, 40.0, 60, 0.02)
    assert a.strata is None and a.stats_json is None


@pytest.mark.parametrize("ok", [
    ["--frames", "1"], ["--frames", "120"], ["--size", "16", "1024"], ["--ms", "20"],
    ["--quality", "0"], ["--quality", "100"],
])
def test_args_accept_the_ends_of_each_range(ok):
    tt.parse_args(["world.usda", "out", *ok])


@pytest.mark.parametrize("bad, message", [
    (["--frames", "121"], "--frames must be between 1 and 120"),
    (["--size", "16", "1025"], "--size must be between 16 and 1024"),
    (["--size", "15", "16"], "--size must be between 16 and 1024"),
    (["--ms", "19"], "--ms must be at least 20"),
    (["--quality", "-1"], "--quality must be between 0 and 100"),
])
def test_args_just_outside_each_range_are_rejected_with_a_named_message(bad, message, capsys):
    with pytest.raises(SystemExit) as exc:
        tt.parse_args(["world.usda", "out", *bad])
    assert exc.value.code == 2
    assert message in capsys.readouterr().err


# --- main: stats output and exit codes ---


def test_main_prints_the_stats_and_writes_them_to_stats_json(tmp_path, monkeypatch, capsys):
    stats = {"frames": 3, "bytes": {"thumb.webp": 10, "poster.png": 5}}
    monkeypatch.setattr(tt, "render", lambda args: stats)
    out = tmp_path / "stats.json"
    assert tt.main(["world.usda", str(tmp_path / "t"), "--stats-json", str(out)]) == 0
    text = json.dumps(stats, indent=1)
    assert capsys.readouterr().out == text + "\n"
    assert out.read_text(encoding="utf-8") == text + "\n"


def test_main_without_stats_json_writes_no_stats_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tt, "render", lambda args: {"frames": 1})
    assert tt.main(["world.usda", str(tmp_path / "t")]) == 0
    assert json.loads(capsys.readouterr().out) == {"frames": 1}
    assert list(tmp_path.iterdir()) == []


def test_main_exits_with_the_errors_own_code_and_writes_no_stats(tmp_path, monkeypatch, capsys):
    def fail(args):
        raise pv.PreviewError("cannot load libstrata.so", pv.EXIT_STRATA)

    monkeypatch.setattr(tt, "render", fail)
    out = tmp_path / "stats.json"
    assert tt.main(["world.usda", str(tmp_path / "t"), "--stats-json", str(out)]) == pv.EXIT_STRATA
    captured = capsys.readouterr()
    assert captured.err == "mc-usd-thumbnail: cannot load libstrata.so\n"
    assert captured.out == "" and not out.exists()


# --- render: wiring between the stage, Strata, the scene and the packer ---


def _stub_render_deps(monkeypatch, calls, *, resolve=None):
    monkeypatch.setattr(pv, "open_stage", lambda path: calls.append(("open_stage", path)) or "STAGE")
    monkeypatch.setattr(pv, "read_lock", lambda: {"version": "lock"})
    monkeypatch.setattr(pv, "resolve_library",
                        resolve or (lambda path, lock: calls.append(("resolve", path, lock)) or "LIB"))
    monkeypatch.setattr(pv, "Strata", lambda lib: calls.append(("strata", lib)) or FakeStrata())

    def build(stage, factions):
        calls.append(("build", stage, factions))
        s = scene()
        s.stats = {"quads": 10, "ratio": 0.5, "source": "example"}
        return s

    monkeypatch.setattr(pv, "build_top_surface", build)

    def write(frames, out_dir, *, ms, quality):
        calls.append(("write", len(frames), out_dir, ms, quality))
        return {"thumb.webp": 10, "poster.png": 5}

    monkeypatch.setattr(tt, "write_turntable", write)


def test_render_builds_a_claimless_top_surface_and_reports_what_it_wrote(tmp_path, monkeypatch):
    calls = []
    _stub_render_deps(monkeypatch, calls)
    args = tt.parse_args(["world.usda", str(tmp_path / "t"), "--frames", "3", "--size", "32", "32",
                          "--ms", "100", "--quality", "70", "--strata", "/x/libstrata.so"])
    stats = tt.render(args)
    assert calls == [
        ("open_stage", Path("world.usda")),
        ("resolve", "/x/libstrata.so", {"version": "lock"}),
        ("strata", "LIB"),
        ("build", "STAGE", []),  # never claims
        ("write", 3, tmp_path / "t", 100, 70),
    ]
    assert (stats["frames"], stats["size"], stats["ms"]) == (3, [32, 32], 100)
    assert stats["bytes"] == {"thumb.webp": 10, "poster.png": 5}
    assert stats["scene"] == {"quads": 10, "ratio": 0.5}  # non-numeric scene stats are dropped
    assert stats["strata"] == "0.0.0-fake"
    assert {"s_encode", "s_total", "s_strata_create", "s_frames"} <= set(stats)


def test_strata_is_resolved_before_the_scene_is_built(tmp_path, monkeypatch, capsys):
    calls = []

    def missing(path, lock):
        raise pv.PreviewError("Strata library not found: /x", pv.EXIT_STRATA)

    _stub_render_deps(monkeypatch, calls, resolve=missing)
    assert tt.main(["world.usda", str(tmp_path / "t"), "--strata", "/x"]) == pv.EXIT_STRATA
    assert [c[0] for c in calls] == ["open_stage"]  # no scene built, nothing written
    assert "Strata library not found" in capsys.readouterr().err
