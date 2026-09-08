#!/usr/bin/env python3
"""Generate the PWA / home-screen icons for Agent Capital.

Run from the repo root:  ./venv/bin/python scripts/make-icons.py

Outputs (all PNG, drawn with Pillow only — no fonts, no emoji):
  frontend/public/icons/icon-192.png            standard 192x192
  frontend/public/icons/icon-512.png            standard 512x512
  frontend/public/icons/icon-512-maskable.png   512x512, full-bleed bg, mark inside the 80% safe zone
  frontend/public/apple-touch-icon.png          180x180, full-bleed (iOS applies its own corner mask)
  frontend/public/favicon.png                   64x64 tab icon

The mark is a simple geometric capitol: a dome with a lantern on top, an
entablature, five columns and a stepped base, in the app's accent purple on
the app's dark surface colour (see frontend/src/App.css :root).
"""
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / "frontend" / "public"

BG = (22, 22, 29, 255)        # --surface  #16161d
ACCENT = (124, 92, 252, 255)  # --accent   #7c5cfc
ACCENT2 = (167, 139, 250, 255)  # --accent2 #a78bfa

SS = 4  # supersampling factor for smooth edges


def draw_mark(draw, cx, cy, size):
    """Draw the capitol mark centred on (cx, cy), fitting in a size x size box.
    All coordinates are in the (supersampled) pixel space of `draw`."""
    s = size
    left = cx - s / 2
    top = cy - s / 2

    def X(f):
        return left + f * s

    def Y(f):
        return top + f * s

    # Stepped base (two slabs)
    draw.rounded_rectangle([X(0.06), Y(0.86), X(0.94), Y(0.94)], radius=s * 0.015, fill=ACCENT)
    draw.rounded_rectangle([X(0.12), Y(0.78), X(0.88), Y(0.855)], radius=s * 0.012, fill=ACCENT)

    # Columns (five)
    col_w = 0.075
    gap = (0.76 - 5 * col_w) / 4
    x = 0.12
    for _ in range(5):
        draw.rounded_rectangle([X(x), Y(0.50), X(x + col_w), Y(0.775)], radius=s * 0.008, fill=ACCENT)
        x += col_w + gap

    # Entablature
    draw.rounded_rectangle([X(0.09), Y(0.43), X(0.91), Y(0.495)], radius=s * 0.012, fill=ACCENT)

    # Drum below the dome
    draw.rectangle([X(0.30), Y(0.36), X(0.70), Y(0.43)], fill=ACCENT2)

    # Dome (upper half of an ellipse)
    draw.pieslice([X(0.24), Y(0.14), X(0.76), Y(0.58)], start=180, end=360, fill=ACCENT2)

    # Lantern + finial on top
    draw.rounded_rectangle([X(0.455), Y(0.09), X(0.545), Y(0.17)], radius=s * 0.01, fill=ACCENT2)
    draw.ellipse([X(0.475), Y(0.035), X(0.525), Y(0.085)], fill=ACCENT2)


def render(px, *, rounded, mark_fraction):
    """Render a px x px icon. rounded=True draws the dark square with rounded
    corners on a transparent canvas; rounded=False fills the whole canvas."""
    big = px * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if rounded:
        d.rounded_rectangle([0, 0, big - 1, big - 1], radius=big * 0.22, fill=BG)
    else:
        d.rectangle([0, 0, big, big], fill=BG)
    draw_mark(d, big / 2, big / 2, big * mark_fraction)
    return img.resize((px, px), Image.LANCZOS)


def main():
    icons = PUBLIC / "icons"
    icons.mkdir(parents=True, exist_ok=True)
    outputs = [
        (icons / "icon-192.png", 192, True, 0.72),
        (icons / "icon-512.png", 512, True, 0.72),
        # Maskable: the OS may crop to a circle covering the central 80%, so keep
        # the mark well inside that (0.58 of the full width) and fill the bg edge-to-edge.
        (icons / "icon-512-maskable.png", 512, False, 0.58),
        (PUBLIC / "apple-touch-icon.png", 180, False, 0.72),
        (PUBLIC / "favicon.png", 64, True, 0.78),
    ]
    for path, px, rounded, frac in outputs:
        render(px, rounded=rounded, mark_fraction=frac).save(path, optimize=True)
        print(f"wrote {path.relative_to(ROOT)} ({px}x{px})")


if __name__ == "__main__":
    main()
