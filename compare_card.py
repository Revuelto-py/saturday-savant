"""Share images for /compare: a face-off drawn with Pillow in the site's typeface.

Three sizes: the post (1080x1350, which feeds show uncropped), the story
(1080x1920) and the wide link preview (1200x630) that a pasted URL unfurls
into. They replace the page's html2canvas export, which shipped a 200KB library
to every visitor and took seconds on a phone. main.compare_card() decides what
goes in and caches the bytes; nothing here touches the database.

`card` is a plain dict:
    kind   'QB comparison, 2026'
    note   'Bars: percentile among qualified FBS QBs'
    ents   two of {first, last, meta, color, img, team(bool)}
    rows   {label, av, bv, ap, bp, lead}  lead 0 / 1 / None for a tie
"""
import io
import os
from functools import lru_cache

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

HERE = os.path.dirname(os.path.abspath(__file__))
FONT = os.path.join(HERE, 'static', 'fonts', 'MonaSans.ttf')
MARK = os.path.join(HERE, 'static', 'logo-mark-256.png')
SIZES = {'post': (1080, 1350), 'story': (1080, 1920), 'wide': (1200, 630)}
BG = (8, 9, 11)
WHITE, SOFT, DIM, FAINT = (255, 255, 255), (231, 233, 234), (169, 175, 180), (143, 150, 156)


@lru_cache(maxsize=64)
def font(size, weight=600, width=100):
    f = ImageFont.truetype(FONT, size)
    f.set_variation_by_axes([width, weight])
    return f


def rgb(hex_color):
    h = (hex_color or '#5d6268').lstrip('#')
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def mix(fg, bg, t):
    """fg laid over bg at opacity t, as a solid colour: ImageDraw writes alpha
    straight into the pixel instead of blending, so a translucent line drawn on
    the canvas comes out as solid white once the image is flattened."""
    return tuple(round(f * t + b * (1 - t)) for f, b in zip(fg, bg))


def lum(c):
    ch = [v / 255 for v in c]
    ch = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in ch]
    return .2126 * ch[0] + .7152 * ch[1] + .0722 * ch[2]


def fetch(url):
    if not url:
        return None
    try:
        r = requests.get(url, timeout=8)
        return Image.open(io.BytesIO(r.content)).convert('RGBA') if r.status_code == 200 else None
    except Exception:
        return None


def glow(img, color, cx, cy, rx, ry, alpha):
    """A soft radial wash of `color`, drawn small and blurred, then scaled up."""
    w, h = img.size
    k = 4
    layer = Image.new('RGBA', (w // k, h // k), color + (0,))
    ImageDraw.Draw(layer).ellipse([(cx - rx) / k, (cy - ry) / k, (cx + rx) / k, (cy + ry) / k],
                                  fill=color + (alpha,))
    layer = layer.filter(ImageFilter.GaussianBlur(min(rx, ry) / k * .45)).resize((w, h), Image.BILINEAR)
    img.alpha_composite(layer)


def fade(img, top, bottom):
    """The floor shade under the faces, so names sit on solid ground."""
    w = img.size[0]
    grad = Image.new('L', (1, bottom - top))
    for y in range(bottom - top):
        grad.putpixel((0, y), int(255 * (y / (bottom - top)) ** 1.4))
    shade = Image.new('RGBA', (w, bottom - top), BG + (255,))
    shade.putalpha(grad.resize((w, bottom - top)))
    img.alpha_composite(shade, (0, top))


def fit(text, max_w, size, weight, width=100, floor=28):
    while size > floor and font(size, weight, width).getlength(text) > max_w:
        size -= 2
    return font(size, weight, width)


def wrap(text, f, max_w):
    words, lines, cur = text.split(), [], ''
    for w in words:
        nxt = (cur + ' ' + w).strip()
        if cur and f.getlength(nxt) > max_w:
            lines.append(cur)
            cur = w
        else:
            cur = nxt
    return lines + [cur] if cur else lines


def face(img, ent, box, mirror):
    """A player's cutout, bottom-anchored and filling `box` (x, y, w, h); a
    team's logo, centred. Initials on the team colour when there is no image."""
    x, y, w, h = box
    pic = fetch(ent.get('img'))
    if pic is None:
        d = ImageDraw.Draw(img)
        s = min(w, h) * .62
        cx, cy = x + w / 2, y + h - s / 2 - 24
        d.ellipse([cx - s / 2, cy - s / 2, cx + s / 2, cy + s / 2], fill=rgb(ent['color']))
        initials = ((ent.get('first') or ' ')[0] + (ent.get('last') or ' ')[0]).strip()
        d.text((cx, cy), initials, font=font(int(s * .38), 850, 78), fill=WHITE, anchor='mm')
        return
    if ent.get('team'):
        s = int(min(w, h) * .78)
        pic.thumbnail((s, s), Image.LANCZOS)
        img.alpha_composite(pic, (int(x + (w - pic.width) / 2), int(y + (h - pic.height) / 2)))
        return
    pic = pic.resize((w, int(w * pic.height / pic.width)), Image.LANCZOS)
    if mirror:
        pic = ImageOps.mirror(pic)
    img.alpha_composite(pic, (x, y + h - pic.height))


def bar(d, img, x0, x1, y, pct, color, right_anchored):
    """A percentile bar in a track; None draws a hatched track (no FBS rank)."""
    h = 14
    track = Image.new('RGBA', img.size, (0, 0, 0, 0))
    td = ImageDraw.Draw(track)
    td.rounded_rectangle([x0, y - h / 2, x1, y + h / 2], radius=h / 2, fill=(255, 255, 255, 16))
    if pct is None:
        for hx in range(int(x0) - h, int(x1), 9):
            td.line([(hx, y + h / 2), (hx + h, y - h / 2)], fill=(255, 255, 255, 22), width=3)
        mask = Image.new('L', img.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle([x0, y - h / 2, x1, y + h / 2], radius=h / 2, fill=255)
        track.putalpha(Image.composite(track.getchannel('A'), mask, mask))
        img.alpha_composite(track)
        return
    img.alpha_composite(track)
    w = max(h, (x1 - x0) * max(pct, 2) / 100)
    fx0, fx1 = (x1 - w, x1) if right_anchored else (x0, x0 + w)
    c = rgb(color)
    d.rounded_rectangle([fx0, y - h / 2, fx1, y + h / 2], radius=h / 2, fill=c,
                        outline=mix(WHITE, c, .35) if lum(c) < .12 else None, width=2)


def value_font(lead, side):
    if lead is None:
        return font(27, 650), SOFT
    return (font(28, 800), WHITE) if lead == side else (font(27, 600), FAINT)


def brand(img, d, x, y, size):
    mark = Image.open(MARK).convert('RGBA').resize((size, size), Image.LANCZOS)
    img.alpha_composite(mark, (x, y))
    d.text((x + size + 12, y + size / 2), 'Saturday Savant', font=font(int(size * .66), 700), fill=WHITE, anchor='lm')


def render(card, fmt):
    W, H = SIZES[fmt]
    img = Image.new('RGBA', (W, H), BG + (255,))
    a, b = card['ents']
    ca, cb = rgb(a['color']), rgb(b['color'])
    wide = fmt == 'wide'
    # Each side's colour from its own corner, kept to the faces so the stat
    # rows below sit on near-black; a light colour (Oklahoma's white beside
    # Alabama) is washed far thinner so it reads as a tint, not a fog.
    for c, cx in ((ca, 0), (cb, W)):
        alpha = 60 if lum(c) > .5 else 150
        if wide:
            glow(img, c, cx, H * .55, W * .42, H * .75, alpha)
        else:
            glow(img, c, cx, H * .2, W * .5, H * .26, alpha)
    d = ImageDraw.Draw(img)

    if wide:
        pw = 540
        box_h = int(pw * 436 / 600)
        face(img, a, (-24, H - box_h - 0, pw, box_h), False)
        face(img, b, (W - pw + 24, H - box_h - 0, pw, box_h), True)
        brand(img, d, 36, 32, 40)
        return png(img)

    pad = 44
    brand(img, d, pad, 34, 40)
    d.text((W - pad, 54), card['kind'], font=font(24, 700), fill=SOFT, anchor='rm')

    story = fmt == 'story'
    pw = 600 if story else 560
    top, floor = 96, (800 if story else 600)
    box_h = floor - top
    face(img, a, (-28, top, pw, box_h), False)
    face(img, b, (W - pw + 28, top, pw, box_h), True)
    fade(img, floor - 150, floor)
    d = ImageDraw.Draw(img)
    for ent, x, anchor in ((a, pad, 'l'), (b, W - pad, 'r')):
        name = f"{ent.get('first') or ''} {ent['last']}".strip()
        f = fit(name, W / 2 - pad - 20, 62, 850, 78, floor=36)
        d.text((x, floor - 52), name, font=f, fill=WHITE, anchor=anchor + 's')
        d.text((x, floor - 16), ent.get('meta') or '', font=font(24, 600), fill=SOFT, anchor=anchor + 's')

    rows = card['rows'][:8 if story else 6]
    y = floor + 64
    fy = H - 92
    # Rows share whatever height the faces leave; a short list (four defensive
    # stats) sits centred with its heading rather than top-heavy over a gap.
    row_h = min(104, (fy - 24 - y) / max(len(rows), 1))
    y += (fy - 24 - y - row_h * len(rows)) / 2
    d.text((W / 2, y - 30), 'Where they differ most', font=font(22, 700), fill=(196, 200, 204), anchor='mm')
    vw, lw, gap = 96, 300, 20
    lx1 = pad + vw + gap + (W - 2 * pad - 2 * vw - lw - 4 * gap) / 2      # left bar's inner end
    rx0 = W - lx1
    for r in rows:
        d.line([(pad, y), (W - pad, y)], fill=mix(WHITE, BG, .08), width=2)
        cy = y + row_h / 2
        fa, ka = value_font(r['lead'], 0)
        fb, kb = value_font(r['lead'], 1)
        d.text((pad + vw, cy), r['av'], font=fa, fill=ka, anchor='rm')
        d.text((W - pad - vw, cy), r['bv'], font=fb, fill=kb, anchor='lm')
        bar(d, img, pad + vw + gap, lx1, cy, r['ap'], a['color'], True)
        bar(d, img, rx0, W - pad - vw - gap, cy, r['bp'], b['color'], False)
        d = ImageDraw.Draw(img)
        lf = font(23, 650)
        lines = wrap(r['label'], lf, lw)
        for i, line in enumerate(lines[:2]):
            d.text((W / 2, cy + (i - (len(lines[:2]) - 1) / 2) * 28), line, font=lf, fill=SOFT, anchor='mm')
        y += row_h

    d.line([(pad, fy), (W - pad, fy)], fill=mix(WHITE, BG, .1), width=2)
    d.text((pad, fy + 46), card['note'], font=font(22, 550), fill=DIM, anchor='lm')
    d.text((W - pad, fy + 46), 'saturdaysavant.com/compare', font=font(23, 800), fill=WHITE, anchor='rm')
    return png(img)


def png(img):
    buf = io.BytesIO()
    img.convert('RGB').save(buf, 'PNG', optimize=True)
    return buf.getvalue()


if __name__ == '__main__':
    # Smoke test: python3 compare_card.py OUT_DIR -> post.png, story.png, wide.png
    import sys
    R2 = 'https://pub-1d0241430ed04c3591fd11c3b2cef4c3.r2.dev'
    card = {
        'kind': 'QB comparison, 2026', 'note': 'Bars: percentile among qualified FBS QBs',
        'ents': [{'first': 'Jayden', 'last': 'Maiava', 'meta': 'USC, AP 19', 'color': '#9e2237', 'img': f'{R2}/4685454.png?v=780e459c'},
                 {'first': 'Darian', 'last': 'Mensah', 'meta': 'Miami, AP 4', 'color': '#005030', 'img': f'{R2}/5121169.png?v=6835adff'}],
        'rows': [{'label': 'EPA per rush play', 'av': '−0.720', 'bv': '1.069', 'ap': 2, 'bp': 98, 'lead': 1},
                 {'label': 'Interceptions per game', 'av': '0.5', 'bv': '0.0', 'ap': 35, 'bp': 95, 'lead': 1},
                 {'label': 'Average depth of target', 'av': '8.2', 'bv': '6.2', 'ap': 44, 'bp': 3, 'lead': 0},
                 {'label': 'Rush yards per game', 'av': '−0.7', 'bv': '15.8', 'ap': 13, 'bp': 53, 'lead': 1},
                 {'label': 'Air yards share', 'av': '49.5%', 'bv': '41.4%', 'ap': 40, 'bp': 8, 'lead': 0},
                 {'label': 'Usage rate', 'av': '32.8%', 'bv': '32.8%', 'ap': None, 'bp': None, 'lead': None}]}
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    for fmt in SIZES:
        with open(os.path.join(out, f'{fmt}.png'), 'wb') as fh:
            fh.write(render(card, fmt))
    print('wrote', out)
