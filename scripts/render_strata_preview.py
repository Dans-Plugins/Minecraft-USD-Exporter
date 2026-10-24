"""Render a headless preview of an export with Strata: no GPU, no window, no Blender.

Run with the project's own Python (not inside Blender)::

    python scripts/render_strata_preview.py out/world.usda preview.png
    python scripts/render_strata_preview.py out/world.usda full.png --full
    python scripts/render_strata_preview.py out/world.usda day.gif --sequence 24

``STAGE`` is a layer the exporter wrote, normally ``world.usda``. The preview reads
that USD output, not the exporter's in-memory arrays, so it shows what a user opens.

- **Default (path A):** usd-web reduces the terrain to its top surface and
  greedy-meshes it; each 128 x 128 tile becomes one Strata static mesh, lit by a
  sun with shadows. Trees read as columns, because a top surface has no overhangs.
- **``--full`` (path B):** every visible block becomes one Strata cube primitive,
  caves and the box's cut faces included, at about 50 times the frame cost.
- **Claims:** when the stage composes ``overlays/claims.*``, blocks in claimed
  chunks are tinted toward the faction colour, each claim's outer edge is traced
  over the terrain in that colour, and a legend is added with Pillow (Strata's text
  is 8 x 8 ASCII; faction names are player-chosen). ``--no-claims`` turns this off.
- **Camera and sun are fixed** (``--azimuth``/``--elevation``; ``--sun``/``--lat``/
  ``--lon``), so two runs on one export frame the same way. A fixed sun is a framing
  choice, not a claim about the world.
- **``--sequence N``** renders N frames with the sun moving from just after sunrise
  to just before sunset on ``--sun``'s date and writes them as one animated GIF.

Strata is reached through ``ctypes`` over its C ABI; every struct is checked
against ``strata_struct_size`` before use. The library is the release pinned in
``scripts/strata.lock.json``: pass ``--strata PATH`` (or set ``MCUSD_STRATA_LIB``),
or the script downloads the pinned asset into a per-user cache, verifies its
SHA-256 and deletes it on a mismatch. Headless rendering needs ``libEGL.so.1`` and
a Mesa driver; without a GPU use ``LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=llvmpipe``.

Not a project dependency (Linux x86-64 only, as released)::

    pip install -e . "usd-web @ git+https://github.com/Preponderous-Software/usd-web@03f1cd518b01276da2ecc73435ab2e821c5fba89" pillow

Exit status: 0 written; 1 rendering failed or the image came out blank; 2 bad
input; 3 Strata (or EGL/Mesa) could not be loaded. This does not replace the
usdview/Blender viewer check (AC11): it reads the stage through usd-core itself.
"""

from __future__ import annotations

import argparse
import ctypes as C
import datetime as dt
import hashlib
import json
import math
import os
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
LOCK = HERE / "strata.lock.json"
TERRAIN_PREFIX = "/World/Terrain"
CLAIMS_PREFIX = "/World/Overlays/Claims"
CHUNK = 16
TILE = 128
EXCLUDED_BLOCKS = ("short_grass", "tall_grass")  # thin plants, left out of the surface
TINT = 0.35  # the exporter's CLAIM_OPACITY; how far a claimed block's colour moves toward the faction colour
DEFAULT_SUN = "2026-06-21T16:00:00Z"  # mid-morning at the default place
DEFAULT_LAT, DEFAULT_LON = 40.0, -105.0
ABI_MAJOR, ABI_MIN_MINOR = 1, 6  # meshes 1.4, lines 1.5, strata_solar_position 1.6

EXIT_RENDER, EXIT_INPUT, EXIT_STRATA = 1, 2, 3


class PreviewError(Exception):
    def __init__(self, message: str, code: int = EXIT_RENDER):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- colours


def display_color(c) -> np.ndarray:
    """A display colour as Strata takes it. Strata neither decodes nor encodes sRGB
    (its framebuffer holds colours as given), so the exporter's display colours are
    passed through unchanged and come out as written."""
    return np.asarray(c, dtype=np.float64)


def hex_to_rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


# --------------------------------------------------------------------------- claims


@dataclass
class Faction:
    name: str
    color: tuple[float, float, float]  # sRGB 0..1, as the overlay stores it
    chunks: set[tuple[int, int]] = field(default_factory=set)


def read_claims(stage) -> list[Faction]:
    """Factions and claimed chunks from the composed ``/World/Overlays/Claims`` prims."""
    from pxr import Usd, UsdGeom

    root = stage.GetPrimAtPath(CLAIMS_PREFIX)
    if not root:
        return []
    factions = []
    for prim in root.GetChildren():
        primvar = UsdGeom.PrimvarsAPI(prim).GetPrimvar("displayColor")
        value = primvar.Get() if primvar else None
        color = tuple(float(v) for v in value[0]) if value else (1.0, 1.0, 1.0)
        name = prim.GetCustomDataByKey("mcusd:factionName") or prim.GetName()
        faction = Faction(str(name), color)
        for child in Usd.PrimRange(prim):
            if child.IsA(UsdGeom.PointInstancer):
                pi = UsdGeom.PointInstancer(child)
                pos = np.asarray(pi.GetPositionsAttr().Get() or [], dtype=np.float64).reshape(-1, 3)
                m = np.array(pi.ComputeLocalToWorldTransform(Usd.TimeCode.Default()), dtype=np.float64)
                world = (np.c_[pos, np.ones(len(pos))] @ m)[:, :3]
                faction.chunks |= {(int(math.floor(x / CHUNK)), int(math.floor(z / CHUNK))) for x, _, z in world}
        if faction.chunks:
            factions.append(faction)
    return factions


def chunk_owner(factions: list[Faction]) -> dict[tuple[int, int], int]:
    return {c: i for i, f in enumerate(factions) for c in f.chunks}


def tint_colors(colors: np.ndarray, chunk_x: np.ndarray, chunk_z: np.ndarray,
                factions: list[Faction], amount: float = TINT) -> np.ndarray:
    """Blend ``colors`` (n, 3) toward the faction colour where the chunk is claimed."""
    out = np.array(colors, dtype=np.float32, copy=True)
    if not factions:
        return out
    owner = chunk_owner(factions)
    fac_lin = display_color(np.array([f.color for f in factions])).astype(np.float32)
    idx = np.array([owner.get((int(x), int(z)), -1) for x, z in zip(chunk_x, chunk_z)], dtype=np.int64)
    hit = idx >= 0
    out[hit] = (1.0 - amount) * out[hit] + amount * fac_lin[idx[hit]]
    return out


def border_edges(chunks: set[tuple[int, int]], owner: dict[tuple[int, int], int], me: int):
    """Outer edges of one faction's claim: ``(x0, z0, x1, z1)`` block coordinates of each
    chunk edge whose neighbour is owned by someone else or no one."""
    edges = []
    for cx, cz in sorted(chunks):
        bx, bz = cx * CHUNK, cz * CHUNK
        for (nx, nz), seg in (((cx - 1, cz), (bx, bz, bx, bz + CHUNK)),
                              ((cx + 1, cz), (bx + CHUNK, bz, bx + CHUNK, bz + CHUNK)),
                              ((cx, cz - 1), (bx, bz, bx + CHUNK, bz)),
                              ((cx, cz + 1), (bx, bz + CHUNK, bx + CHUNK, bz + CHUNK))):
            if owner.get((nx, nz)) != me:
                edges.append(seg)
    return edges


def border_polyline(edge, height_at, lift: float = 0.6) -> list[tuple[float, float, float]]:
    """Points tracing ``edge`` over the terrain: one step per block, the line riding
    ``lift`` above the top of the claimed column next to the edge, with vertical
    risers where the height changes. ``height_at(x, z)`` gives the y of a column's top
    face, or None where there is no column."""
    x0, z0, x1, z1 = edge
    pts: list[tuple[float, float, float]] = []
    along_x = z0 == z1
    n = int(abs((x1 - x0) if along_x else (z1 - z0)))
    for i in range(n):
        if along_x:
            # the claimed side is the chunk this edge bounds; sample both neighbours, keep the higher
            cands = [height_at(x0 + i, z0), height_at(x0 + i, z0 - 1)]
        else:
            cands = [height_at(x0, z0 + i), height_at(x0 - 1, z0 + i)]
        cands = [c for c in cands if c is not None]
        if not cands:
            continue
        y = max(cands) + lift
        a = (x0 + i, y, z0) if along_x else (x0, y, z0 + i)
        b = (x0 + i + 1, y, z0) if along_x else (x0, y, z0 + i + 1)
        if pts and (pts[-1][0], pts[-1][2]) == (a[0], a[2]) and pts[-1][1] != y:
            pts.append(a)  # riser
        elif pts and (pts[-1][0], pts[-1][2]) != (a[0], a[2]):
            pts.append(None)  # gap (holes along the edge)
            pts.append(a)
        elif not pts:
            pts.append(a)
        pts.append(b)
    return pts


def polyline_segments(points) -> list[tuple[tuple, tuple]]:
    segs = []
    for a, b in zip(points, points[1:]):
        if a is not None and b is not None:
            segs.append((a, b))
    return segs


# --------------------------------------------------------------------------- path A: top surface


NORMALS = np.array([[0, 1, 0], [1, 0, 0], [-1, 0, 0], [0, 0, 1], [0, 0, -1]], dtype=np.float32)


def quad_cells(q: np.ndarray, ox: int, oz: int) -> tuple[np.ndarray, np.ndarray]:
    """World block column (x, z) each usd-web quad belongs to. A +X face at plane x
    belongs to column x - 1, a +Z face at plane z to row z - 1."""
    d, x, z = q[:, 0], q[:, 1] + ox, q[:, 2] + oz
    cx = np.where(d == 1, x - 1, x)
    cz = np.where(d == 3, z - 1, z)
    return cx, cz


def quads_to_mesh(q: np.ndarray, ox: int, oz: int, colors: np.ndarray):
    """usd-web quads (``dir, x, z, y0, du, dv, color``, tile-local) to world-space
    triangles: 4 vertices and 2 counter-clockwise triangles per quad, flat normals,
    ``colors`` (n, 3) RGB per quad. Returns positions, normals, colours, indices."""
    q = np.asarray(q, dtype=np.int64)
    d, x, z, y0, du, dv = (q[:, i].astype(np.float32) for i in range(6))
    d = q[:, 0]
    n = len(q)
    v = np.zeros((n, 4, 3), dtype=np.float32)
    top = d == 0
    xs = (d == 1) | (d == 2)
    zs = d >= 3
    st = lambda *cols: np.stack(cols, 1)  # noqa: E731
    v[top] = np.stack([st(x, y0, z), st(x, y0, z + dv), st(x + du, y0, z + dv), st(x + du, y0, z)], 1)[top]
    v[xs] = np.stack([st(x, y0, z), st(x, y0, z + du), st(x, y0 + dv, z + du), st(x, y0 + dv, z)], 1)[xs]
    v[zs] = np.stack([st(x, y0, z), st(x + du, y0, z), st(x + du, y0 + dv, z), st(x, y0 + dv, z)], 1)[zs]
    v[:, :, 0] += ox
    v[:, :, 2] += oz
    nrm = NORMALS[d]
    cr = np.cross(v[:, 1] - v[:, 0], v[:, 2] - v[:, 0])
    flip = (cr * nrm).sum(1) < 0
    v[flip] = v[flip][:, ::-1]
    pos = v.reshape(-1, 3)
    nor = np.repeat(nrm, 4, axis=0).astype(np.float32)
    col = np.repeat(np.asarray(colors, dtype=np.float32), 4, axis=0)
    base = (np.arange(n, dtype=np.uint32) * 4)[:, None]
    idx = (base + np.array([0, 1, 2, 0, 2, 3], dtype=np.uint32)).reshape(-1)
    return (np.ascontiguousarray(pos), np.ascontiguousarray(nor), np.ascontiguousarray(col),
            np.ascontiguousarray(idx))


def align_to_chunks(heights: np.ndarray, colors: np.ndarray, sub: np.ndarray, origin: tuple[int, int], hole: int):
    """Pad a ``[z, x]`` surface with holes in front so its origin is a multiple of 16:
    usd-web never lets a quad cross a tile-local multiple of 16, so this makes every
    quad fall in one world chunk."""
    x0, z0 = origin
    px, pz = x0 % CHUNK, z0 % CHUNK
    if px == 0 and pz == 0:
        return heights, colors, sub, origin
    pad = ((pz, 0), (px, 0))
    return (np.pad(heights, pad, constant_values=hole), np.pad(colors, pad), np.pad(sub, pad),
            (x0 - px, z0 - pz))


@dataclass
class Scene:
    meshes: list = field(default_factory=list)  # (positions, normals, colours, indices)
    cubes: np.ndarray | None = None  # StrataRenderItem records (path B)
    lines: np.ndarray | None = None  # StrataLineVertex records
    bounds: tuple = (0.0, 0.0, 0.0, 1.0, 1.0, 1.0)  # x0, y0, z0, x1, y1, z1 of the drawn blocks
    focus_y: float = 0.0
    factions: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def terrain_only(prim) -> bool:
    return str(prim.GetPath()).startswith(TERRAIN_PREFIX) and prim.GetName() not in EXCLUDED_BLOCKS


def surface_heights(surf, hole):
    """``height_at(x, z)``: y of the top face of world column (x, z), None for a hole."""
    x0, z0 = surf.origin
    h = surf.heights

    def height_at(x: int, z: int):
        i, j = z - z0, x - x0
        if 0 <= i < h.shape[0] and 0 <= j < h.shape[1] and h[i, j] != hole:
            return float(h[i, j])
        return None

    return height_at


def build_top_surface(stage, factions: list[Faction]) -> Scene:
    from usd_web import greedy_mesh, surface_from_stage
    from usd_web.surface import HOLE

    t0 = time.perf_counter()
    surf, pal = surface_from_stage(stage, include=terrain_only)
    t_surface = time.perf_counter() - t0
    t1 = time.perf_counter()
    pal_lin = display_color(np.array([hex_to_rgb(c) for c in pal.colors])).astype(np.float32)
    heights, colors, sub, (x0, z0) = align_to_chunks(surf.heights, surf.colors, surf.subsurface, surf.origin, HOLE)
    present = heights != HOLE
    floor = int(heights[present].min()) - 3
    scene = Scene(factions=factions)
    quads = 0
    for tz in range(0, heights.shape[0], TILE):
        for tx in range(0, heights.shape[1], TILE):
            sl = (slice(tz, tz + TILE), slice(tx, tx + TILE))
            q = greedy_mesh(heights[sl], colors[sl], sub[sl], chunk_size=CHUNK, floor=floor)
            if not len(q):
                continue
            quads += len(q)
            cx, cz = quad_cells(q, x0 + tx, z0 + tz)
            col = tint_colors(pal_lin[q[:, 6]], cx // CHUNK, cz // CHUNK, factions)
            scene.meshes.append(quads_to_mesh(q, x0 + tx, z0 + tz, col))
    top = heights[present]
    zi, xi = np.nonzero(present)
    scene.bounds = (float(x0 + xi.min()), float(floor), float(z0 + zi.min()),
                    float(x0 + xi.max() + 1), float(top.max() + 1), float(z0 + zi.max() + 1))
    scene.focus_y = float(np.median(top))
    scene.lines = claim_lines(factions, surface_heights(surf, HOLE))
    scene.stats.update(columns=int(present.sum()), quads=quads, meshes=len(scene.meshes),
                       vertices=int(sum(len(m[0]) for m in scene.meshes)),
                       s_read_usd=round(t_surface, 3), s_mesh=round(time.perf_counter() - t1, 3))
    return scene


# --------------------------------------------------------------------------- path B: every block


RENDER_ITEM = np.dtype([("kind", "<i4"), ("position", "<f4", 3), ("scale", "<f4", 3), ("color", "<f4", 4)])
LINE_VERTEX = np.dtype([("position", "<f4", 3), ("color", "<f4", 4)])


def build_cubes(stage, factions: list[Faction]) -> Scene:
    from pxr import Usd, UsdGeom
    from usd_web.stage import prototype_color

    t0 = time.perf_counter()
    parts = []
    for prim in stage.Traverse():
        if not prim.IsA(UsdGeom.PointInstancer) or not str(prim.GetPath()).startswith(TERRAIN_PREFIX):
            continue
        pi = UsdGeom.PointInstancer(prim)
        pos = np.asarray(pi.GetPositionsAttr().Get() or [], dtype=np.float64).reshape(-1, 3)
        if not len(pos):
            continue
        ids = np.asarray(pi.GetProtoIndicesAttr().Get(), dtype=np.int64)
        protos = [stage.GetPrimAtPath(p) for p in pi.GetPrototypesRel().GetTargets()]
        keep_proto = np.array([p.GetName() not in EXCLUDED_BLOCKS for p in protos])
        cols = display_color(np.array([prototype_color(p) for p in protos], dtype=np.float64)).astype(np.float32)
        m = np.array(pi.ComputeLocalToWorldTransform(Usd.TimeCode.Default()), dtype=np.float64)
        world = (np.c_[pos, np.ones(len(pos))] @ m)[:, :3]
        keep = keep_proto[ids]
        world, ids = world[keep], ids[keep]
        rec = np.zeros(len(world), dtype=RENDER_ITEM)
        rec["kind"] = 0  # STRATA_PRIMITIVE_CUBE: centred, so move by half a block
        rec["position"] = world + 0.5
        rec["scale"] = 1.0
        bx, bz = np.floor(world[:, 0]).astype(np.int64), np.floor(world[:, 2]).astype(np.int64)
        rec["color"][:, :3] = tint_colors(cols[ids], bx // CHUNK, bz // CHUNK, factions)
        rec["color"][:, 3] = 1.0
        parts.append(rec)
    if not parts:
        raise PreviewError("the stage has no terrain instances under /World/Terrain", EXIT_INPUT)
    cubes = np.concatenate(parts)
    p = cubes["position"]
    # top of each column, for the camera focus and for tracing claim borders
    bx, by, bz = (np.floor(p[:, 0]).astype(np.int64), np.floor(p[:, 1]).astype(np.int64),
                  np.floor(p[:, 2]).astype(np.int64))
    keys = bx * 1_000_003 + bz
    order = np.lexsort((by, keys))
    last = np.r_[keys[order][1:] != keys[order][:-1], True]
    tops = {(int(x), int(z)): float(y + 1) for x, z, y in zip(bx[order][last], bz[order][last], by[order][last])}
    scene = Scene(cubes=cubes, factions=factions)
    scene.bounds = (float(bx.min()), float(by.min()), float(bz.min()),
                    float(bx.max() + 1), float(by.max() + 1), float(bz.max() + 1))
    scene.focus_y = float(np.median(np.fromiter(tops.values(), dtype=np.float64)))
    scene.lines = claim_lines(factions, lambda x, z: tops.get((x, z)))
    scene.stats.update(cubes=int(len(cubes)), s_read_usd=round(time.perf_counter() - t0, 3))
    return scene


def claim_lines(factions: list[Faction], height_at, layers=(0.5, 1.0, 1.5)) -> np.ndarray:
    """Border segments of every claim as StrataLineVertex records, stacked a few
    times above each other so the one-pixel lines read as a ribbon."""
    owner = chunk_owner(factions)
    out = []
    for i, f in enumerate(factions):
        rgb = display_color(np.array(f.color))
        for edge in border_edges(f.chunks, owner, i):
            for lift in layers:
                for a, b in polyline_segments(border_polyline(edge, height_at, lift)):
                    out.append((a, (*rgb, 1.0)))
                    out.append((b, (*rgb, 1.0)))
    arr = np.zeros(len(out), dtype=LINE_VERTEX)
    if out:
        arr["position"] = [p for p, _ in out]
        arr["color"] = [c for _, c in out]
    return arr


# --------------------------------------------------------------------------- camera, sun, checks


def camera_pose(bounds, focus_y: float, azimuth_deg: float, elevation_deg: float, fov_deg: float, aspect: float):
    """Eye and target framing ``bounds`` from a fixed direction. Azimuth is clockwise
    from north as seen from above (135 = looking from the south-east)."""
    x0, y0, z0, x1, y1, z1 = bounds
    cx, cz = (x0 + x1) / 2, (z0 + z1) / 2
    target = np.array([cx, focus_y, cz])
    radius = 0.5 * math.sqrt((x1 - x0) ** 2 + (z1 - z0) ** 2 + (0.5 * (y1 - y0)) ** 2)
    half = math.radians(fov_deg) / 2
    half = min(half, math.atan(math.tan(half) * aspect))
    dist = radius / math.sin(half) * 0.72
    az, el = math.radians(azimuth_deg), math.radians(elevation_deg)
    # Strata/Minecraft: +X east, -Z north
    direction = np.array([math.sin(az) * math.cos(el), math.sin(el), -math.cos(az) * math.cos(el)])
    return target + dist * direction, target, dist


def parse_time(text: str) -> float:
    t = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.timestamp()


def daylight_times(day_start_utc: float, lon: float, elevation_at, frames: int, min_elevation: float = 4.0,
                   step: float = 60.0) -> list[float]:
    """``frames`` instants spread evenly while the sun is at least ``min_elevation``
    degrees up, on the local solar day containing ``day_start_utc``.
    ``elevation_at(unix_seconds)`` returns the sun's elevation in degrees."""
    noon = day_start_utc - day_start_utc % 86400 + 43200 - lon / 15.0 * 3600
    ts = np.arange(noon - 43200, noon + 43200, step)
    up = [t for t in ts if elevation_at(float(t)) >= min_elevation]
    if not up:
        raise PreviewError("the sun never rises above the horizon on that day and place", EXIT_INPUT)
    a, b = up[0], up[-1]
    if frames == 1:
        return [float((a + b) / 2)]
    return [float(a + (b - a) * i / (frames - 1)) for i in range(frames)]


def backdrop_for(elevation_deg: float) -> tuple[float, float, float]:
    """A flat backdrop colour for a sun ``elevation_deg`` up: warm near the horizon,
    pale blue by 30 degrees. Decoration only; it says nothing about the world."""
    low, high = np.array([0.96, 0.74, 0.52]), np.array([0.62, 0.77, 0.92])
    t = float(np.clip((elevation_deg - 3.0) / 27.0, 0.0, 1.0))
    return tuple(float(v) for v in (1 - t) * low + t * high)


def flat_image(shape, rgb) -> np.ndarray:
    """What a frame with nothing drawn looks like: the backdrop colour everywhere."""
    img = np.zeros(shape, dtype=np.uint8)
    img[..., :3] = np.round(np.asarray(rgb) * 255).astype(np.uint8)
    return img


def coverage(image: np.ndarray, empty: np.ndarray, threshold: int = 12) -> float:
    """Fraction of pixels where ``image`` differs from ``empty`` (a frame with nothing
    drawn) by more than ``threshold`` in any channel. Below a few percent, the preview
    is blank: the stage was empty, the camera missed it, or nothing rasterised."""
    a = np.asarray(image, dtype=np.int16)[..., :3]
    b = np.asarray(empty, dtype=np.int16)[..., :3]
    if a.shape != b.shape:
        raise ValueError("images differ in size")
    return float((np.abs(a - b).max(axis=-1) > threshold).mean())


# --------------------------------------------------------------------------- the Strata library


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def verify_sha256(path: Path, expected: str, *, delete_on_mismatch: bool = True) -> None:
    actual = sha256_of(path)
    if actual != expected.lower():
        if delete_on_mismatch:
            path.unlink(missing_ok=True)
        raise PreviewError(f"{path.name}: SHA-256 {actual} does not match the pinned {expected}", EXIT_STRATA)


def read_lock(path: Path = LOCK) -> dict:
    lock = json.loads(path.read_text(encoding="utf-8"))
    for key in ("version", "asset", "url", "sha256"):
        if not isinstance(lock.get(key), str) or not lock[key]:
            raise PreviewError(f"{path.name}: missing {key}", EXIT_INPUT)
    return lock


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "mcusd" / "strata"


def resolve_library(explicit: str | None, lock: dict, *, download=urllib.request.urlretrieve) -> Path:
    """``--strata``, else ``MCUSD_STRATA_LIB``, else the pinned asset in the cache,
    downloaded and SHA-256 checked on first use. An explicit path is the caller's
    choice and is not hash-checked."""
    explicit = explicit or os.environ.get("MCUSD_STRATA_LIB")
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise PreviewError(f"Strata library not found: {path}", EXIT_STRATA)
        return path
    path = cache_dir() / lock["version"] / lock["asset"]
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_suffix(path.suffix + ".part")
        try:
            download(lock["url"], part)
        except OSError as exc:
            part.unlink(missing_ok=True)
            raise PreviewError(f"could not download {lock['url']}: {exc}", EXIT_STRATA) from None
        verify_sha256(part, lock["sha256"])
        part.replace(path)
    verify_sha256(path, lock["sha256"])
    return path


class EngineDesc(C.Structure):
    _fields_ = [("struct_size", C.c_uint32), ("abi_version", C.c_uint32), ("surface", C.c_int32),
                ("width", C.c_int32), ("height", C.c_int32), ("flags", C.c_uint32), ("title", C.c_char_p),
                ("get_proc_address", C.c_void_p), ("get_proc_address_user", C.c_void_p)]


class Lighting(C.Structure):
    _fields_ = [("mode", C.c_int32), ("legacy_direction", C.c_float * 3), ("legacy_ambient", C.c_float),
                ("time_of_day", C.c_float), ("latitude_degrees", C.c_float), ("cascade_count", C.c_int32),
                ("shadow_map_size", C.c_int32), ("shadow_distance", C.c_float), ("split_lambda", C.c_float),
                ("fog", C.c_int32)]


class SolarPosition(C.Structure):
    _fields_ = [("to_sun", C.c_float * 3), ("elevation_degrees", C.c_float), ("azimuth_degrees", C.c_float),
                ("declination_degrees", C.c_float), ("hour_angle_degrees", C.c_float),
                ("equation_of_time_minutes", C.c_float)]


class Mesh(C.Structure):
    _fields_ = [("positions", C.c_void_p), ("normals", C.c_void_p), ("colors", C.c_void_p),
                ("indices", C.c_void_p), ("vertex_count", C.c_int32), ("index_count", C.c_int32),
                ("color", C.c_float * 4)]


class FrameDesc(C.Structure):
    _fields_ = [("clear_flags", C.c_uint32), ("clear_color", C.c_float * 4)]


# (struct id, size in this binding)
STRUCTS = {0: RENDER_ITEM.itemsize, 1: C.sizeof(EngineDesc), 3: C.sizeof(Lighting), 4: C.sizeof(FrameDesc),
           9: C.sizeof(Mesh), 10: LINE_VERTEX.itemsize, 13: C.sizeof(SolarPosition)}
SURFACE_HEADLESS, LIGHTING_SUN = 1, 1
CLEAR_COLOR_DEPTH = 1 | 2  # a flat backdrop: Strata's analytic sky reads dark without an sRGB encode


class Strata:
    """The slice of Strata's C ABI a preview needs."""

    def __init__(self, path: Path):
        try:
            self.lib = L = C.CDLL(str(path))
        except OSError as exc:
            raise PreviewError(f"cannot load {path}: {exc}", EXIT_STRATA) from None
        for name, res in (("strata_abi_version", C.c_uint32), ("strata_abi_minor", C.c_uint32),
                          ("strata_version_string", C.c_char_p), ("strata_last_error", C.c_char_p),
                          ("strata_struct_size", C.c_int32), ("strata_gpu_description", C.c_char_p)):
            getattr(L, name).restype = res
        L.strata_gpu_description.argtypes = [C.c_void_p]
        L.strata_solar_position.argtypes = [C.c_double, C.c_double, C.c_double, C.POINTER(SolarPosition)]
        major, minor = L.strata_abi_version(), L.strata_abi_minor()
        if major != ABI_MAJOR or minor < ABI_MIN_MINOR:
            raise PreviewError(f"Strata ABI {major}.{minor} is not {ABI_MAJOR}.>={ABI_MIN_MINOR}", EXIT_STRATA)
        for sid, size in STRUCTS.items():
            theirs = L.strata_struct_size(sid)
            if theirs != size:
                raise PreviewError(f"Strata struct {sid} is {theirs} bytes, this script expects {size}", EXIT_STRATA)
        self.version = L.strata_version_string().decode()
        self.engine = None

    def ok(self, status: int, what: str) -> None:
        if status != 0:
            raise PreviewError(f"{what}: status {status}: {self.lib.strata_last_error().decode()}")

    def solar(self, unix: float, lat: float, lon: float) -> SolarPosition:
        sp = SolarPosition()
        self.ok(self.lib.strata_solar_position(unix, lat, lon, C.byref(sp)), "strata_solar_position")
        return sp

    def create(self, width: int, height: int) -> None:
        desc = EngineDesc(C.sizeof(EngineDesc), ABI_MAJOR, SURFACE_HEADLESS, width, height, 0, None, None, None)
        eng = C.c_void_p()
        status = self.lib.strata_create(C.byref(desc), C.byref(eng))
        if status != 0:
            raise PreviewError("strata_create (headless EGL) failed: " + self.lib.strata_last_error().decode()
                               + " - is libEGL.so.1 with a Mesa driver installed?", EXIT_STRATA)
        self.engine = eng
        self.size = (width, height)
        self.gpu = self.lib.strata_gpu_description(eng).decode()

    def lighting(self, shadow_distance: float) -> None:
        light = Lighting()
        self.ok(self.lib.strata_lighting_defaults(LIGHTING_SUN, C.byref(light)), "strata_lighting_defaults")
        light.cascade_count = 4
        light.shadow_map_size = 2048
        light.shadow_distance = shadow_distance
        light.fog = 0
        self.ok(self.lib.strata_set_lighting(self.engine, C.byref(light)), "strata_set_lighting")

    def sun(self, to_sun) -> None:
        self.ok(self.lib.strata_set_sun_direction(self.engine, (C.c_float * 3)(*to_sun)), "strata_set_sun_direction")

    def camera(self, eye, target, fov: float, near: float, far: float) -> None:
        w, h = self.size
        cam = (C.c_uint8 * self.lib.strata_struct_size(2))()
        self.ok(self.lib.strata_camera_look_at((C.c_float * 3)(*eye), (C.c_float * 3)(*target),
                                               (C.c_float * 3)(0, 1, 0), C.c_float(fov), C.c_float(w / h),
                                               C.c_float(near), C.c_float(far), cam), "strata_camera_look_at")
        self.ok(self.lib.strata_set_camera(self.engine, cam), "strata_set_camera")

    def add_mesh(self, pos, nor, col, idx) -> None:
        m = Mesh(pos.ctypes.data, nor.ctypes.data, col.ctypes.data, idx.ctypes.data, len(pos), len(idx),
                 (C.c_float * 4)(1, 1, 1, 1))
        mid = C.c_uint32()
        self.ok(self.lib.strata_add_mesh(self.engine, C.byref(m), C.byref(mid)), "strata_add_mesh")

    def frame(self, cubes=None, lines=None, backdrop=(0.62, 0.77, 0.92)) -> np.ndarray:
        desc = FrameDesc(CLEAR_COLOR_DEPTH, (C.c_float * 4)(*backdrop, 1.0))
        self.ok(self.lib.strata_begin_frame(self.engine, C.byref(desc)), "strata_begin_frame")
        if cubes is not None and len(cubes):
            self.ok(self.lib.strata_submit_primitives(self.engine, C.c_void_p(cubes.ctypes.data), len(cubes)),
                    "strata_submit_primitives")
        if lines is not None and len(lines):
            self.ok(self.lib.strata_submit_lines(self.engine, C.c_void_p(lines.ctypes.data), len(lines)),
                    "strata_submit_lines")
        self.ok(self.lib.strata_end_frame(self.engine), "strata_end_frame")
        w, h = self.size
        buf = np.zeros((h, w, 4), dtype=np.uint8)
        ow, oh = C.c_int32(), C.c_int32()
        self.ok(self.lib.strata_read_pixels_rgba8(self.engine, C.c_void_p(buf.ctypes.data), buf.nbytes,
                                                  C.byref(ow), C.byref(oh)), "strata_read_pixels_rgba8")
        return buf

    def destroy(self) -> None:
        if self.engine:
            self.lib.strata_destroy(self.engine)
            self.engine = None


# --------------------------------------------------------------------------- legend


FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _font(path: str, size: int):
    from PIL import ImageFont

    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default(size)


def annotate(rgba: np.ndarray, factions: list[Faction], caption: str, scale: float = 1.0):
    """The rendered frame as a Pillow image with a faction legend (top left) and a
    caption strip (bottom), as scripts/compose_preview.py does for the Blender images."""
    from PIL import Image, ImageDraw

    img = Image.fromarray(rgba[..., :3].copy())
    draw = ImageDraw.Draw(img, "RGBA")
    s = lambda v: max(1, int(round(v * scale)))  # noqa: E731
    if factions:
        f, hf = _font(FONT, s(15)), _font(FONT_BOLD, s(12))
        heading = "Claims (example data)"
        row = s(22)
        width = s(20) + max([s(28) + int(draw.textlength(x.name, font=f)) for x in factions]
                            + [int(draw.textlength(heading, font=hf))])
        x0, y0 = s(12), s(12)
        draw.rounded_rectangle((x0, y0, x0 + width, y0 + s(10) + row * (len(factions) + 1)), radius=s(6),
                               fill=(255, 255, 255, 215), outline=(210, 214, 222, 255))
        draw.text((x0 + s(10), y0 + s(7)), heading, font=hf, fill=(92, 99, 112))
        for i, fac in enumerate(factions):
            y = y0 + s(6) + row * (i + 1)
            rgb = tuple(int(round(c * 255)) for c in fac.color)
            draw.rounded_rectangle((x0 + s(10), y + s(2), x0 + s(24), y + s(16)), radius=s(3), fill=rgb)
            draw.text((x0 + s(30), y), fac.name, font=f, fill=(34, 38, 46))
    if caption:
        cf = _font(FONT, s(13))
        h = s(24)
        draw.rectangle((0, img.height - h, img.width, img.height), fill=(20, 22, 28, 170))
        draw.text((s(10), img.height - h + s(5)), caption, font=cf, fill=(235, 238, 243))
    return img


# --------------------------------------------------------------------------- main


def open_stage(path: Path):
    from pxr import Tf, Usd

    if not path.is_file():
        raise PreviewError(f"no such stage: {path}", EXIT_INPUT)
    try:
        stage = Usd.Stage.Open(str(path))
    except Tf.ErrorException as exc:
        raise PreviewError(f"cannot open {path}: {str(exc).strip()}", EXIT_INPUT) from None
    if stage is None:
        raise PreviewError(f"cannot open {path}", EXIT_INPUT)
    return stage


def render(args) -> dict:
    stage = open_stage(Path(args.stage))
    strata = Strata(resolve_library(args.strata, read_lock()))  # fail fast, before the slow part
    factions = [] if args.no_claims else read_claims(stage)
    scene = build_cubes(stage, factions) if args.full else build_top_surface(stage, factions)
    w, h = args.size
    out = Path(args.out)
    gif = out.suffix.lower() == ".gif"
    if gif and not args.sequence:
        args.sequence = 24
    unix = parse_time(args.sun)
    try:
        t = time.perf_counter()
        strata.create(w, h)
        scene.stats["s_strata_create"] = round(time.perf_counter() - t, 3)
        x0, y0, z0, x1, y1, z1 = scene.bounds
        span = max(x1 - x0, z1 - z0, y1 - y0)
        strata.lighting(shadow_distance=span * 2.5)
        eye, target, dist = camera_pose(scene.bounds, scene.focus_y, args.azimuth, args.elevation, args.fov, w / h)
        strata.camera(eye, target, args.fov, max(0.5, dist / 400), dist * 4)
        sp = strata.solar(unix, args.lat, args.lon)
        strata.sun(sp.to_sun)
        backdrop = backdrop_for(sp.elevation_degrees)
        t = time.perf_counter()
        for m in scene.meshes:
            strata.add_mesh(*m)
        scene.stats["s_mesh_upload"] = round(time.perf_counter() - t, 3)
        frame_times = []

        def draw(bg=None):
            t0 = time.perf_counter()
            px = strata.frame(scene.cubes, scene.lines, backdrop=bg or backdrop)
            frame_times.append(round(time.perf_counter() - t0, 3))
            return px

        mode = "every visible block as a cube" if args.full else "top surface"
        where = f"{abs(args.lat):g}{'N' if args.lat >= 0 else 'S'} {abs(args.lon):g}{'E' if args.lon >= 0 else 'W'}"
        if args.sequence:
            times = daylight_times(unix, args.lon, lambda u: strata.solar(u, args.lat, args.lon).elevation_degrees,
                                   args.sequence)
            images = []
            draw()  # warm-up
            for u in times:
                pos = strata.solar(u, args.lat, args.lon)
                strata.sun(pos.to_sun)
                bg = backdrop_for(pos.elevation_degrees)
                px = draw(bg)
                cov = coverage(px, flat_image(px.shape, bg))
                if cov < args.min_coverage:
                    raise PreviewError(f"frame at {u} is blank ({cov:.1%} drawn)")
                stamp = dt.datetime.fromtimestamp(u, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                el = strata.solar(u, args.lat, args.lon).elevation_degrees
                caption = args.caption or f"Strata {strata.version}, {mode}; sun at {stamp}, {where} ({el:.0f} deg up)"
                images.append(annotate(px, scene.factions if not args.no_legend else [], caption, w / 960))
            scene.stats["coverage"] = round(cov, 3)
            write_gif(images, out, args.gif_width, args.gif_ms,
                      [] if args.no_legend else [f.color for f in scene.factions])
        else:
            draw()  # shadows settle on the second frame
            px = draw()
            cov = coverage(px, flat_image(px.shape, backdrop))
            scene.stats["coverage"] = round(cov, 3)
            if cov < args.min_coverage:
                raise PreviewError(f"the preview is blank: only {cov:.2%} of pixels differ from an empty frame")
            stamp = dt.datetime.fromtimestamp(unix, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            caption = args.caption or f"Strata {strata.version}, {mode}; sun at {stamp}, {where}"
            img = annotate(px, scene.factions if not args.no_legend else [], caption, w / 960)
            out.parent.mkdir(parents=True, exist_ok=True)
            img.save(out, optimize=True)
        scene.stats.update(strata=strata.version, gpu=strata.gpu, s_frames=frame_times,
                           claim_line_vertices=int(len(scene.lines)) if scene.lines is not None else 0,
                           factions=len(scene.factions), out=str(out))
    finally:
        strata.destroy()
    return scene.stats


def write_gif(images, out: Path, width: int | None, ms: int, swatches=()) -> None:
    from PIL import Image

    if width and images[0].width > width:
        h = round(images[0].height * width / images[0].width)
        images = [im.resize((width, h), Image.LANCZOS) for im in images]
    # one palette for every frame, so legend swatches and claim colours do not flicker
    # (with a band of each faction colour, so the legend's swatches survive the cut)
    w, h = images[0].size
    band = h // 2
    sheet = Image.new("RGB", (w, h * len(images) + band * len(swatches)))
    for i, im in enumerate(images):
        sheet.paste(im, (0, i * h))
    for i, rgb in enumerate(swatches):
        sheet.paste(tuple(int(round(c * 255)) for c in rgb), (0, h * len(images) + i * band, w, h * len(images) + (i + 1) * band))
    palette = sheet.quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    pal = [im.quantize(palette=palette, dither=Image.Dither.NONE) for im in images]
    out.parent.mkdir(parents=True, exist_ok=True)
    pal[0].save(out, save_all=True, append_images=pal[1:], duration=ms, loop=0, optimize=True)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("stage", help="world.usda (or any layer the exporter wrote)")
    p.add_argument("out", help="output .png, or .gif for a sun sequence")
    p.add_argument("--strata", help="path to libstrata (default: MCUSD_STRATA_LIB, else the pinned release)")
    p.add_argument("--size", nargs=2, type=int, default=[960, 540], metavar=("W", "H"))
    p.add_argument("--full", action="store_true", help="every visible block as a cube (path B), not the top surface")
    p.add_argument("--sun", default=DEFAULT_SUN, help="UTC date and time of the sun (ISO 8601)")
    p.add_argument("--lat", type=float, default=DEFAULT_LAT)
    p.add_argument("--lon", type=float, default=DEFAULT_LON)
    p.add_argument("--azimuth", type=float, default=135.0, help="camera direction, degrees clockwise from north")
    p.add_argument("--elevation", type=float, default=38.0, help="camera elevation in degrees")
    p.add_argument("--fov", type=float, default=40.0)
    p.add_argument("--no-claims", action="store_true", help="ignore the claims overlay")
    p.add_argument("--no-legend", action="store_true")
    p.add_argument("--caption", help="replace the generated caption")
    p.add_argument("--sequence", type=int, default=0, metavar="N",
                   help="N frames from sunrise to sunset on --sun's date, written as a GIF")
    p.add_argument("--gif-width", type=int, default=640)
    p.add_argument("--gif-ms", type=int, default=160, help="GIF frame duration")
    p.add_argument("--min-coverage", type=float, default=0.02,
                   help="fail if fewer than this fraction of pixels differ from an empty frame")
    p.add_argument("--stats-json", help="also write timings and counts as JSON")
    args = p.parse_args(argv)
    if args.sequence < 0 or (args.sequence and Path(args.out).suffix.lower() != ".gif"):
        p.error("--sequence writes a .gif")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    t = time.perf_counter()
    try:
        stats = render(args)
    except PreviewError as exc:
        print(f"render_strata_preview: {exc}", file=sys.stderr)
        return exc.code
    stats["s_total"] = round(time.perf_counter() - t, 3)
    text = json.dumps(stats, indent=1)
    if args.stats_json:
        Path(args.stats_json).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
