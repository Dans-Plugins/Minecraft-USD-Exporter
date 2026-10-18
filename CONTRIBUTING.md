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
virtualenv gives a working `mc-usd-export --help`.

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
- The tool is read-only: nothing may write into a world folder.

## Pull requests

- Branch from `main` (`feature/<short-description>`), keep each PR to one milestone
  or fix, and reference the issue it closes.
- Make sure `python -m pytest` passes locally and CI is green.
