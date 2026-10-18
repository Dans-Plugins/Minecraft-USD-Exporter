"""CLI.

Parses arguments, wires the pipeline, reports progress and a summary.

Milestone M1 provides only ``--help`` and ``--version``; the export pipeline is
wired in milestone M7.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from mcusd import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mc-usd-export",
        description=(
            "Export a bounding box of a Minecraft Java Edition world as a "
            "layered OpenUSD stage."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
