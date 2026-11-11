"""A small looping turntable of an export, rendered headless with Strata.

    mc-usd-thumbnail out/world.usda thumb/
    mc-usd-thumbnail out/world.usda thumb/ --frames 12 --size 200 132 --ms 250

Writes two files into the output folder:

- ``thumb.webp``: an animated WebP that loops forever, the camera circling the
  export once at a fixed elevation;
- ``poster.png``: the first frame as a still, for viewers who asked for no motion.

The scene is the top-surface path of ``mcusd.preview`` (usd-web's greedy-meshed
top surface, one Strata mesh per 128 x 128 tile, a fixed sun); only the camera loop
and the image packing are new here. Claims are never drawn: a thumbnail carries
block geometry and nothing else. Every frame is checked for blankness like a
preview, so a camera that misses or an empty box fails the run instead of writing
an empty picture.

It needs what ``mc-usd-preview`` needs (usd-web, Strata's Python binding, Pillow,
``libEGL.so.1`` with a Mesa driver) and loads the Strata release pinned in
``mcusd/strata.lock.json`` the same way. Exit status matches ``mc-usd-preview``:
0 written; 1 rendering failed or a frame came out blank; 2 bad input; 3 Strata (or
EGL/Mesa) could not be loaded.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

from . import preview as pv

THUMB, POSTER = "thumb.webp", "poster.png"
DEFAULT_FRAMES = 12
DEFAULT_SIZE = (200, 132)
DEFAULT_MS = 250
DEFAULT_ELEVATION = 35.0
DEFAULT_AZIMUTH = 135.0  # first frame looks from the south-east, as mc-usd-preview does
DEFAULT_QUALITY = 60


def azimuths(frames: int, start: float = DEFAULT_AZIMUTH) -> list[float]:
    """``frames`` camera azimuths, evenly spaced over one full turn from ``start``.
    The last frame stops one step short of ``start`` so the loop does not stutter."""
    if frames < 1:
        raise ValueError("frames must be at least 1")
    return [(start + 360.0 * i / frames) % 360.0 for i in range(frames)]


def turntable_frames(strata, scene, *, frames: int = DEFAULT_FRAMES, size=DEFAULT_SIZE,
                     elevation: float = DEFAULT_ELEVATION, fov: float = 40.0,
                     start: float = DEFAULT_AZIMUTH, min_coverage: float = 0.02,
                     sun: str = pv.DEFAULT_SUN, lat: float = pv.DEFAULT_LAT, lon: float = pv.DEFAULT_LON,
                     timings: dict | None = None) -> list[np.ndarray]:
    """Render ``frames`` RGBA frames of ``scene`` with the camera circling it.

    ``strata`` is a ``mcusd.preview.Strata`` (or anything with its methods); this
    function creates its engine and always destroys it. Raises ``PreviewError``
    when a frame is blank."""
    w, h = size
    timings = {} if timings is None else timings
    out: list[np.ndarray] = []
    try:
        t = time.perf_counter()
        strata.create(w, h)
        timings["s_strata_create"] = round(time.perf_counter() - t, 3)
        x0, y0, z0, x1, y1, z1 = scene.bounds
        strata.lighting(shadow_distance=max(x1 - x0, z1 - z0, y1 - y0) * 2.5)
        sp = strata.solar(pv.parse_time(sun), lat, lon)
        strata.sun(sp.to_sun)
        backdrop = pv.backdrop_for(sp.elevation_degrees)
        for m in scene.meshes:
            strata.add_mesh(*m)
        empty = None
        frame_s = []
        angles = azimuths(frames, start)
        for i, az in enumerate([angles[0], *angles]):  # the first draw is a warm-up: shadows settle
            eye, target, dist = pv.camera_pose(scene.bounds, scene.focus_y, az, elevation, fov, w / h)
            strata.camera(eye, target, fov, max(0.5, dist / 400), dist * 4)
            t = time.perf_counter()
            px = strata.frame(None, None, backdrop=backdrop)
            frame_s.append(round(time.perf_counter() - t, 3))
            if i == 0:
                continue
            if empty is None:
                empty = pv.flat_image(px.shape, backdrop)
            cov = pv.coverage(px, empty)
            if cov < min_coverage:
                raise pv.PreviewError(f"frame {i} at azimuth {az:g} is blank ({cov:.1%} drawn)")
            out.append(px)
        timings.update(s_frames=frame_s, gpu=strata.gpu, strata=strata.version)
    finally:
        strata.destroy()
    return out


def write_turntable(frames: list[np.ndarray], out_dir: Path, *, ms: int = DEFAULT_MS,
                    quality: int = DEFAULT_QUALITY) -> dict:
    """Write ``thumb.webp`` (animated, loops) and ``poster.png`` (frame 0) into
    ``out_dir``. Each file is written under a temporary name and renamed, so a
    reader never sees half a picture. Returns the byte size of each file."""
    from PIL import Image

    if not frames:
        raise ValueError("no frames")
    out_dir.mkdir(parents=True, exist_ok=True)
    images = [Image.fromarray(np.ascontiguousarray(f[:, :, :3])) for f in frames]
    sizes = {}
    for name, save in (
        (POSTER, lambda p: images[0].save(p, format="PNG", optimize=True)),
        (THUMB, lambda p: images[0].save(p, format="WEBP", save_all=True, append_images=images[1:],
                                         duration=ms, loop=0, quality=quality, method=6)),
    ):
        part = out_dir / f".{name}.part"
        save(part)
        os.replace(part, out_dir / name)
        sizes[name] = (out_dir / name).stat().st_size
    return sizes


def render(args) -> dict:
    t_all = time.perf_counter()
    stage = pv.open_stage(Path(args.stage))
    strata = pv.Strata(pv.resolve_library(args.strata, pv.read_lock()))  # fail fast, before the slow part
    scene = pv.build_top_surface(stage, [])  # never claims: geometry only
    stats: dict = {}
    frames = turntable_frames(strata, scene, frames=args.frames, size=tuple(args.size), elevation=args.elevation,
                              fov=args.fov, min_coverage=args.min_coverage, timings=stats)
    t = time.perf_counter()
    sizes = write_turntable(frames, Path(args.out), ms=args.ms, quality=args.quality)
    stats.update(
        frames=len(frames), size=list(args.size), ms=args.ms, bytes=sizes,
        s_encode=round(time.perf_counter() - t, 3), s_total=round(time.perf_counter() - t_all, 3),
        scene={k: v for k, v in scene.stats.items() if isinstance(v, (int, float))},
    )
    return stats


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="mc-usd-thumbnail", description=__doc__.split("\n\n")[0])
    p.add_argument("stage", help="world.usda (or any layer the exporter wrote)")
    p.add_argument("out", help="output folder for thumb.webp and poster.png")
    p.add_argument("--strata", help="path to libstrata (default: MCUSD_STRATA_LIB, else the pinned release)")
    p.add_argument("--frames", type=int, default=DEFAULT_FRAMES, help="frames in one full turn")
    p.add_argument("--size", nargs=2, type=int, default=list(DEFAULT_SIZE), metavar=("W", "H"))
    p.add_argument("--ms", type=int, default=DEFAULT_MS, help="duration of each frame in milliseconds")
    p.add_argument("--elevation", type=float, default=DEFAULT_ELEVATION, help="camera elevation in degrees")
    p.add_argument("--fov", type=float, default=40.0)
    p.add_argument("--quality", type=int, default=DEFAULT_QUALITY, help="WebP quality, 0-100")
    p.add_argument("--min-coverage", type=float, default=0.02,
                   help="fail if fewer than this fraction of a frame's pixels differ from an empty frame")
    p.add_argument("--stats-json", help="also write timings and sizes as JSON")
    args = p.parse_args(argv)
    if not 1 <= args.frames <= 120:
        p.error("--frames must be between 1 and 120")
    if min(args.size) < 16 or max(args.size) > 1024:
        p.error("--size must be between 16 and 1024 pixels on each side")
    if args.ms < 20:
        p.error("--ms must be at least 20")
    if not 0 <= args.quality <= 100:
        p.error("--quality must be between 0 and 100")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        stats = render(args)
    except pv.PreviewError as exc:
        print(f"mc-usd-thumbnail: {exc}", file=sys.stderr)
        return exc.code
    text = json.dumps(stats, indent=1)
    if args.stats_json:
        Path(args.stats_json).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
