# Concept

> Status: planning — not yet implemented. Facts about other projects below were checked on 2026-10-17 and are linked to the source they came from.

## Problem

A Minecraft Java Edition world is a large, sparse, voxel dataset stored in a format (Anvil region files containing NBT) that only Minecraft and a handful of specialist tools understand. Getting a region of a world into a general-purpose 3D pipeline — to render it, inspect it, annotate it, or combine it with other data — usually means exporting a flat mesh (for example Wavefront OBJ). Flat meshes have two problems for this kind of data:

1. **Size.** A modest 512 × 512 area of terrain can hold millions of visible blocks. Writing every face of every block as unique geometry produces files that are slow to write, slow to load and wasteful, because almost every block is an identical unit cube.
2. **No separation of concerns.** Once terrain and annotations (for example, which faction owns which chunk on a server) are baked into one mesh, they cannot be toggled, swapped or regenerated independently.

[OpenUSD](https://openusd.org) addresses both directly:

- `UsdGeomPointInstancer` stores one prototype per block type and an array of positions plus prototype indices, so a million stone blocks cost one cube mesh plus a million `point3f` entries.
- **Layer composition** lets independent data sit in separate files (layers) that are composed into one stage at load time. Terrain can be written once; overlays such as territory claims can be regenerated nightly as a small layer and composed on top, and muted or swapped in a viewer without touching the terrain.

## Who it is for

- **Minecraft server operators** who want a renderable, inspectable 3D snapshot of their world, with server data (such as Medieval Factions territory) layered on top.
- **Technical artists and hobbyists** who want Minecraft terrain in Blender, `usdview` or any other USD-capable tool through a scriptable, headless command line rather than a GUI.
- **People learning OpenUSD** who want a realistic, non-trivial, reproducible dataset for exploring instancing and layer composition.

## What makes it interesting

- **Instancing as the storage model.** Each block type becomes a prototype; each placed block is just an index and a position. The output scales with the number of *visible* blocks, not with face count.
- **Layer composition as a headline feature, not an afterthought.** The output is a small root layer that sublayers a base terrain layer plus optional overlay layers. The first overlay is a Medieval Factions territory layer, driven by a documented JSON input contract (see [ARCHITECTURE.md](ARCHITECTURE.md#claims-overlay-input-contract)) whose claim records match the shape Medieval Factions already produces — no new storage format is invented.
- **Culling before export.** Blocks completely enclosed by opaque neighbours are dropped before they reach USD, which typically removes the bulk of underground solid blocks.
- **Headless and scriptable.** A plain Python CLI that runs on Linux, macOS and Windows, suitable for scheduled tasks and CI pipelines.
- **Tested against modern worlds.** Java Edition 26.1 moved the overworld region folder from the world root to `dimensions/minecraft/overworld/region/` (and `DIM-1`/`DIM1` to `dimensions/minecraft/the_nether` and `dimensions/minecraft/the_end`), per the [Java Edition level format](https://minecraft.wiki/w/Java_Edition_level_format) article. The reader resolves both layouts.

## Non-goals

- **Textures and block models.** The MVP renders every block as a coloured unit cube. It does not ship, extract or redistribute Mojang textures or block models, and stairs, slabs, plants and similar blocks are approximated as cubes.
- **Entities, block entities and inventories** (mobs, item frames, chest contents, signs).
- **Bedrock Edition** worlds (LevelDB-based storage).
- **Writing back** to Minecraft: the tool is read-only and never modifies a world save.
- **A live server plugin.** The exporter runs offline against a copy of a world folder; it is not a Bukkit/Spigot plugin.
- **Lighting, biome tinting and sky simulation.**
- **Defining Medieval Factions' storage format.** The exporter consumes what Medieval Factions already writes or serves; it does not propose changes to it.

## Prior art

| Project | What it is (verified) | How this project differs |
|---|---|---|
| [Mineways](https://github.com/erich666/Mineways) | "An open-source GUI program that exports Minecraft maps into 3D models for use in 3D software, printing and rendering" ([README](https://github.com/erich666/Mineways/blob/98404f8de9730b5f1b12446146bb56285fbddcc2/README.md)). The Windows build is C++; the README says there is no Linux build but it runs under WINE. Its documentation describes a **Pixar USD (USDA) export**, including `*_BlockLibrary.usda` / `*_MaterialLibrary.usda` side files, UsdPreviewSurface and optional MDL materials, and its changelog mentions a "USD PointInstancer mode" ([docs/mineways.html](https://github.com/erich666/Mineways/blob/98404f8de9730b5f1b12446146bb56285fbddcc2/docs/mineways.html)). | Mineways is the most complete Minecraft-to-USD exporter found and is far ahead on materials and block models. This project does not try to compete there. It differs in being a headless, cross-platform Python library + CLI built on the official `usd-core` Python bindings, and in making **layer composition with external data overlays** (territory claims) the central design idea. |
| [jMc2Obj](https://github.com/jmc2obj/j-mc-2-obj) | "A Java based converter of Minecraft maps to the OBJ format" ([README](https://github.com/jmc2obj/j-mc-2-obj)); GPL-2.0 licensed per the GitHub repository metadata. | Exports OBJ meshes rather than USD, so there is no instancing or layer composition in the output format. |
| [Bluemap_MedievalFactions](https://github.com/Dans-Plugins/Bluemap_MedievalFactions) | A Minecraft plugin that renders Medieval Factions land claims "as coloured shape overlays on your BlueMap web map", merging claimed chunks into contiguous polygons per faction ([README](https://github.com/Dans-Plugins/Bluemap_MedievalFactions/blob/7dd6435ff13ab92779b5c8f059aa3cdddf09a451/README.md)). | Same idea — claims as a visual overlay — but live inside a running server and a web map. This project produces an offline, portable 3D file where the overlay is a separate USD layer. |
| [Amulet](https://www.amuletmc.com) | A Minecraft world editor with a Python core (`amulet-core` on PyPI, actively released in 2026). | Not an exporter, and its licence is not open source (see [library choice](#world-reading-library-choice)). |

## World-reading library choice

The brief asked whether `anvil-parser` or Amulet should be used to read worlds. Findings (2026-10-17):

| Library | Status | Licence | Modern format support | Verdict |
|---|---|---|---|---|
| [`anvil-parser`](https://pypi.org/project/anvil-parser/) ([matcool/anvil-parser](https://github.com/matcool/anvil-parser)) | Last PyPI release 0.9.0 on 2021-01-02; GitHub repository is **archived**. | MIT | Predates the 1.18 chunk format. | Not used. |
| [`anvil-parser2`](https://pypi.org/project/anvil-parser2/) ([0xTiger/anvil-parser2](https://github.com/0xTiger/anvil-parser2)) | Fork of the above "in order to additionally support minecraft versions 1.18 and above" ([README](https://github.com/0xTiger/anvil-parser2/blob/91a7eda55f0fb55a948d1072896430c29d906809/README.md)). Latest release 0.10.6 (2024-07-21); last commit 2025-10-07. | MIT | Reads post-21w43a chunks (top-level `sections` / `block_states` palettes) — [`anvil/chunk.py`](https://github.com/0xTiger/anvil-parser2/blob/91a7eda55f0fb55a948d1072896430c29d906809/anvil/chunk.py#L79-L94). Gaps: [`anvil/region.py`](https://github.com/0xTiger/anvil-parser2/blob/91a7eda55f0fb55a948d1072896430c29d906809/anvil/region.py#L71-L81) only decompresses zlib (raises on gzip; no uncompressed, LZ4 or external `.mcc` handling), and section Y range is hard-coded to −4..19 ([`chunk.py` L50-L54](https://github.com/0xTiger/anvil-parser2/blob/91a7eda55f0fb55a948d1072896430c29d906809/anvil/chunk.py#L50-L54)). No explicit testing against 1.21.x or 26.x is documented. | **Chosen for chunk/palette decoding**, behind an internal interface, with the region-file container handled by this project. |
| [Amulet](https://www.amuletmc.com) (`amulet-core` 1.9.50 on PyPI, 2026-10-08) | Very actively maintained. | The `LICENSE` file reads "All rights reserved. A licence must be purchased to use this software." ([Amulet-Core `1.0` branch](https://github.com/Amulet-Team/Amulet-Core/blob/0b3825e02352660672364206ab297b9eadf17b9b/LICENSE)). | Broad. | **Not used**: incompatible with an MIT-licensed open-source dependency tree. |

The decision and the region-container work it implies are detailed in [ARCHITECTURE.md](ARCHITECTURE.md#world-reader).
