#!/usr/bin/env bash
# Regenerate the README's turntable thumbnail and fixture still
# (docs/images/fixture-turntable.webp, fixture-turntable-poster.png and
# fixture-claims-legend.png) from the committed 26.3 fixture world and the
# bundled example claims, with the repository's own commands.
#
# Needs the preview extras installed (see "Headless previews with Strata" in the
# README): usd-web, Strata's Python binding, Pillow and a Mesa EGL driver.
#
#     scripts/make_readme_turntable.sh            # writes into docs/images/
#     scripts/make_readme_turntable.sh OUT_DIR    # writes somewhere else
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
out="${1:-$repo/docs/images}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

export LIBGL_ALWAYS_SOFTWARE="${LIBGL_ALWAYS_SOFTWARE:-1}" GALLIUM_DRIVER="${GALLIUM_DRIVER:-llvmpipe}"

mc-usd-export "$repo/tests/fixtures/world-26.3" --bbox -16 -64 -16 15 319 15 \
  --claims "$repo/examples/claims.json" --out "$work/usd" --quiet

# The turntable: 24 frames at twice the default size, otherwise the defaults.
mc-usd-thumbnail "$work/usd/world.usda" "$work/thumb" --frames 24 --size 400 264 --ms 160 > /dev/null
# The still: mc-usd-preview's defaults, legend headed by the claims' recorded source.
mc-usd-preview "$work/usd/world.usda" "$work/still.png" > /dev/null

mkdir -p "$out"
cp "$work/thumb/thumb.webp" "$out/fixture-turntable.webp"
cp "$work/thumb/poster.png" "$out/fixture-turntable-poster.png"
cp "$work/still.png" "$out/fixture-claims-legend.png"
ls -l "$out"/fixture-turntable.webp "$out"/fixture-turntable-poster.png "$out"/fixture-claims-legend.png
