"""Put two preview renders side by side with captions and a faction legend.

Pairs with ``scripts/render_preview.py``: render ``terrain.usdc`` and ``world.usda``
of one export, then::

    python scripts/compose_preview.py TERRAIN.png WORLD.png CLAIMS.json OUT.png \\
        --footer "Minecraft 26.3, seed 8675309, ..."

The legend lists every faction in ``CLAIMS.json`` (an ``mcusd-claims`` file) with
its colour. Needs Pillow, which is not a project dependency
(``uv run --with pillow python scripts/compose_preview.py ...``).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
INK = (34, 38, 46)
MUTED = (92, 99, 112)
PAPER = (246, 247, 249)
PANEL = (226, 233, 241)  # behind the renders, which have a transparent background


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default(size)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("terrain")
    parser.add_argument("world")
    parser.add_argument("claims")
    parser.add_argument("out")
    parser.add_argument("--footer", default="")
    args = parser.parse_args()

    left, right = Image.open(args.terrain).convert("RGBA"), Image.open(args.world).convert("RGBA")
    w, h = left.size
    gap, pad, head, foot = 16, 24, 64, 44
    canvas = Image.new("RGB", (pad * 2 + w * 2 + gap, head + h + foot), PAPER)
    for x, image in ((pad, left), (pad + w + gap, right)):
        panel = Image.new("RGBA", image.size, PANEL + (255,))
        canvas.paste(Image.alpha_composite(panel, image).convert("RGB"), (x, head))
    draw = ImageDraw.Draw(canvas)

    title, sub = font(FONT_BOLD, 22), font(FONT, 16)
    for x, label, detail in ((pad, "Base layer", "terrain.usdc: instanced, culled blocks"),
                             (pad + w + gap, "Composed stage",
                              "world.usda = overlays/claims.usda over terrain.usdc")):
        draw.text((x + 2, 10), label, font=title, fill=INK)
        draw.text((x + 2, 38), detail, font=sub, fill=MUTED)

    factions = json.loads(Path(args.claims).read_text(encoding="utf-8"))["factions"]
    legend_font = font(FONT, 17)
    row = 28
    heading, heading_font = "Factions (example data)", font(FONT_BOLD, 14)
    box_w = 24 + max([38 + int(draw.textlength(f["name"], font=legend_font)) for f in factions]
                     + [int(draw.textlength(heading, font=heading_font))])
    box_h = 12 + row * (len(factions) + 1)
    bx, by = pad + w + gap + 14, head + 14
    draw.rounded_rectangle((bx, by, bx + box_w, by + box_h), radius=8, fill=(255, 255, 255),
                           outline=(210, 214, 222))
    draw.text((bx + 12, by + 8), heading, font=heading_font, fill=MUTED)
    for i, faction in enumerate(factions):
        y = by + 8 + row * (i + 1)
        color = faction["color"].lstrip("#")
        rgb = tuple(int(color[k:k + 2], 16) for k in (0, 2, 4))
        draw.rounded_rectangle((bx + 12, y + 2, bx + 30, y + 20), radius=3, fill=rgb)
        draw.text((bx + 38, y), faction["name"], font=legend_font, fill=INK)

    draw.text((pad + 2, head + h + 12), args.footer, font=font(FONT, 15), fill=MUTED)
    canvas.save(args.out, optimize=True)
    print(f"wrote {args.out} ({canvas.size[0]}x{canvas.size[1]}, {Path(args.out).stat().st_size} bytes)")


if __name__ == "__main__":
    main()
