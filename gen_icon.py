"""Generate a multi-size Windows .ico from icon.svg.

Produces icon.ico (16/32/48/64/128/256 px) for PyInstaller and Inno Setup.
The SVG is rendered with Pillow (no cairo required). Run: python gen_icon.py
"""

import os
import math
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
SVG = os.path.join(HERE, "icon.svg")
ICO = os.path.join(HERE, "icon.ico")
PNG = os.path.join(HERE, "icon_512.png")
SIZES = [16, 32, 48, 64, 128, 256]
N = 512

CYAN0 = (0, 242, 254)
CYAN1 = (79, 172, 254)
GREEN0 = (0, 255, 135)
GREEN1 = (96, 239, 255)


def lerp(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def bbox_gradient(bbox, c0, c1, vertical=True):
    x0, y0, x1, y1 = bbox
    w = max(1, x1 - x0)
    h = max(1, y1 - y0)
    img = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    px = img.load()
    for y in range(N):
        for x in range(N):
            if x < x0 or x > x1 or y < y0 or y > y1:
                continue
            t = (y - y0) / h if vertical else (((x - x0) / w) + ((y - y0) / h)) / 2.0
            px[x, y] = lerp(c0, c1, t) + (255,)
    return img


def paint(canvas, draw_fn, bbox, c0, c1, alpha=1.0, vertical=False):
    mask = Image.new("L", (N, N), 0)
    draw_fn(ImageDraw.Draw(mask))
    grad = bbox_gradient(bbox, c0, c1, vertical=vertical)
    amask = mask.point(lambda p: int(p * alpha))
    return Image.composite(grad, canvas, amask)


def dashed_ring(cx, cy, r, width, c0, c1, alpha, dash, gap, vertical=False):
    bbox = [cx - r, cy - r, cx + r, cy + r]
    circ = 2 * math.pi * r
    step = dash + gap
    n = max(1, int(circ / step))
    seg = 360.0 / n
    frac = dash / step

    def draw(md):
        for i in range(n):
            start = i * seg
            end = start + seg * frac
            md.arc(bbox, start, end, fill=255, width=width)

    return paint(Image.new("RGBA", (N, N), (0, 0, 0, 0)),
                 draw, bbox, c0, c1, alpha, vertical)


def render_master():
    canvas = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle([0, 0, N, N], radius=110, fill="#1e222b")

    cx = cy = 256
    canvas = Image.alpha_composite(
        canvas, dashed_ring(cx, cy, 180, 12, CYAN0, CYAN1, 0.2, 20, 15))
    canvas = Image.alpha_composite(
        canvas, dashed_ring(cx, cy, 130, 8, CYAN0, CYAN1, 0.4, 10, 10))

    arms = [(-90, -40, -90, -90, -40, -90), (90, -40, 90, -90, 40, -90),
            (-90, 40, -90, 90, -40, 90), (90, 40, 90, 90, 40, 90)]
    w = 24
    for ax1, ay1, ax2, ay2, ax3, ay3 in arms:
        pts = [(cx + ax1, cy + ay1), (cx + ax2, cy + ay2), (cx + ax3, cy + ay3)]
        bbox = [min(p[0] for p in pts) - w, min(p[1] for p in pts) - w,
                max(p[0] for p in pts) + w, max(p[1] for p in pts) + w]

        def draw(md, _pts=pts):
            md.line([_pts[0], _pts[1]], fill=255, width=w, joint="curve")
            md.line([_pts[1], _pts[2]], fill=255, width=w, joint="curve")

        canvas = Image.alpha_composite(
            canvas, paint(Image.new("RGBA", (N, N), (0, 0, 0, 0)),
                          draw, bbox, GREEN0, GREEN1, 1.0, vertical=True))

    bars = [(-50, -30, 16, 60), (-20, -55, 16, 110),
            (10, -40, 16, 80), (40, -20, 16, 40)]
    for bx, by, bw, bh in bars:
        x0, y0 = cx + bx, cy + by
        x1, y1 = x0 + bw, y0 + bh
        bbox = [x0, y0, x1, y1]

        def draw(md, _b=(x0, y0, x1, y1)):
            md.rounded_rectangle(_b, radius=8, fill=255)

        canvas = Image.alpha_composite(
            canvas, paint(Image.new("RGBA", (N, N), (0, 0, 0, 0)),
                          draw, bbox, CYAN0, CYAN1, 1.0, vertical=False))

    r = 18
    rx, ry = 390, 120
    rect = [rx - r, ry - r, rx + r, ry + r]
    rec = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    rec.paste((255, 51, 102, 255), rect)
    m = Image.new("L", (N, N), 0)
    ImageDraw.Draw(m).ellipse(rect, fill=255)
    canvas = Image.composite(rec, canvas, m)
    return canvas


def main():
    try:
        import cairosvg
        import io
        buf = io.BytesIO()
        cairosvg.svg2png(url=SVG, write_to=buf, output_width=N,
                         output_height=N)
        buf.seek(0)
        base = Image.open(buf).convert("RGBA")
        print("Rendered with cairosvg")
    except Exception as exc:
        print("cairosvg unavailable ({}); using Pillow renderer.".format(exc))
        base = render_master()

    base.save(PNG)
    frames = [base.resize((s, s), Image.LANCZOS) for s in SIZES]
    frames[0].save(ICO, format="ICO", append_images=frames[1:])
    print("Wrote", ICO, "sizes", [f.size for f in frames])


if __name__ == "__main__":
    main()