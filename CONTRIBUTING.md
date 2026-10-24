# Contributing

Thanks for your interest in Minecraft-USD-Exporter. The design lives in
[CONCEPT.md](CONCEPT.md), [MVP.md](MVP.md) and [ARCHITECTURE.md](ARCHITECTURE.md);
read them before changing behaviour.

## Development setup

Python 3.12 or newer is required.

Linux / macOS:

    python3 -m venv .venv
    . .venv/bin/activate
    pip install -e ".[lz4,dev]"

Windows:

    py -m venv .venv
    .venv\Scripts\activate
    pip install -e ".[lz4,dev]"

## Running the tests

    python -m pytest

CI (GitHub Actions, `.github/workflows/ci.yml`) runs the same command on Linux
with Python 3.12 and 3.14, after checking that a plain `pip install .` in a fresh
virtualenv gives a working `mc-usd-export --help`. A second workflow,
`.github/workflows/usd-asset-gate.yml`, exports the fixture worlds and checks the
written stages with [usd-asset-gate](https://github.com/Preponderous-Software/usd-asset-gate)
against the rules in `.usd-asset-gate.yml`; it fails on any warning.

A third CI job, **Strata previews** (in `ci.yml`), renders the fixture exports
headless with `scripts/render_strata_preview.py` (Strata on Mesa's llvmpipe, no
GPU) and uploads the images as the `strata-previews` build artifact, so a change to
how an export looks can be seen in review. It fails on a renderer error or a blank
image, never on pixel differences. See the README's
[Headless previews with Strata](README.md#headless-previews-with-strata).

## Conventions

- Package layout follows the [component table](ARCHITECTURE.md#components).
- Work on dense `numpy` arrays; avoid per-block Python loops in hot paths.
- Dependencies are pinned in `pyproject.toml` as listed in
  [ARCHITECTURE.md § Dependencies](ARCHITECTURE.md#dependencies); changing a pin or
  licence is a design decision, not a routine edit.
- Every acceptance criterion in [MVP.md](MVP.md#acceptance-criteria) that is not
  marked *manual* needs an automated test.
- Tests build synthetic region files in-test. The only committed world data is the
  generated fixtures under `tests/fixtures/`, produced by the
  [fixture recipe](ARCHITECTURE.md#fixture-world-recipe). Never commit a played world
  or any player data.
- To regenerate the fixtures, run `python scripts/make_fixtures.py --accept-eula`
  (it needs Java 21 for 1.21.11 and Java 25 for 26.3, passed with
  `--java VERSION=PATH`; running it accepts the Minecraft EULA). It rewrites the
  worlds, `fixtures.json` and `RECIPE.md` together; commit all three.
- The tool is read-only: nothing may write into a world folder.

## Pull requests

- Branch from `main` (`feature/<short-description>`), keep each PR to one milestone
  or fix, and reference the issue it closes.
- Make sure `python -m pytest` passes locally and CI is green.
