"""Generate the Hex CLI logo: assets/hexcli.ico + assets/hexcli.png.

The mark is a glowing hexagon (the model runs on the Hexagon NPU) around a
terminal prompt. Drawn procedurally at 1024px and downscaled, so the repo
carries no binary design sources -- rerun this script to regenerate.

Requires Pillow. Usage: python tools/gen_icon.py
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SIZE = 1024
CX = CY = SIZE // 2

CYAN = (34, 211, 238)
VIOLET = (139, 92, 246)
BG_TOP = (13, 17, 23)
BG_BOT = (22, 30, 44)
PROMPT = (240, 250, 255)


def lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def hex_vertices(cx, cy, r):
    """Pointy-top hexagon vertices, starting from the top."""
    pts = []
    for i in range(6):
        ang = math.radians(-90 + 60 * i)
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    return pts


def draw_hex_ring(layer, r, width):
    """Hexagon outline with a cyan->violet gradient that wraps seamlessly."""
    d = ImageDraw.Draw(layer)
    verts = hex_vertices(CX, CY, r)
    segs_per_edge = 40
    total = 6 * segs_per_edge
    prev = verts[0]
    for i in range(1, total + 1):
        edge, k = divmod(i, segs_per_edge)
        a = verts[edge % 6]
        b = verts[(edge + 1) % 6] if k else verts[edge % 6]
        if k:
            t_edge = k / segs_per_edge
            cur = (a[0] + (b[0] - a[0]) * t_edge, a[1] + (b[1] - a[1]) * t_edge)
        else:
            cur = a
        t = i / total
        color = lerp(CYAN, VIOLET, (1 - math.cos(2 * math.pi * t)) / 2)
        d.line([prev, cur], fill=color + (255,), width=width)
        d.ellipse(
            [cur[0] - width / 2, cur[1] - width / 2,
             cur[0] + width / 2, cur[1] + width / 2],
            fill=color + (255,),
        )
        prev = cur


def build_logo():
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    # Rounded dark tile with a vertical gradient.
    tile = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    grad = Image.new("RGBA", (SIZE, SIZE))
    gd = ImageDraw.Draw(grad)
    for y in range(SIZE):
        gd.line([(0, y), (SIZE, y)], fill=lerp(BG_TOP, BG_BOT, y / SIZE) + (255,))
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle([28, 28, SIZE - 28, SIZE - 28], radius=210, fill=255)
    tile.paste(grad, (0, 0), mask)
    img.alpha_composite(tile)

    # Glow pass under the ring.
    ring = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw_hex_ring(ring, r=352, width=58)
    glow = ring.filter(ImageFilter.GaussianBlur(38))
    img.alpha_composite(Image.eval(glow, lambda v: v))
    img.alpha_composite(ring)

    # Prompt chevron "> _" centered in the hexagon.
    d = ImageDraw.Draw(img)
    w = 74
    chev = [(CX - 168, CY - 128), (CX - 22, CY), (CX - 168, CY + 128)]
    d.line(chev, fill=PROMPT + (255,), width=w, joint="curve")
    for p in (chev[0], chev[-1]):
        d.ellipse([p[0] - w / 2, p[1] - w / 2, p[0] + w / 2, p[1] + w / 2], fill=PROMPT + (255,))
    d.rounded_rectangle([CX + 52, CY + 128 - w, CX + 210, CY + 128], radius=w // 2, fill=CYAN + (255,))

    return img


def main():
    assets = Path(__file__).resolve().parent.parent / "assets"
    assets.mkdir(exist_ok=True)
    logo = build_logo()
    logo.resize((256, 256), Image.LANCZOS).save(assets / "hexcli.png")
    logo.resize((256, 256), Image.LANCZOS).save(
        assets / "hexcli.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f"wrote {assets / 'hexcli.ico'} and hexcli.png")


if __name__ == "__main__":
    main()
