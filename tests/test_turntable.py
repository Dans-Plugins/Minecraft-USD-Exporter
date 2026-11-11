"""mcusd.turntable: the camera loop and image packing of the thumbnail turntable.

Strata is replaced by a fake here (the CI job "Strata previews" renders a real
turntable); the packing tests need Pillow and are skipped without it.
"""

import json
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
