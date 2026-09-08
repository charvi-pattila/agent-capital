"""Generate desktop/build/icon.png (1024x1024) for the Agent Capital app.

Rounded dark square, accent-coloured capitol dome over three columns.
electron-builder converts the PNG to .icns at build time.
Run: ./venv/bin/python desktop/build/make-icon.py  (from the repo root)
"""
from pathlib import Path
from PIL import Image, ImageDraw

S = 1024
BG = (13, 13, 17, 255)          # --bg #0d0d11
ACCENT = (124, 92, 252, 255)    # --accent #7c5cfc
ACCENT2 = (168, 148, 255, 255)
BORDER = (42, 42, 53, 255)

# Draw at 4x and downsample for smooth edges.
SS = 4
W = S * SS
img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# macOS-style rounded square (~22% corner radius), slightly inset from the canvas.
inset = int(W * 0.06)
d.rounded_rectangle([inset, inset, W - inset, W - inset], radius=int(W * 0.20), fill=BG, outline=BORDER, width=SS * 3)

cx = W / 2
# --- Base / steps ---
base_y = int(W * 0.80)
step_h = int(W * 0.035)
for i, half in enumerate([0.32, 0.29]):
    y0 = base_y - i * step_h
    d.rectangle([cx - W * half, y0 - step_h, cx + W * half, y0], fill=ACCENT if i == 1 else ACCENT2)

# --- Columns ---
col_top = int(W * 0.50)
col_bottom = base_y - 2 * step_h
col_w = int(W * 0.055)
for off in (-0.19, 0.0, 0.19):
    x = cx + W * off
    d.rectangle([x - col_w / 2, col_top, x + col_w / 2, col_bottom], fill=ACCENT)
    # capitals
    d.rectangle([x - col_w * 0.75, col_top - int(W * 0.02), x + col_w * 0.75, col_top], fill=ACCENT2)

# --- Entablature under the dome ---
ent_y = col_top - int(W * 0.02)
d.rectangle([cx - W * 0.29, ent_y - int(W * 0.035), cx + W * 0.29, ent_y], fill=ACCENT)

# --- Dome ---
dome_r = W * 0.20
dome_base = ent_y - int(W * 0.035)
d.pieslice([cx - dome_r, dome_base - dome_r, cx + dome_r, dome_base + dome_r], 180, 360, fill=ACCENT2)
# drum ring
d.rectangle([cx - dome_r, dome_base - int(W * 0.012), cx + dome_r, dome_base], fill=ACCENT)
# lantern + finial
lan_w = int(W * 0.05)
lan_top = dome_base - dome_r - int(W * 0.055)
d.rectangle([cx - lan_w / 2, lan_top, cx + lan_w / 2, dome_base - dome_r + int(W * 0.01)], fill=ACCENT2)
d.ellipse([cx - int(W * 0.018), lan_top - int(W * 0.045), cx + int(W * 0.018), lan_top - int(W * 0.009)], fill=ACCENT2)

out = Path(__file__).resolve().parent / "icon.png"
img.resize((S, S), Image.LANCZOS).save(out)
print(f"wrote {out}")
