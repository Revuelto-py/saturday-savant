# This script lives one directory below the repo root; ROOT points back at it so
# .env, the model artifacts and the shared modules resolve the same as before.
import os as _os, sys as _sys
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if ROOT not in _sys.path:
    _sys.path.insert(0, ROOT)
from PIL import Image
import os

BASE_DIR = ROOT
logo_path = os.path.join(BASE_DIR, 'static', 'logo.png')

img = Image.open(logo_path).convert("RGBA")

bbox = img.getbbox()
if bbox:
    img = img.crop(bbox)

sizes = {
    'logo-512.png': 512,
    'logo-256.png': 256,
    'logo-128.png': 128,
    'logo-64.png': 64,
    'logo-32.png': 32,
}
# NOTE: favicon-* are generated below from the white mark on navy, not from
# logo.png (which is a grey-background marketing render).

for filename, size in sizes.items():
    resized = img.copy()
    resized.thumbnail((size, size), Image.LANCZOS)
    resized.save(os.path.join(BASE_DIR, 'static', filename))
    print(f"Saved {filename} ({resized.size})")

# ── Favicons ────────────────────────────────────────────────────────────────
# The source logo.png is a marketing render: the mark on a GREY gradient with a
# drop shadow. Shrunk to 16–48px that ground reads as a grey tile, so favicons
# are built from the transparent mark instead (logo-mark-256.png, the same asset
# used in the navbar) with NO backdrop of its own — the tab strip, bookmark bar
# or results row shows through and the mark sits on the surface it lands on.
mark = Image.open(os.path.join(BASE_DIR, 'static', 'logo-mark-256.png')).convert('RGBA')
mbox = mark.getbbox()
if mbox:
    mark = mark.crop(mbox)

def _frame(size, bg=None, pad_pct=0.04):
    """One icon frame: the mark centred on `bg`. bg=None → transparent.

    Favicon padding is tight (4%) because there is no tile to breathe inside any
    more — the browser already sets the icon in its own gutter. The opaque
    apple-touch tile still wants the wider 12% margin.
    """
    canvas = Image.new('RGBA', (size, size), bg or (0, 0, 0, 0))
    pad = max(0, round(size * pad_pct))
    inner = size - 2 * pad
    m = mark.copy()
    m.thumbnail((inner, inner), Image.LANCZOS)
    canvas.alpha_composite(m, ((size - m.width) // 2, (size - m.height) // 2))
    return canvas

# favicon-<n> — browser tabs, bookmarks, Google. Saved as RGBA, not RGB: the
# transparency IS the point, and flattening here would put back the tile.
for filename, size in {'favicon-16.png': 16, 'favicon-32.png': 32,
                       'favicon-48.png': 48}.items():
    _frame(size).save(os.path.join(BASE_DIR, 'static', filename))
    print(f"Saved {filename} ({size}x{size}) — transparent mark")

# apple-touch-icon stays opaque on the brand navy: iOS ignores the alpha channel
# and composites a transparent icon onto black, which would leave the mark's own
# black linework invisible against the home-screen tile.
NAVY = (6, 13, 31, 255)          # #060d1f
_frame(180, bg=NAVY, pad_pct=0.12).convert('RGB').save(
    os.path.join(BASE_DIR, 'static', 'logo-mark-180.png'))
print("Saved logo-mark-180.png (180x180) — mark on navy (iOS has no alpha)")

# favicon.ico — the canonical root icon crawlers probe at /favicon.ico. Multi-
# resolution (16/32/48) so browsers/Google pick the size they want; each frame
# is rendered at its own size (not a single downscale) to stay crisp at 16px.
ico_frames = [_frame(s) for s in (16, 32, 48)]
ico_frames[-1].save(os.path.join(BASE_DIR, 'static', 'favicon.ico'),
                    format='ICO', sizes=[(16, 16), (32, 32), (48, 48)],
                    append_images=ico_frames[:-1])
print("Saved favicon.ico (16/32/48) — transparent mark")

print("Done")
