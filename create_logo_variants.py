from PIL import Image
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
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
# The source logo.png is a marketing render: a white mark on a GREY gradient
# with a drop shadow. Shrunk to 16–48px it loses the white fill and reads as
# dark linework on a light ground — the inverse of the brand. Instead build the
# favicons from the transparent white mark (logo-mark-256.png, the same asset
# used in the navbar) composited on the brand navy, so they read as white-on-
# dark at every size and on Google's white results background.
NAVY = (6, 13, 31, 255)          # #060d1f — matches <meta name="theme-color">
mark = Image.open(os.path.join(BASE_DIR, 'static', 'logo-mark-256.png')).convert('RGBA')
mbox = mark.getbbox()
if mbox:
    mark = mark.crop(mbox)

# favicon-<n> (browser tabs / Google) + logo-mark-180 (apple-touch-icon)
favicons = {'favicon-16.png': 16, 'favicon-32.png': 32, 'favicon-48.png': 48,
            'logo-mark-180.png': 180}
def _composite(size):
    """White mark centred on the brand navy, ~12% padding — one favicon frame."""
    canvas = Image.new('RGBA', (size, size), NAVY)
    pad = max(1, round(size * 0.12))           # ~12% breathing room each side
    inner = size - 2 * pad
    m = mark.copy()
    m.thumbnail((inner, inner), Image.LANCZOS)
    canvas.alpha_composite(m, ((size - m.width) // 2, (size - m.height) // 2))
    return canvas

for filename, size in favicons.items():
    _composite(size).convert('RGB').save(os.path.join(BASE_DIR, 'static', filename))
    print(f"Saved {filename} ({size}x{size}) — white mark on navy")

# favicon.ico — the canonical root icon crawlers probe at /favicon.ico. Multi-
# resolution (16/32/48) so browsers/Google pick the size they want; each frame
# is composited at its own size (not a single downscale) to stay crisp at 16px.
ico_frames = [_composite(s).convert('RGBA') for s in (16, 32, 48)]
ico_frames[-1].save(os.path.join(BASE_DIR, 'static', 'favicon.ico'),
                    format='ICO', sizes=[(16, 16), (32, 32), (48, 48)],
                    append_images=ico_frames[:-1])
print("Saved favicon.ico (16/32/48) — white mark on navy")

print("Done")
