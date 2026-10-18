"""Generate the committed fixture worlds under ``tests/fixtures/``.

Recipe (ARCHITECTURE.md § Fixture world recipe): for each pinned version, download
the official, unmodified vanilla ``server.jar`` from Mojang's version manifest
(verifying its SHA-1), run it in a throwaway temporary directory with a fixed
seed and ``generate-structures=false``, force-load the four chunks around the
origin (chunk X and Z in {-1, 0}), save, stop, then keep only those chunks
(rewritten into fresh zlib region files) and ``level.dat``. Everything else —
player data, logs, other server files — is deleted with the temporary directory.
No player ever joins.

Running a server requires accepting the Minecraft EULA
(https://aka.ms/MinecraftEULA); pass ``--accept-eula`` to do so for these
throwaway servers. CI never runs this script; it only reads the committed output.

Usage (from the repository root, in the project virtualenv)::

    python scripts/make_fixtures.py --accept-eula \\
        --java 1.21.11=/path/to/java21/bin/java --java 26.3=/path/to/java25/bin/java

Writes ``tests/fixtures/world-<version>/``, ``tests/fixtures/fixtures.json``
(versions, hashes, seeds and the counts the end-to-end tests assert) and
``tests/fixtures/RECIPE.md``.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from mcusd.blocks import BlockCatalogue  # noqa: E402
from mcusd.cull import cull_region  # noqa: E402
from mcusd.select import BBox, ChunkSource, iter_region_volumes, region_volume  # noqa: E402
from mcusd.world.layout import locate  # noqa: E402
from mcusd.world.region import RegionFile, parse_nbt, write_region  # noqa: E402

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"

# Exactly pinned versions and seeds. Change only by regenerating.
FIXTURES = {
    "1.21.11": {"seed": 8675309, "layout": "pre-26.1"},
    "26.3": {"seed": 42, "layout": "26.1+"},
}
KEPT_CHUNKS = [(-1, -1), (0, -1), (-1, 0), (0, 0)]
FORCELOAD = "forceload add -16 -16 15 15"
BBOX = BBox(-16, -64, -16, 15, 319, 15)  # exactly the four kept chunks, full overworld height
SAMPLE_COLUMNS = [(-16, -16), (-1, -1), (0, 0), (15, 15), (-16, 15), (7, -9)]


def fetch_json(url: str):
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def download_server(version: str, dest: Path) -> dict:
    manifest = fetch_json(MANIFEST_URL)
    entry = next((v for v in manifest["versions"] if v["id"] == version), None)
    if entry is None:
        raise SystemExit(f"version {version} not found in the Mojang version manifest")
    meta = fetch_json(entry["url"])
    server = meta["downloads"]["server"]
    jar = dest / "server.jar"
    with urllib.request.urlopen(server["url"], timeout=300) as response, open(jar, "wb") as f:
        shutil.copyfileobj(response, f)
    data = jar.read_bytes()
    if hashlib.sha1(data).hexdigest() != server["sha1"]:
        raise SystemExit(f"{version}: server.jar SHA-1 does not match the manifest")
    return {
        "jar": jar,
        "sha1": server["sha1"],
        "sha256": hashlib.sha256(data).hexdigest(),
        "url": server["url"],
        "javaMajorVersion": meta.get("javaVersion", {}).get("majorVersion"),
    }


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_server(java: str, jar: Path, workdir: Path, seed: int) -> None:
    (workdir / "eula.txt").write_text("eula=true\n")
    properties = {
        "level-name": "world",
        "level-seed": str(seed),
        "level-type": "minecraft\\:normal",
        "generate-structures": "false",
        "server-port": str(free_port()),
        "online-mode": "false",
        "white-list": "true",
        "enforce-whitelist": "true",
        "max-players": "1",
        "enable-rcon": "false",
        "enable-query": "false",
        "pause-when-empty-seconds": "-1",
        "sync-chunk-writes": "true",
    }
    (workdir / "server.properties").write_text("".join(f"{k}={v}\n" for k, v in properties.items()))
    process = subprocess.Popen([java, "-Xmx2G", "-jar", str(jar), "--nogui"], cwd=workdir,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, bufsize=1)
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: [lines.put(line) for line in process.stdout], daemon=True).start()

    def wait_for(text: str, timeout: float = 600) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                line = lines.get(timeout=1)
            except queue.Empty:
                if process.poll() is not None:
                    raise RuntimeError("server exited unexpectedly")
                continue
            if text in line:
                return
        raise TimeoutError(f"server did not print {text!r}")

    def command(text: str) -> None:
        process.stdin.write(text + "\n")
        process.stdin.flush()

    try:
        wait_for("Done (")
        command(FORCELOAD)
        wait_for("force loaded")
        time.sleep(20)  # let the forced chunks finish generating
        command("save-all flush")
        wait_for("Saved the game")
        command("stop")
        process.wait(timeout=300)
    finally:
        if process.poll() is None:
            process.kill()


def trim_world(server_world: Path, target: Path) -> None:
    """Copy level.dat and only the kept chunks (rewritten with zlib) into ``target``."""
    location = locate(server_world, "overworld")
    region_rel = location.region_dir.relative_to(server_world)
    if target.exists():
        shutil.rmtree(target)
    (target / region_rel).mkdir(parents=True)
    shutil.copy2(server_world / "level.dat", target / "level.dat")
    by_region: dict[tuple[int, int], dict[tuple[int, int], bytes]] = {}
    for cx, cz in KEPT_CHUNKS:
        region = RegionFile(location.region_dir / f"r.{cx >> 5}.{cz >> 5}.mca")
        raw = region.read_chunk(cx & 31, cz & 31)
        if raw is None:
            raise SystemExit(f"chunk ({cx}, {cz}) was not saved")
        status = parse_nbt(raw)["Status"].value
        if status != "minecraft:full":
            raise SystemExit(f"chunk ({cx}, {cz}) has status {status}, expected minecraft:full")
        by_region.setdefault((cx >> 5, cz >> 5), {})[(cx & 31, cz & 31)] = raw
    for (rx, rz), chunks in by_region.items():
        write_region(target / region_rel / f"r.{rx}.{rz}.mca", chunks)


def assert_no_player_data(world: Path) -> None:
    files = sorted(p.relative_to(world).as_posix() for p in world.rglob("*") if p.is_file())
    allowed = {"level.dat"} | {p for p in files if p.endswith(".mca")}
    if set(files) != allowed:
        raise SystemExit(f"unexpected files in {world}: {sorted(set(files) - allowed)}")
    level = parse_nbt(gzip.decompress((world / "level.dat").read_bytes()))
    if "Player" in level["Data"]:
        raise SystemExit(f"{world}/level.dat contains a Player tag")


def record_counts(world: Path) -> dict:
    location = locate(world, "overworld")
    catalogue = BlockCatalogue()
    source = ChunkSource(location.region_dir, location.dimension)
    regions, total = {}, 0
    for volume in iter_region_volumes(source, BBOX):
        result = cull_region(volume, catalogue)
        regions[f"r.{volume.region_x}.{volume.region_z}"] = {"visible": result.kept, "hidden": result.culled}
        total += result.kept
    samples = []
    for x, z in SAMPLE_COLUMNS:
        volume = region_volume(source, BBOX, x >> 9, z >> 9)
        column = [volume.block_at(x, y, z) for y in range(-64, 320)]
        top = max(y for y in range(-64, 320) if column[y + 64] != "minecraft:air")
        for y in (-64, 0, top):
            samples.append({"x": x, "y": y, "z": z, "block": column[y + 64]})
    return {"bbox": BBOX.as_list(), "visible": total, "regions": regions, "samples": samples}


def java_version(java: str) -> str:
    out = subprocess.run([java, "-version"], capture_output=True, text=True).stderr.splitlines()
    return out[0].strip() if out else "unknown"


def write_recipe(fixtures_dir: Path, records: dict) -> None:
    rows = "\n".join(
        f"| `{v}` | `world-{v}/` | {r['layout']} | `{r['seed']}` | `{r['serverJarSha256']}` | {r['java']} |"
        for v, r in records.items()
    )
    counts = "\n".join(
        f"| `{v}` | {r['counts']['visible']} | "
        + ", ".join(f"`{k}`: {c['visible']}" for k, c in r["counts"]["regions"].items()) + " |"
        for v, r in records.items()
    )
    text = f"""# Fixture worlds

Generated by `scripts/make_fixtures.py` (see the
[fixture recipe](../../ARCHITECTURE.md#fixture-world-recipe)); do not edit by hand. To
regenerate, run the script again: it changes these files only by regenerating them.

Each fixture is a fresh world made by a throwaway, unmodified vanilla server
(`server.jar` from Mojang's version manifest, SHA-1 checked against the manifest),
run with `level-type=minecraft:normal`, `generate-structures=false` and the seed
below. The four chunks with chunk X and Z in {{-1, 0}} (blocks -16..15 on X and Z, in
the four region files `r.-1.-1`, `r.-1.0`, `r.0.-1`, `r.0.0`) were force-loaded
(`/{FORCELOAD}`), saved (`/save-all flush`) and the server stopped. Only those
chunks, rewritten with zlib compression, and `level.dat` are committed. No player
ever joined; `playerdata/`, `stats/`, `advancements/`, `usercache.json`, logs and
every other server file were deleted with the temporary directory.

| Version | Folder | Layout | Seed | `server.jar` SHA-256 | Java used |
|---|---|---|---|---|---|
{rows}

World generation is not bit-for-bit reproducible between runs (decoration such as
trees near the force-loaded chunks can differ slightly), so regenerating may change
the recorded counts; `fixtures.json` is always rewritten together with the worlds.

## Recorded counts

For the box `{BBOX.as_list()}` (the four kept chunks, full overworld height), with the
built-in block catalogue. `fixtures.json` holds the per-region numbers and sample
block IDs that `tests/test_end_to_end.py` asserts.

| Version | Visible blocks | Per region |
|---|---|---|
{counts}

The exporter version that recorded the counts is stored in `fixtures.json`.
Running the servers required accepting the [Minecraft EULA](https://aka.ms/MinecraftEULA).
"""
    (fixtures_dir / "RECIPE.md").write_text(text, encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--accept-eula", action="store_true",
                        help="accept the Minecraft EULA for the throwaway servers (required)")
    parser.add_argument("--java", action="append", default=[], metavar="VERSION=PATH",
                        help="java executable per version (default: 'java' on PATH)")
    parser.add_argument("--only", choices=sorted(FIXTURES), help="regenerate one fixture only")
    parser.add_argument("--out", type=Path, default=REPO / "tests" / "fixtures")
    parser.add_argument("--recipe-only", action="store_true",
                        help="re-render RECIPE.md from the existing fixtures.json without running servers")
    args = parser.parse_args(argv)
    if args.recipe_only:
        write_recipe(args.out, json.loads((args.out / "fixtures.json").read_text())["fixtures"])
        return 0
    if not args.accept_eula:
        parser.error("running a Minecraft server requires --accept-eula (https://aka.ms/MinecraftEULA)")
    javas = dict(item.split("=", 1) for item in args.java)
    args.out.mkdir(parents=True, exist_ok=True)
    record_path = args.out / "fixtures.json"
    records = json.loads(record_path.read_text())["fixtures"] if record_path.exists() else {}
    from mcusd import __version__

    for version, spec in FIXTURES.items():
        if args.only and version != args.only:
            continue
        java = javas.get(version, "java")
        with tempfile.TemporaryDirectory(prefix=f"mcusd-fixture-{version}-") as tmp:
            tmp = Path(tmp)
            print(f"[{version}] downloading server.jar", flush=True)
            server = download_server(version, tmp)
            print(f"[{version}] running server (seed {spec['seed']})", flush=True)
            run_server(java, server["jar"], tmp, spec["seed"])
            target = args.out / f"world-{version}"
            trim_world(tmp / "world", target)
        assert_no_player_data(target)
        location = locate(target, "overworld")
        if location.layout != spec["layout"]:
            raise SystemExit(f"{version}: expected layout {spec['layout']}, got {location.layout}")
        records[version] = {
            "folder": f"world-{version}",
            "layout": spec["layout"],
            "seed": spec["seed"],
            "serverJarSha1": server["sha1"],
            "serverJarSha256": server["sha256"],
            "serverJarUrl": server["url"],
            "java": java_version(java),
            "keptChunks": [list(c) for c in KEPT_CHUNKS],
            "counts": record_counts(target),
        }
        print(f"[{version}] {records[version]['counts']['visible']} visible blocks", flush=True)
    record_path.write_text(json.dumps({"exporterVersion": __version__, "fixtures": records}, indent=2) + "\n")
    write_recipe(args.out, records)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
