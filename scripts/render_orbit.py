"""Render a looping orbit of an exported stage, with the claims overlay fading in.

Run inside Blender (headless), not with the project's Python::

    blender -b --threads 4 --python scripts/render_orbit.py -- \\
        world.usda frames/ [--frames 300] [--size 960 540] [--samples 16]

``STAGE`` is normally the root ``world.usda``, so Blender's USD importer composes
``overlays/claims.usda`` over ``terrain.usdc``. The camera circles the export once
(360 degrees over ``--frames`` frames, so the last frame leads straight back into the
first) at a fixed elevation, framed so the whole export box stays in view at every
angle. The claims boxes are invisible for the first half of the loop, fade in to
their authored ``displayOpacity`` (times ``--overlay-strength``; ``--fade-in``, as fractions of the loop), and fade
out again just before the loop restarts (``--fade-out``) so the video loops without
a jump. Only the nearest overlay surface along each ray is tinted, so a large claim
reads as one layer of coloured glass instead of turning opaque where rows of adjacent
chunk boxes stack up.

Frames are written as ``OUT_DIR/frame_0000.png`` and so on; encode them with any
ffmpeg, for example::

    ffmpeg -framerate 30 -i frames/frame_%04d.png -c:v libx264 -pix_fmt yuv420p \\
        -crf 26 -movflags +faststart orbit.mp4

``--still FRACTION`` renders a single frame at that point of the loop instead (to
``OUT_DIR/still.png``), for a poster image.

Shading, colours and stage import are shared with ``scripts/render_preview.py``.
This script is for documentation images and videos; it is not part of the package
and nothing in the test suite runs it.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render_preview  # noqa: E402  (sibling script; needs the path entry above)


def parse_args() -> argparse.Namespace:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(prog="render_orbit.py")
    parser.add_argument("stage")
    parser.add_argument("out_dir")
    parser.add_argument("--frames", type=int, default=300, help="frames in one full orbit")
    parser.add_argument("--size", nargs=2, type=int, default=(960, 540), metavar=("W", "H"))
    parser.add_argument("--samples", type=int, default=16)
    parser.add_argument("--elevation", type=float, default=34.0, help="camera elevation in degrees")
    parser.add_argument("--start-azimuth", type=float, default=-45.0, help="azimuth of frame 0 in degrees")
    parser.add_argument("--lens", type=float, default=40.0, help="focal length in mm (36 mm sensor)")
    parser.add_argument("--fade-in", nargs=2, type=float, default=(0.5, 0.75), metavar=("START", "END"),
                        help="fractions of the loop over which the claims overlay fades in")
    parser.add_argument("--fade-out", nargs=2, type=float, default=(0.95, 1.0), metavar=("START", "END"),
                        help="fractions of the loop over which it fades out again (for a seamless loop)")
    parser.add_argument("--overlay-strength", type=float, default=1.0,
                        help="multiplier on the claims boxes' authored opacity once fully faded in")
    parser.add_argument("--still", type=float, help="render only the frame at this fraction of the loop")
    parser.add_argument("--first", type=int, default=0, help="first frame index to render (to resume)")
    return parser.parse_args(argv)


def overlay_fade(fraction: float, fade_in: tuple[float, float], fade_out: tuple[float, float]) -> float:
    """Overlay opacity multiplier (0..1) at a point of the loop, smoothstepped."""
    def ramp(a: float, b: float) -> float:
        t = min(max((fraction - a) / (b - a), 0.0), 1.0) if b > a else float(fraction >= a)
        return t * t * (3 - 2 * t)
    return ramp(*fade_in) * (1.0 - ramp(*fade_out))


def add_fade_control() -> bpy.types.NodeSocket:
    """Multiply the overlay material's alpha by a value the frame loop sets."""
    mat = bpy.data.materials["mcusd_overlay"]
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    bsdf = nodes["Principled BSDF"]
    opacity = next(n for n in nodes if n.type == "ATTRIBUTE" and n.attribute_name == "displayOpacity")
    fade = nodes.new("ShaderNodeValue")
    fade.name = "fade"
    multiply = nodes.new("ShaderNodeMath")
    multiply.operation = "MULTIPLY"
    links.new(opacity.outputs["Fac"], multiply.inputs[0])
    links.new(fade.outputs[0], multiply.inputs[1])
    # Tint only the first overlay surface a ray meets. Each claimed chunk is its own
    # box, so a ray crossing a large claim passes through dozens of faces, and the
    # stacked alpha would turn the territory opaque; behind the first face, the
    # boxes are fully transparent.
    path = nodes.new("ShaderNodeLightPath")
    first = nodes.new("ShaderNodeMath")
    first.operation = "LESS_THAN"
    first.inputs[1].default_value = 0.5
    links.new(path.outputs["Transparent Depth"], first.inputs[0])
    only_first = nodes.new("ShaderNodeMath")
    only_first.operation = "MULTIPLY"
    links.new(multiply.outputs[0], only_first.inputs[0])
    links.new(first.outputs[0], only_first.inputs[1])
    # Claims are a map annotation, not geometry: they cast no shadow on the terrain.
    lit = nodes.new("ShaderNodeMath")
    lit.operation = "SUBTRACT"
    lit.inputs[0].default_value = 1.0
    links.new(path.outputs["Is Shadow Ray"], lit.inputs[1])
    alpha = nodes.new("ShaderNodeMath")
    alpha.operation = "MULTIPLY"
    links.new(only_first.outputs[0], alpha.inputs[0])
    links.new(lit.outputs[0], alpha.inputs[1])
    links.new(alpha.outputs[0], bsdf.inputs["Alpha"])
    return fade.outputs[0]


def setup_scene(args: argparse.Namespace) -> None:
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = args.samples
    scene.cycles.use_denoising = True
    scene.cycles.transparent_max_bounces = 16
    scene.render.use_persistent_data = True  # keep the scene synced between frames
    scene.render.resolution_x, scene.render.resolution_y = args.size
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    scene.render.image_settings.file_format = "PNG"
    scene.view_settings.view_transform = "Standard"

    # Dim sky light for the blocks, but a brighter sky where the camera sees it directly.
    world = bpy.data.worlds.new("sky")
    world.use_nodes = True
    nodes, links = world.node_tree.nodes, world.node_tree.links
    ambient = nodes["Background"]
    ambient.inputs["Color"].default_value = (0.78, 0.84, 0.92, 1.0)
    ambient.inputs["Strength"].default_value = 0.35
    backdrop = nodes.new("ShaderNodeBackground")
    backdrop.inputs["Color"].default_value = (0.80, 0.86, 0.93, 1.0)
    backdrop.inputs["Strength"].default_value = 1.0
    path = nodes.new("ShaderNodeLightPath")
    mix = nodes.new("ShaderNodeMixShader")
    links.new(path.outputs["Is Camera Ray"], mix.inputs["Fac"])
    links.new(ambient.outputs[0], mix.inputs[1])
    links.new(backdrop.outputs[0], mix.inputs[2])
    links.new(mix.outputs[0], nodes["World Output"].inputs["Surface"])
    scene.world = world

    sun_data = bpy.data.lights.new("sun", "SUN")
    sun_data.energy = 3.0
    sun_data.angle = math.radians(3)
    sun = bpy.data.objects.new("sun", sun_data)
    sun_az, sun_el = math.radians(-85), math.radians(55)
    toward_sun = Vector((math.cos(sun_el) * math.cos(sun_az), math.cos(sun_el) * math.sin(sun_az), math.sin(sun_el)))
    sun.rotation_euler = (-toward_sun).to_track_quat("-Z", "Y").to_euler()
    scene.collection.objects.link(sun)


def make_camera(args: argparse.Namespace) -> bpy.types.Object:
    scene = bpy.context.scene
    cam_data = bpy.data.cameras.new("camera")
    cam_data.lens = args.lens
    cam_data.sensor_width = 36.0
    cam_data.sensor_fit = "HORIZONTAL"
    cam = bpy.data.objects.new("camera", cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam
    return cam


def place_camera(cam: bpy.types.Object, target: Vector, distance: float, azimuth_deg: float,
                 elevation_deg: float) -> None:
    az, el = math.radians(azimuth_deg), math.radians(elevation_deg)
    direction = Vector((math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el)))
    cam.location = target + direction * distance
    cam.rotation_euler = (-direction).to_track_quat("-Z", "Y").to_euler()


def fit_distance(cam: bpy.types.Object, lo: Vector, hi: Vector, target: Vector, args: argparse.Namespace) -> float:
    """Smallest camera distance that keeps every corner of the box in frame at every orbit angle."""
    scene = bpy.context.scene
    corners = [Vector((x, y, z)) for x in (lo.x, hi.x) for y in (lo.y, hi.y) for z in (lo.z, hi.z)]
    margin = 0.02

    def fits(distance: float) -> bool:
        for step in range(36):
            place_camera(cam, target, distance, args.start_azimuth + step * 10, args.elevation)
            bpy.context.view_layer.update()
            for corner in corners:
                u, v, depth = world_to_camera_view(scene, cam, corner)
                if depth <= 0 or not (margin <= u <= 1 - margin and margin <= v <= 1 - margin):
                    return False
        return True

    near, far = 1.0, (hi - lo).length * 10
    for _ in range(40):
        mid = (near + far) / 2
        near, far = (near, mid) if fits(mid) else (mid, far)
    return far


def main() -> None:
    args = parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    render_preview.import_stage(args.stage)
    lo, hi = render_preview.terrain_bounds()
    top = render_preview.export_top(args.stage)
    if top is not None:  # Blender's importer converts the stage's Y-up to Z-up
        hi.z = max(hi.z, top)
    counts = render_preview.assign_materials()
    has_overlay = "mcusd_overlay" in bpy.data.materials and counts["claim_prototypes"] > 0
    fade = add_fade_control() if has_overlay else None
    instances = sum(len(o.data.points) for o in bpy.data.objects if o.type == "POINTCLOUD")
    print(f"render_orbit: {instances} instances, {counts}, bounds {tuple(lo)} .. {tuple(hi)}")

    setup_scene(args)
    cam = make_camera(args)
    target = Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z + (hi.z - lo.z) * 0.4))
    distance = fit_distance(cam, lo, hi, target, args)
    cam.data.clip_end = distance * 4
    print(f"render_orbit: camera distance {distance:.1f}")

    scene = bpy.context.scene
    if args.still is not None:
        renders = [(args.still, os.path.join(args.out_dir, "still.png"))]
    else:
        renders = [(i / args.frames, os.path.join(args.out_dir, f"frame_{i:04d}.png"))
                for i in range(args.first, args.frames)]
    for fraction, path in renders:
        started = time.monotonic()
        place_camera(cam, target, distance, args.start_azimuth + 360.0 * fraction, args.elevation)
        if fade is not None:
            fade.default_value = args.overlay_strength * overlay_fade(fraction, tuple(args.fade_in),
                                                                      tuple(args.fade_out))
        scene.render.filepath = path
        bpy.ops.render.render(write_still=True)
        print(f"render_orbit: wrote {path} ({time.monotonic() - started:.1f} s)", flush=True)


if __name__ == "__main__":
    main()
