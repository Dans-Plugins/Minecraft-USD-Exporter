"""Block catalogue.

Maps a namespaced block ID to a stable prim-safe name, a display colour and an
"opaque full cube" flag. Built-in table plus an override file.

Colours are flat approximations chosen for this project; no Mojang textures or
models are read, shipped or derived. Occlusion follows an explicit allow-list plus
suffix rules; **unknown blocks are non-occluding**, so a catalogue gap keeps
extra blocks rather than opening holes in the terrain.
"""

from __future__ import annotations

import colorsys
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

AIR_IDS = frozenset({"minecraft:air", "minecraft:cave_air", "minecraft:void_air"})

Color = tuple[float, float, float]


def _hex(value: str) -> Color:
    value = value.strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        raise ValueError(f"expected a #rrggbb colour, got {value!r}")
    return tuple(int(value[i : i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


# Built-in colours (approximate, chosen by eye) for common vanilla blocks.
_COLORS = {
    "stone": "7f7f7f", "granite": "9a6b57", "diorite": "bcbcbc", "andesite": "888888",
    "deepslate": "4d4d52", "cobbled_deepslate": "4a4a4f", "tuff": "6c6d66", "calcite": "dfe0dc",
    "bedrock": "333333", "dirt": "866043", "coarse_dirt": "77553b", "rooted_dirt": "8f6a4f",
    "grass_block": "5f9f35", "podzol": "5b3f1f", "mycelium": "6f6265", "mud": "3c393d",
    "clay": "a0a6b3", "gravel": "837e7b", "sand": "dbd3a0", "red_sand": "be6621",
    "sandstone": "d8cb9b", "red_sandstone": "b5621f", "snow": "f5fbfb", "snow_block": "f5fbfb",
    "powder_snow": "f2f8f8", "ice": "91b7fd", "packed_ice": "8db4fa", "blue_ice": "74a8fd",
    "water": "3f6ff0", "lava": "d4600f", "obsidian": "100c1c", "netherrack": "6f3634",
    "soul_sand": "513e32", "soul_soil": "4b392e", "basalt": "4f4f55", "blackstone": "2a2328",
    "glowstone": "ac8754", "end_stone": "dbde9e", "cobblestone": "7a7a7a",
    "mossy_cobblestone": "6e7a5e", "moss_block": "596e2d", "dripstone_block": "866b5d",
    "pointed_dripstone": "86705f", "amethyst_block": "8562bf", "glass": "c0e0ef",
    "oak_leaves": "4a7a24", "spruce_leaves": "3a5c3a", "birch_leaves": "5a7f3a",
    "jungle_leaves": "3c8a1c", "acacia_leaves": "4a7a1c", "dark_oak_leaves": "3a6a1a",
    "mangrove_leaves": "4f7a2a", "cherry_leaves": "e5aed0", "azalea_leaves": "5a7d2c",
    "short_grass": "5f9f35", "tall_grass": "5f9f35", "fern": "548f3a", "large_fern": "548f3a",
    "seagrass": "2f7a2f", "tall_seagrass": "2f7a2f", "kelp": "3a7a2a", "kelp_plant": "3a7a2a",
    "glow_lichen": "70857a", "vine": "3f6e1f", "lily_pad": "2a6a24", "sugar_cane": "94c065",
    "cactus": "5a8a2e", "pumpkin": "c6761d", "melon": "6f9124", "dandelion": "f2de3d",
    "poppy": "c4271b", "bubble_column": "3f6ff0", "coal_block": "1a1a1a",
    "iron_block": "dcdcdc", "gold_block": "f6d03d", "diamond_block": "62ede4",
    "emerald_block": "2acb57", "lapis_block": "1f43a3", "redstone_block": "af1805",
    "raw_iron_block": "a6876b", "raw_copper_block": "9a6a4f", "raw_gold_block": "dda92e",
    "copper_block": "c06c50", "bricks": "966153", "stone_bricks": "7a7a7a",
    "mossy_stone_bricks": "737a69", "bookshelf": "6b5434", "crafting_table": "7f5d38",
    "furnace": "6e6e6e", "torch": "f5d06a", "farmland": "5a3b1f", "dirt_path": "947a41",
    "magma_block": "8e3f1a", "prismarine": "63a29a", "sea_lantern": "acc7be", "sponge": "c3c04a",
    "terracotta": "985e43", "hay_block": "a68b0c", "bone_block": "e1ddc9",
}

_DYE = {
    "white": "e9ecec", "orange": "f07613", "magenta": "bd44b3", "light_blue": "3aafd9",
    "yellow": "f8c527", "lime": "70b919", "pink": "ed8dac", "gray": "3e4447",
    "light_gray": "8e8e86", "cyan": "158991", "purple": "792aac", "blue": "35399d",
    "brown": "724728", "green": "546d1b", "red": "a12722", "black": "141519",
}

_WOOD = {
    "oak": "a2834f", "spruce": "735531", "birch": "c0af79", "jungle": "a07351",
    "acacia": "a85a32", "dark_oak": "432b14", "mangrove": "773636", "cherry": "e2b2a6",
    "pale_oak": "e3d9d6", "bamboo": "c3ad51", "crimson": "653047", "warped": "2b6863",
}

# Opaque full cubes: explicit allow-list ...
_OPAQUE = frozenset({
    "stone", "granite", "polished_granite", "diorite", "polished_diorite", "andesite",
    "polished_andesite", "deepslate", "cobbled_deepslate", "polished_deepslate", "tuff",
    "calcite", "bedrock", "dirt", "coarse_dirt", "rooted_dirt", "grass_block", "podzol",
    "mycelium", "mud", "packed_mud", "clay", "gravel", "sand", "red_sand", "sandstone",
    "chiseled_sandstone", "cut_sandstone", "smooth_sandstone", "red_sandstone",
    "cut_red_sandstone", "smooth_red_sandstone", "snow_block", "packed_ice", "blue_ice",
    "obsidian", "crying_obsidian", "netherrack", "soul_sand", "soul_soil", "basalt",
    "polished_basalt", "smooth_basalt", "blackstone", "polished_blackstone", "glowstone",
    "end_stone", "cobblestone", "mossy_cobblestone", "moss_block", "dripstone_block",
    "amethyst_block", "budding_amethyst", "coal_block", "iron_block", "gold_block",
    "diamond_block", "emerald_block", "lapis_block", "redstone_block", "raw_iron_block",
    "raw_copper_block", "raw_gold_block", "copper_block", "exposed_copper",
    "weathered_copper", "oxidized_copper", "netherite_block", "quartz_block",
    "bookshelf", "crafting_table", "furnace", "magma_block", "prismarine", "dark_prismarine",
    "sea_lantern", "sponge", "wet_sponge", "terracotta", "hay_block", "bone_block",
    "pumpkin", "carved_pumpkin", "jack_o_lantern", "melon", "note_block", "jukebox",
    "target", "dried_kelp_block", "nether_wart_block", "warped_wart_block", "shroomlight",
    "mushroom_stem", "brown_mushroom_block", "red_mushroom_block", "sculk", "reinforced_deepslate",
    "ancient_debris", "lodestone", "smithing_table", "fletching_table", "cartography_table",
    "loom", "barrel", "smoker", "blast_furnace", "dispenser", "dropper", "observer",
    "tnt", "purpur_block", "purpur_pillar", "quartz_pillar", "chiseled_quartz_block",
    "quartz_bricks", "smooth_quartz", "smooth_stone", "honeycomb_block", "ochre_froglight",
    "verdant_froglight", "pearlescent_froglight", "resin_block",
})
# ... plus suffix rules.
_OPAQUE_SUFFIXES = (
    "_planks", "_ore", "_concrete", "_concrete_powder", "_wool", "_terracotta", "_bricks",
    "_log", "_wood", "_stem", "_hyphae", "_tiles", "_copper",
)
# Suffix matches that are nevertheless not opaque full cubes.
_NOT_OPAQUE = frozenset({
    "chiseled_bookshelf", "pumpkin_stem", "melon_stem", "attached_pumpkin_stem",
    "attached_melon_stem",
})


@dataclass(frozen=True)
class BlockInfo:
    block_id: str
    prim_name: str
    color: Color
    opaque: bool


def split_id(block_id: str) -> tuple[str, str]:
    if ":" in block_id:
        namespace, path = block_id.split(":", 1)
    else:
        namespace, path = "minecraft", block_id
    return namespace, path


def prim_name(block_id: str) -> str:
    """Stable, valid USD prim name for a block ID; distinct IDs give distinct names.

    ``minecraft:<path>`` with a path of ``[a-z0-9_]`` (every vanilla block) maps to
    the bare path, which is injective. Any other ID is sanitised and suffixed with
    an 8-hex-digit hash of the full ID, so it cannot collide with a vanilla name.
    """
    namespace, path = split_id(block_id)
    if namespace == "minecraft" and re.fullmatch(r"[a-z_][a-z0-9_]*", path):
        return path
    base = re.sub(r"[^A-Za-z0-9_]", "_", f"{namespace}__{path}")
    if base[0].isdigit():
        base = "_" + base
    digest = hashlib.sha1(block_id.encode("utf-8")).hexdigest()[:8]
    return f"{base}_{digest}"


def hashed_color(key: str) -> Color:
    """Deterministic, reasonably saturated colour derived from a string."""
    digest = hashlib.sha1(key.encode("utf-8")).digest()
    hue = digest[0] / 255.0
    saturation = 0.45 + 0.35 * digest[1] / 255.0
    value = 0.55 + 0.35 * digest[2] / 255.0
    return colorsys.hsv_to_rgb(hue, saturation, value)


def _builtin_color(namespace: str, path: str) -> Color | None:
    if namespace != "minecraft":
        return None
    if path in _COLORS:
        return _hex(_COLORS[path])
    for dye in sorted(_DYE, key=len, reverse=True):
        if path.startswith(dye + "_"):
            return _hex(_DYE[dye])
    for wood in sorted(_WOOD, key=len, reverse=True):
        if path.startswith(wood + "_"):
            if path.endswith("_leaves"):
                return _hex("4a7a24")
            return _hex(_WOOD[wood])
    for suffix, color in (("_ore", "7f7f7f"), ("_leaves", "4a7a24"), ("_bricks", "7a7a7a"),
                          ("_copper", "c06c50"), ("_sapling", "4a7a24"), ("_flower", "c4271b")):
        if path.endswith(suffix):
            return _hex(color)
    return None


def _builtin_opaque(namespace: str, path: str) -> bool:
    if namespace != "minecraft" or path in _NOT_OPAQUE:
        return False
    if path in _OPAQUE:
        return True
    if path.startswith(("waxed_", "infested_")):
        return _builtin_opaque(namespace, path.split("_", 1)[1])
    return path.endswith(_OPAQUE_SUFFIXES)


class BlockCatalogue:
    """Resolves block IDs to :class:`BlockInfo`, applying optional overrides.

    The override file is JSON mapping a namespaced block ID to any of
    ``{"color": "#rrggbb", "opaque": true|false}``.
    """

    def __init__(self, overrides: dict[str, dict] | None = None):
        self._overrides = {k: dict(v) for k, v in (overrides or {}).items()}
        for block_id, entry in self._overrides.items():
            unknown = set(entry) - {"color", "opaque"}
            if unknown:
                raise ValueError(f"override for {block_id}: unknown keys {sorted(unknown)}")
            if "color" in entry:
                _hex(entry["color"])
            if "opaque" in entry and not isinstance(entry["opaque"], bool):
                raise ValueError(f"override for {block_id}: 'opaque' must be true or false")
        self._cache: dict[str, BlockInfo] = {}
        self._names: dict[str, str] = {}

    @classmethod
    def from_file(cls, path: str | Path | None) -> "BlockCatalogue":
        if path is None:
            return cls()
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{path}: block override file must be a JSON object")
        return cls(data)

    def info(self, block_id: str) -> BlockInfo:
        cached = self._cache.get(block_id)
        if cached is not None:
            return cached
        namespace, path = split_id(block_id)
        override = self._overrides.get(block_id, {})
        if "color" in override:
            color = _hex(override["color"])
        else:
            color = _builtin_color(namespace, path) or hashed_color(block_id)
        if block_id in AIR_IDS:
            opaque = False
        elif "opaque" in override:
            opaque = override["opaque"]
        else:
            opaque = _builtin_opaque(namespace, path)
        name = prim_name(block_id)
        other = self._names.setdefault(name, block_id)
        if other != block_id:  # pragma: no cover - guarded by prim_name's construction
            raise RuntimeError(f"prim name collision: {block_id} and {other} -> {name}")
        result = BlockInfo(block_id, name, color, opaque)
        self._cache[block_id] = result
        return result

    def is_air(self, block_id: str) -> bool:
        return block_id in AIR_IDS


class BlockIdTable:
    """Layer-wide table of block IDs; index 0 is always ``minecraft:air``.

    The index is what ``primvars:mcusd:blockId`` stores (M5), so it means the same
    block type in every region.
    """

    def __init__(self):
        self.ids: list[str] = ["minecraft:air"]
        self._index: dict[str, int] = {"minecraft:air": 0}

    def index(self, block_id: str) -> int:
        found = self._index.get(block_id)
        if found is None:
            found = self._index[block_id] = len(self.ids)
            self.ids.append(block_id)
        return found

    def __len__(self) -> int:
        return len(self.ids)

    def __getitem__(self, index: int) -> str:
        return self.ids[index]
