# Minecraft-USD-Exporter

> **Status: planning — not yet implemented.** This repository currently contains planning documents only. Nothing below can be installed or run yet; the commands show the intended interface.

## Description

Minecraft-USD-Exporter is a Python tool that reads Minecraft Java Edition world saves (Anvil region files) and exports a selected area as an [OpenUSD](https://openusd.org) stage.

- Every block type becomes a prototype under a `UsdGeomPointInstancer`, so millions of blocks are stored as compact index and position arrays instead of unique geometry.
- Hidden blocks — those fully enclosed by opaque neighbours — are culled before export.
- Output uses **USD layer composition**: a base terrain layer plus optional overlay layers. The first overlay shows [Medieval Factions](https://github.com/Dans-Plugins/Medieval-Factions) territory claims, read from Medieval Factions' own claim data through a documented JSON contract.
- Results open in `usdview`, Blender, or any USD-capable tool.

Planning documents:

- [CONCEPT.md](CONCEPT.md) — problem, audience, non-goals, prior art, and the world-reading library decision.
- [MVP.md](MVP.md) — the smallest end-to-end slice, acceptance criteria and milestones.
- [ARCHITECTURE.md](ARCHITECTURE.md) — components, data flow, USD stage layout, the claims input contract, dependencies, testing and performance.

## Installation

*Planned — not yet available.*

### First Time Installation

1. Install Python 3.12 or newer.
2. Clone this repository: `git clone https://github.com/Dans-Plugins/Minecraft-USD-Exporter.git`
3. Install it into a virtual environment: `pip install .` (add `.[lz4]` to read LZ4-compressed worlds).

### Viewers

- `usdview` comes from a full OpenUSD build: the [`usd-core`](https://pypi.org/project/usd-core/) wheel "does not provide any of the optional plugins or imaging features from the full USD distribution".
- [Blender](https://www.blender.org) can import the output with its built-in USD importer.

## Usage

*Planned interface:*

```bash
mc-usd-export /path/to/world \
  --dimension overworld \
  --bbox -256 -64 -256 255 319 255 \
  --claims claims.json --world-uuid <world-uuid> \
  --out out/
usdview out/world.usda
```

The root and overlay layers are written as ASCII `.usda` by default; add `--binary` to write them as `.usdc`. The terrain layer is always binary (`terrain.usdc`) because it is large.

Always export from a **copy** of a world folder or from a stopped server; the tool only reads, but reading files a running server is writing can give inconsistent results.

### Documentation

- [MVP scope and acceptance criteria](MVP.md)
- [Claims overlay input contract](ARCHITECTURE.md#claims-overlay-input-contract)
- [Output stage layout](ARCHITECTURE.md#output-layout)

A User Guide and a full CLI reference will be added with the first release.

## Support

You can find the support Discord server [here](https://discord.gg/xXtuAQ2).

### Experiencing a bug?

Please fill out a bug report [here](https://github.com/Dans-Plugins/Minecraft-USD-Exporter/issues).

- [Known Bugs](https://github.com/Dans-Plugins/Minecraft-USD-Exporter/issues?q=is%3Aissue+is%3Aopen+label%3Abug)

## Contributing

- The MVP is tracked as issues labelled [`mvp`](https://github.com/Dans-Plugins/Minecraft-USD-Exporter/issues?q=is%3Aissue+label%3Amvp); each maps to a milestone in [MVP.md](MVP.md#milestones).
- A `CONTRIBUTING.md` will be added with the project skeleton (milestone M1).

## Testing

*Planned.* The test suite will use `pytest`.

Linux:

    python -m pytest

Windows:

    py -m pytest

If pytest reports all tests passed, the tests have passed.

## Development

This is a command-line Python tool rather than a server plugin, so there is no Docker test server. Development will use a virtual environment:

Linux:

    python3 -m venv .venv
    . .venv/bin/activate
    pip install -e ".[lz4,dev]"

Windows:

    py -m venv .venv
    .venv\Scripts\activate
    pip install -e ".[lz4,dev]"

## Authors and Acknowledgement

### Developers

| Name | Main Contributions |
|------|--------------------|
| Daniel McCoy Stephenson | Creator |

Planning documents were drafted with Claude (Anthropic).

## License

This project is licensed under the [MIT License](LICENSE).

See the [LICENSE](LICENSE) file for full details.

## Project Status

Planning — not yet implemented. See [MVP.md](MVP.md) for the roadmap.
