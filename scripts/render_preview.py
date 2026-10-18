"""Render a preview image of an exported stage with Blender's built-in USD importer.

Run inside Blender (headless), not with the project's Python::

    blender -b --threads 6 --python scripts/render_preview.py -- \\
        STAGE OUT.png [--frame-from TERRAIN] [--size 1200 900] [--samples 48]

``STAGE`` is any layer the exporter wrote: ``world.usda`` (terrain plus the claims
overlay, composed by Blender's USD importer) or ``terrain.usdc`` alone. The camera
is an orthographic, isometric-style view framed on the terrain's bounds (raised to
the top of the export box when the layer records ``mcusd:bbox``). Pass the same
``--frame-from world.usda`` to both renders of one export (``terrain.usdc`` and
``world.usda``) so they line up exactly.

Shading uses only what the stage carries: every prototype's constant
``primvars:displayColor`` (the exporter's per-block display colour) and, for the
claims boxes, ``primvars:displayOpacity``. Blender's importer turns each
``PointInstancer`` into a point cloud instanced through geometry nodes, so one
shared material reading those attributes covers every block and claim box.

This script is for documentation images (docs/images/); it is not part of the
package and nothing in the test suite runs it.
"""

from __future__ import annotations

import argparse
import math
import sys

import bpy
import numpy as np
from mathutils import Matrix, Vector


def parse_args() -> argparse.Namespace:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(prog="render_preview.py")
    parser.add_argument("stage")
    parser.add_argument("out")
    parser.add_argument("--frame-from", help="layer whose terrain bounds frame the camera (default: STAGE)")
    parser.add_argument("--size", nargs=2, type=int, default=(1200, 900), metavar=("W", "H"))
    parser.add_argument("--samples", type=int, default=48)
    parser.add_argument("--azimuth", type=float, default=-45.0, help="camera azimuth in degrees")
    parser.add_argument("--elevation", type=float, default=32.0, help="camera elevation in degrees")
    return parser.parse_args(argv)


def import_stage(path: str) -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.wm.usd_import(filepath=path, import_materials=False, read_mesh_colors=True,
                          read_mesh_attributes=True)


def terrain_bounds() -> tuple[Vector, Vector]:
    """World-space bounds of every instanced block (points under the ``Terrain`` prim)."""
    lo = np.full(3, np.inf)
    hi = np.full(3, -np.inf)
    for obj in bpy.data.objects:
        if obj.type != "POINTCLOUD" or obj.parent is None or obj.parent.name.split(".")[0] != "Terrain":
            continue
        pts = np.empty(len(obj.data.points) * 3, dtype=np.float32)
        obj.data.attributes["position"].data.foreach_get("vector", pts)
        pts = pts.reshape(-1, 3)
        mw = np.array(obj.matrix_world)
        world = pts @ mw[:3, :3].T + mw[:3, 3]
        lo = np.minimum(lo, world.min(axis=0))
        hi = np.maximum(hi, world.max(axis=0) + 1.0)  # a block spans [p, p + 1]
    if not np.isfinite(lo).all():
        raise SystemExit("no terrain point instancers found in the stage")
    return Vector(lo.tolist()), Vector(hi.tolist())


def export_top(path: str) -> float | None:
    """Top of the export box (``mcusd:bbox`` y2 + 1) from a root layer, if it records one.

    The claims boxes reach the top of the export's Y range, above the highest block, so
    framing on it keeps them in the picture; ``terrain.usdc`` alone has no bbox.
    """
    from pxr import Sdf

    layer = Sdf.Layer.FindOrOpen(path)
    bbox = layer.customLayerData.get("mcusd:bbox") if layer else None
    return float(bbox[4]) + 1.0 if bbox else None


def display_material() -> bpy.types.Material:
    """Base colour from each prototype's ``displayColor``."""
    mat = bpy.data.materials.new("mcusd_display")
    mat.use_nodes = True
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    bsdf = nodes["Principled BSDF"]
    bsdf.inputs["Roughness"].default_value = 0.85
    color = nodes.new("ShaderNodeAttribute")
    color.attribute_name = "displayColor"
    links.new(color.outputs["Color"], bsdf.inputs["Base Color"])
    return mat


def overlay_material() -> bpy.types.Material:
    mat = display_material()
    mat.name = "mcusd_overlay"
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    bsdf = nodes["Principled BSDF"]
    opacity = nodes.new("ShaderNodeAttribute")
    opacity.attribute_name = "displayOpacity"
    links.new(opacity.outputs["Fac"], bsdf.inputs["Alpha"])
    return mat


def assign_materials() -> dict[str, int]:
    """Give every prototype mesh the display material; returns prototype counts per layer."""
    block_mat, overlay_mat = display_material(), overlay_material()
    counts = {"block_prototypes": 0, "claim_prototypes": 0}
    for mesh in bpy.data.meshes:
        is_overlay = "displayOpacity" in mesh.attributes
        mesh.materials.clear()
        mesh.materials.append(overlay_mat if is_overlay else block_mat)
        counts["claim_prototypes" if is_overlay else "block_prototypes"] += 1
    return counts


def setup_scene(lo: Vector, hi: Vector, args: argparse.Namespace) -> None:
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = args.samples
    scene.cycles.use_denoising = True
    scene.render.resolution_x, scene.render.resolution_y = args.size
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = True  # composited onto a flat page colour later
    scene.view_settings.view_transform = "Standard"

    world = bpy.data.worlds.new("sky")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.78, 0.84, 0.92, 1.0)
    world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.35
    scene.world = world

    sun_data = bpy.data.lights.new("sun", "SUN")
    sun_data.energy = 3.0
    sun_data.angle = math.radians(3)
    sun = bpy.data.objects.new("sun", sun_data)
    az, el = math.radians(args.azimuth), math.radians(args.elevation)
    # Light from the camera's side, swung 40 degrees round and higher, so the two visible
    # sides of the area get different, readable shading.
    sun_az, sun_el = az - math.radians(40), math.radians(55)
    toward_sun = Vector((math.cos(sun_el) * math.cos(sun_az), math.cos(sun_el) * math.sin(sun_az), math.sin(sun_el)))
    sun.rotation_euler = (-toward_sun).to_track_quat("-Z", "Y").to_euler()
    scene.collection.objects.link(sun)

    center = (lo + hi) / 2
    direction = Vector((math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el)))
    cam_data = bpy.data.cameras.new("camera")
    cam_data.type = "ORTHO"
    cam = bpy.data.objects.new("camera", cam_data)
    distance = (hi - lo).length * 2
    cam.location = center + direction * distance
    cam.rotation_euler = (-direction).to_track_quat("-Z", "Y").to_euler()
    cam_data.clip_end = distance * 3
    scene.collection.objects.link(cam)
    scene.camera = cam

    # Fit the orthographic frame to the projected corners of the bounds, with a margin.
    bpy.context.view_layer.update()
    inv = cam.matrix_world.inverted()
    corners = [inv @ Vector((x, y, z)) for x in (lo.x, hi.x) for y in (lo.y, hi.y) for z in (lo.z, hi.z)]
    width = max(c.x for c in corners) - min(c.x for c in corners)
    height = max(c.y for c in corners) - min(c.y for c in corners)
    aspect = args.size[0] / args.size[1]
    cam_data.ortho_scale = max(width, height * aspect) * 1.06
    mid = Vector(((max(c.x for c in corners) + min(c.x for c in corners)) / 2,
                  (max(c.y for c in corners) + min(c.y for c in corners)) / 2, 0))
    cam.location = cam.matrix_world @ Matrix.Translation(mid) @ Vector((0, 0, 0))


def main() -> None:
    args = parse_args()
    frame = args.frame_from or args.stage
    if args.frame_from:
        import_stage(args.frame_from)
        lo, hi = terrain_bounds()
    import_stage(args.stage)
    if not args.frame_from:
        lo, hi = terrain_bounds()
    top = export_top(frame)
    if top is not None:  # Blender's importer converts the stage's Y-up to Z-up
        hi.z = max(hi.z, top)
    counts = assign_materials()
    instancers = sum(1 for o in bpy.data.objects if o.type == "POINTCLOUD")
    instances = sum(len(o.data.points) for o in bpy.data.objects if o.type == "POINTCLOUD")
    print(f"render_preview: {instancers} point instancers, {instances} instances, {counts}")
    setup_scene(lo, hi, args)
    bpy.context.scene.render.filepath = args.out
    bpy.ops.render.render(write_still=True)
    print(f"render_preview: wrote {args.out}")


if __name__ == "__main__":
    main()
