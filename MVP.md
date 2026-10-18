# MVP

> Status: in development — milestones M1–M5 are complete; M6–M7 are not yet implemented. Design details live in [ARCHITECTURE.md](ARCHITECTURE.md); motivation and prior art in [CONCEPT.md](CONCEPT.md).

## The smallest end-to-end slice

One command turns a bounding box of a Minecraft Java Edition world into a composed OpenUSD stage that opens in `usdview` and Blender:

```bash
mc-usd-export /path/to/world \
  --dimension overworld \
  --bbox -256 -64 -256 255 319 255 \
  --claims claims.json --world-uuid 9c2e... \
  --out out/
```

producing `out/world.usda` (root) → sublayers `out/overlays/claims.usda` (Medieval Factions territory) over `out/terrain.usdc` (instanced, culled blocks). Root and overlay layers are ASCII `.usda` by default and `.usdc` with `--binary`; terrain is always `.usdc` ([output layout](ARCHITECTURE.md#output-layout)).

## In scope

- Reading Java Edition worlds saved by **1.21.x and 26.x** (pre-26.1 and 26.1+ folder layouts), dimensions `overworld`, `the_nether`, `the_end`.
- All region-file compression schemes defined by the format except custom (127): gzip, zlib, uncompressed, LZ4 (optional extra), and external `.mcc` chunks.
- Inclusive block-space bounding-box selection.
- Whole-block culling of air and fully enclosed opaque blocks.
- Base terrain layer: one coloured unit-cube prototype per block ID, one `UsdGeomPointInstancer` per region, with a per-instance block ID (`primvars:mcusd:blockId`) and per-instance `ids` so later overlays can cut away or recolour terrain ([stage layout](ARCHITECTURE.md#stage--prim-layout)).
- Output format: `.usda` by default, `--binary` for `.usdc`; the terrain layer is always `.usdc`.
- Claims overlay layer from the documented [claims input contract](ARCHITECTURE.md#claims-overlay-input-contract), including direct ingestion of Medieval Factions' `GET /api/claims` output and JSON-storage files.
- Root layer composing overlays over terrain, with provenance in `customLayerData`.
- A CLI, a README usage section and a manual viewer checklist.

## Out of scope (MVP)

- Textures, materials beyond `displayColor`, and non-cube block models (stairs, slabs, fences, plants are cubes).
- Per-face culling and greedy meshing.
- Block states (e.g. `facing`, `waterlogged`) — prototypes are keyed by block ID only.
- Entities, block entities, biomes, lighting.
- Payloads / load-on-demand, LOD, multi-process export.
- Reading Medieval Factions' SQL database directly, or auto-detecting world UUID.
- Bedrock Edition; datapack dimensions with non-default heights; custom (127) chunk compression.
- Publishing to PyPI.

## Acceptance criteria

Each is checked by an automated test unless marked *manual*.

1. **AC1 — Installs cleanly.** `pip install .` on Python 3.12 in a fresh virtualenv installs `usd-core`, `anvil-parser2` and `numpy`, and `mc-usd-export --help` exits 0.
2. **AC2 — Both layouts.** Given a pre-26.1 fixture world and a 26.1+ fixture world, the locator returns the correct region directory for each of the three dimensions, and errors clearly (non-zero exit, message naming the missing path) for a folder that is not a world.
3. **AC3 — Every compression scheme.** Synthetic region files using schemes 1, 2, 3, 4 and an external `.mcc` chunk all decode to identical NBT; scheme 127 raises an error that names the region file and chunk coordinates.
4. **AC4 — Selection is exact.** For a fixture world, exporting a bbox yields only instances whose world positions lie inside the inclusive box, including boxes spanning negative coordinates and region boundaries.
5. **AC5 — Culling is safe and effective.** On a synthetic 16³ solid stone volume, exactly the 1,352 shell blocks survive (16³ − 14³); a 1-block air pocket inside exposes its 6 neighbours; no block adjacent to air, water or an unknown block is ever culled.
6. **AC6 — Valid instancing.** For every `PointInstancer` in `terrain.usdc`: `len(protoIndices) == len(positions) == len(ids) == len(primvars:mcusd:blockId)`, every index is a valid prototype, every `blockId` resolves through `/World/BlockLibrary.mcusd:blockIds` to the same namespaced block ID as the instance's prototype, `ids` are unique within the instancer, `extent` encloses all instances, and the total instance count equals the culled block count reported by the CLI.
7. **AC7 — Stage metadata and format.** `world.usda` has `defaultPrim = "World"`, `upAxis = "Y"`, `metersPerUnit = 1`, sublayers in the order `[overlays/claims.usda, terrain.usdc]`, and `customLayerData` recording source world, dimension, bbox and exporter version. With `--binary`, the root and overlay are `world.usdc` and `overlays/claims.usdc` (sublayer paths match), and the terrain layer is `terrain.usdc` in both modes.
8. **AC8 — Claims contract.** The native contract, a saved `GET /api/claims` array, and an MF JSON-storage directory (`claimed_chunks.json` + `factions.json`) each produce the same overlay for the same data; a claim at chunk (x, z) yields a box whose minimum corner is at block (16x, ymin, 16z); faction colours come from `flags.color` / `color` when present.
9. **AC9 — Composition works.** Opening `world.usda` with `Usd.Stage.Open` shows `/World/Overlays/Claims`; after `stage.MuteLayer` on the overlay layer it is gone and `/World/Terrain` is unchanged.
10. **AC10 — Real worlds.** End-to-end export of committed 1.21.x and 26.x fixture worlds succeeds in CI and matches recorded block counts at known coordinates. The fixtures are tiny fresh worlds generated by throwaway vanilla servers at fixed seeds and exactly pinned versions, contain only the chunks named in the [fixture recipe](ARCHITECTURE.md#fixture-world-recipe), and contain no player data.
11. **AC11 — Viewers (*manual*).** `usdview out/world.usda` shows terrain and claim boxes; muting the overlay hides the boxes; Blender's USD importer loads the stage with terrain instanced. Results recorded in the README with tool versions.

## Milestones

Each milestone is one GitHub issue labelled `mvp`.

<a id="m1"></a>
### M1 — Project skeleton, packaging and CI

*Status: done.*


`pyproject.toml` (Python ≥ 3.12, pinned dependencies from [ARCHITECTURE.md](ARCHITECTURE.md#dependencies), `[lz4]` and `[dev]` extras, `mc-usd-export` console script), `mcusd/` package skeleton matching the [component table](ARCHITECTURE.md#components), `pytest` wiring, GitHub Actions on Python 3.12 and 3.14. Covers **AC1**.

<a id="m2"></a>
### M2 — World locator and region-file reader

*Status: done.* AC2's CLI exit-code half is tested with the CLI in M7.


Resolve region directories for pre-26.1 and 26.1+ layouts; implement the `.mca` header, all compression schemes and external `.mcc` chunks; hand decompressed NBT to `anvil-parser2`. Covers **AC2**, **AC3**.

<a id="m3"></a>
### M3 — Chunk decoding, bounding-box selection and block catalogue

*Status: done.* AC4 is tested on synthetic worlds in both palette encodings; the committed fixture worlds are added to it in M7.


Decode sections into `numpy` palette-index arrays, map bbox → regions/chunks and clip, and build the block catalogue (prim-safe names, colours, opaque-full-cube flags, override file). Covers **AC4**.

<a id="m4"></a>
### M4 — Hidden-block culling

*Status: done.*


Vectorised six-neighbour culling with a one-block halo across chunk and region boundaries; air always dropped; unknown blocks non-occluding. Covers **AC5**.

<a id="m5"></a>
### M5 — Base terrain USD layer

*Status: done.* The inherited-prototype viewer check moves to the manual checklist (AC11), and the per-instance-array size measurement is recorded with the M7 fixture measurements.


Write `terrain.usdc` (always crate format): abstract `/World/BlockLibrary` with a layer-wide block-ID table, one region-local `PointInstancer` per region with inherited prototypes, per-instance `primvars:mcusd:blockId` and `ids`, extents, kinds. Covers **AC6**.

<a id="m6"></a>
### M6 — Claims input contract and overlay layer

Implement the claims contract and the three input adapters, then write `overlays/claims.usda` with one translucent-box `PointInstancer` per faction. Covers **AC8**.

<a id="m7"></a>
### M7 — CLI, root stage assembly and end-to-end verification

Wire the CLI (including `--binary`), write `world.usda` with sublayers and metadata, write the fixture recipe (`scripts/make_fixtures.py` + `tests/fixtures/RECIPE.md`) and commit the 1.21.x and 26.x fixture worlds it generates, run end-to-end tests, perform the manual viewer checklist and document usage and measured performance in the README. Covers **AC7**, **AC9**, **AC10**, **AC11**.
