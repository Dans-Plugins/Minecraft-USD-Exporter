"""AC10: the fixture generator's offline steps (``scripts/make_fixtures.py``).

Characterizes the parts of the recipe that run without a server or network:
trimming a saved world to the four kept chunks, the no-player-data guard, the
recorded counts and the rendered ``RECIPE.md``. Downloading and running the
vanilla servers is not exercised here.
"""

import gzip
import importlib.util
import json
import shutil
import struct
from pathlib import Path

import pytest
from nbt import nbt

from mcusd.world import region as R
from mcusd.world.region import RegionFile, write_region
from tests.nbt_builders import chunk_bytes, to_bytes

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
RECORD = json.loads((FIXTURES / "fixtures.json").read_text())["fixtures"]
VERSIONS = sorted(RECORD)

_spec = importlib.util.spec_from_file_location("make_fixtures", ROOT / "scripts" / "make_fixtures.py")
mf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mf)

REGION_DIRS = {"pre-26.1": "region", "26.1+": "dimensions/minecraft/overworld/region"}
KEPT_FILES = [f"r.{rx}.{rz}.mca" for rx in (-1, 0) for rz in (-1, 0)]


def level_dat(*, player: bool = False) -> bytes:
    root = nbt.NBTFile()
    root.name = ""
    data = nbt.TAG_Compound(name="Data")
    data.tags.append(nbt.TAG_String(name="LevelName", value="world"))
    if player:
        data.tags.append(nbt.TAG_Compound(name="Player"))
    root.tags.append(data)
    return gzip.compress(to_bytes(root))


def server_world(root: Path, chunks, *, layout: str = "pre-26.1", partial=(), player: bool = False) -> Path:
    """A world as a server leaves it: gzip-compressed regions, player data and other files."""
    region_dir = root / REGION_DIRS[layout]
    region_dir.mkdir(parents=True)
    (root / "level.dat").write_bytes(level_dat(player=player))
    by_region = {}
    for cx, cz in chunks:
        status = "minecraft:noise" if (cx, cz) in partial else "minecraft:full"
        by_region.setdefault((cx >> 5, cz >> 5), {})[(cx & 31, cz & 31)] = chunk_bytes(cx, cz, status=status)
    for (rx, rz), region_chunks in by_region.items():
        write_region(region_dir / f"r.{rx}.{rz}.mca", region_chunks, scheme=R.GZIP)
    for rel in ("playerdata/0000.dat", "stats/0000.json", "advancements/0000.json", "session.lock"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(b"x")
    return root


def files_under(world: Path) -> list[str]:
    return sorted(p.relative_to(world).as_posix() for p in world.rglob("*") if p.is_file())


def scheme_byte(path: Path, local_x: int, local_z: int) -> int:
    data = path.read_bytes()
    (location,) = struct.unpack_from(">I", data, 4 * (local_x + 32 * local_z))
    return data[(location >> 8) * 4096 + 4]


# ------------------------------------------------------------------ trim_world


@pytest.mark.parametrize("layout", ["pre-26.1", "26.1+"])
def test_trim_world_keeps_level_dat_and_the_four_chunks_as_zlib(tmp_path, layout):
    extra = [(1, 0), (-2, -1), (0, 32)]  # (0, 32) lives in r.0.1, which must not be copied
    source = server_world(tmp_path / "server", mf.KEPT_CHUNKS + extra, layout=layout)
    target = tmp_path / "trimmed"
    mf.trim_world(source, target)

    region_rel = REGION_DIRS[layout]
    assert files_under(target) == sorted(["level.dat"] + [f"{region_rel}/{name}" for name in KEPT_FILES])
    assert (target / "level.dat").read_bytes() == (source / "level.dat").read_bytes()
    for cx, cz in mf.KEPT_CHUNKS:
        name = f"r.{cx >> 5}.{cz >> 5}.mca"
        trimmed = RegionFile(target / region_rel / name)
        assert [trimmed.chunk_coords(*c) for c in trimmed.present_chunks()] == [(cx, cz)]
        original = RegionFile(source / region_rel / name)
        assert trimmed.read_chunk(cx & 31, cz & 31) == original.read_chunk(cx & 31, cz & 31)
        assert scheme_byte(source / region_rel / name, cx & 31, cz & 31) == R.GZIP
        assert scheme_byte(target / region_rel / name, cx & 31, cz & 31) == R.ZLIB


def test_trim_world_replaces_an_existing_target(tmp_path):
    source = server_world(tmp_path / "server", mf.KEPT_CHUNKS)
    target = tmp_path / "trimmed"
    (target / "playerdata").mkdir(parents=True)
    (target / "playerdata" / "stale.dat").write_bytes(b"old")
    mf.trim_world(source, target)
    assert files_under(target) == sorted(["level.dat"] + [f"region/{name}" for name in KEPT_FILES])


def test_trim_world_rejects_a_kept_chunk_that_was_not_saved(tmp_path):
    chunks = [c for c in mf.KEPT_CHUNKS if c != (0, 0)] + [(1, 0)]  # r.0.0 exists, chunk (0, 0) does not
    source = server_world(tmp_path / "server", chunks)
    with pytest.raises(SystemExit, match=r"chunk \(0, 0\) was not saved"):
        mf.trim_world(source, tmp_path / "trimmed")


def test_trim_world_rejects_an_unfinished_kept_chunk(tmp_path):
    source = server_world(tmp_path / "server", mf.KEPT_CHUNKS, partial={(0, -1)})
    with pytest.raises(SystemExit, match=r"chunk \(0, -1\) has status minecraft:noise, expected minecraft:full"):
        mf.trim_world(source, tmp_path / "trimmed")


def test_trim_world_copies_level_dat_unchanged_so_the_player_guard_must_catch_it(tmp_path):
    """trim_world does not strip a Player tag; assert_no_player_data is what refuses it."""
    source = server_world(tmp_path / "server", mf.KEPT_CHUNKS, player=True)
    target = tmp_path / "trimmed"
    mf.trim_world(source, target)
    with pytest.raises(SystemExit, match="contains a Player tag"):
        mf.assert_no_player_data(target)


# ------------------------------------------------------------------ assert_no_player_data


@pytest.mark.parametrize("version", VERSIONS)
def test_committed_fixtures_pass_the_player_data_guard(version):
    mf.assert_no_player_data(FIXTURES / RECORD[version]["folder"])


def test_a_freshly_trimmed_world_passes_the_player_data_guard(tmp_path):
    source = server_world(tmp_path / "server", mf.KEPT_CHUNKS)
    mf.trim_world(source, tmp_path / "trimmed")
    mf.assert_no_player_data(tmp_path / "trimmed")


@pytest.mark.parametrize("extra", ["playerdata/0000.dat", "usercache.json", "region/c.0.0.mcc"])
def test_player_data_guard_rejects_any_file_but_level_dat_and_regions(tmp_path, extra):
    source = server_world(tmp_path / "server", mf.KEPT_CHUNKS)
    target = tmp_path / "trimmed"
    mf.trim_world(source, target)
    (target / extra).parent.mkdir(parents=True, exist_ok=True)
    (target / extra).write_bytes(b"x")
    with pytest.raises(SystemExit, match="unexpected files") as excinfo:
        mf.assert_no_player_data(target)
    assert str(excinfo.value).endswith(f"{[extra]}")


# ------------------------------------------------------------------ recorded counts and RECIPE.md


@pytest.mark.parametrize("version", VERSIONS)
def test_record_counts_reproduce_the_committed_record(version):
    assert mf.record_counts(FIXTURES / RECORD[version]["folder"]) == RECORD[version]["counts"]


def test_write_recipe_reproduces_the_committed_recipe(tmp_path):
    mf.write_recipe(tmp_path, RECORD)
    assert (tmp_path / "RECIPE.md").read_text(encoding="utf-8") == (FIXTURES / "RECIPE.md").read_text(encoding="utf-8")


def test_recipe_only_rerenders_recipe_from_fixtures_json(tmp_path):
    shutil.copy2(FIXTURES / "fixtures.json", tmp_path / "fixtures.json")
    assert mf.main(["--recipe-only", "--out", str(tmp_path)]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["RECIPE.md", "fixtures.json"]
    assert (tmp_path / "RECIPE.md").read_text(encoding="utf-8") == (FIXTURES / "RECIPE.md").read_text(encoding="utf-8")


def test_running_servers_requires_accepting_the_eula(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        mf.main(["--out", str(tmp_path / "out")])
    assert excinfo.value.code == 2
    assert "--accept-eula" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()  # refused before anything is downloaded or written
