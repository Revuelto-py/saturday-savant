"""Give every team a real brand colour, derived from its own logo.

WHY THIS EXISTS
    teams.color comes from CFBD, and for a programme carried only so its logo can
    appear on an opponent's schedule — every FCS team on this site — it is very
    often "#000000" or empty. ESPN is no better: its team endpoint and its game
    summaries both report Merrimack as 000000. There is no feed to fix this from.

    But the brand is in the logo. Every team already has one stored, and the
    dominant non-neutral colour in it IS the team's colour. Measured against
    teams whose feed colour is known good, the derivation lands within a few
    points: Colorado #cfb87c -> #d2ba7d, Penn State #041e42 -> #051440,
    Georgia #ba0c2f -> #bb0529. That agreement is what makes it trustworthy
    where the feed is blank.

    For a team whose primary really is black (Army, Navy), the derivation
    returns their OTHER brand colour — Army gold rather than Army black. On a
    near-black site that is the right answer: a real colour that can be seen,
    rather than an invisible one or a generic slate.

SCOPE
    Only teams whose stored colour cannot be used are touched: missing, malformed
    or so dark it disappears against the site's surfaces. A team with a usable
    colour is never overwritten.

Usage:  python3 tools/backfill_team_colors.py            # report only
        python3 tools/backfill_team_colors.py --write    # apply
"""

# This script lives one directory below the repo root; ROOT points back at it so
# .env, the model artifacts and the shared modules resolve the same as before.
import os as _os, sys as _sys
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if ROOT not in _sys.path:
    _sys.path.insert(0, ROOT)

import colorsys
import io
import os
import sys
from collections import Counter

import psycopg2
import requests
from dotenv import load_dotenv
from PIL import Image

load_dotenv(_os.path.join(ROOT, '.env'))

WRITE = '--write' in sys.argv

# Matches main.py's team_hex(): below this a colour is black in all but name.
LUM_FLOOR = 0.045
HEADERS = {'User-Agent': 'Mozilla/5.0 (saturdaysavant colour backfill)'}


def luminance(hex6):
    r, g, b = (int(hex6[i:i + 2], 16) for i in (0, 2, 4))
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255


def usable(value):
    """The same test the templates apply, so this script and the site agree."""
    if not value:
        return False
    h = str(value).strip().lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    if len(h) != 6 or any(c not in '0123456789abcdefABCDEF' for c in h):
        return False
    return luminance(h.lower()) >= LUM_FLOOR


def dominant_colour(url, buckets=24):
    """The most common branded pixel in a logo, averaged for a true value.

    White, black and grey are skipped: they are the scaffolding almost every
    logo is drawn with, and none of them is what anyone means by a team colour.
    """
    raw = requests.get(url, headers=HEADERS, timeout=15)
    if not raw.ok:
        return None
    im = Image.open(io.BytesIO(raw.content)).convert('RGBA').resize((96, 96))
    px = list(im.getdata())

    def branded(p):
        r, g, b, a = p
        if a < 160:
            return None
        h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        if l > 0.93 or l < 0.06 or s < 0.18:
            return None
        return (round(h * buckets), round(l * 6), round(s * 4))

    tally = Counter(k for k in (branded(p) for p in px) if k)
    if not tally:
        return None
    win = tally.most_common(1)[0][0]
    acc, n = [0, 0, 0], 0
    for p in px:
        if branded(p) == win:
            acc[0] += p[0]; acc[1] += p[1]; acc[2] += p[2]; n += 1
    out = '#%02x%02x%02x' % tuple(v // n for v in acc)
    return out if usable(out) else None


def main():
    conn = psycopg2.connect(os.getenv('DATABASE_URL'))
    try:
        cur = conn.cursor()
        cur.execute('SELECT name, color, alt_color, logo, logo_dark FROM teams ORDER BY name')
        rows = cur.fetchall()

        todo = [(n, c, a, lo, ld) for n, c, a, lo, ld in rows if not usable(c)]
        print(f'{len(rows)} teams, {len(todo)} without a usable colour\n')

        fixed, from_alt, failed = [], [], []
        for name, color, alt, logo, logo_dark in todo:
            # A team's own alternate is a real brand colour and costs nothing.
            if usable(alt):
                from_alt.append((name, color, alt))
                if WRITE:
                    cur.execute('UPDATE teams SET color = %s WHERE name = %s', (alt, name))
                continue
            src = logo or logo_dark
            got = dominant_colour(src) if src else None
            if got:
                fixed.append((name, color, got))
                if WRITE:
                    cur.execute('UPDATE teams SET color = %s WHERE name = %s', (got, name))
            else:
                failed.append((name, color, src))
        if WRITE:
            conn.commit()

        if from_alt:
            print(f'— {len(from_alt)} taken from the team\'s own alternate colour —')
            for n, old, new in from_alt:
                print(f'   {n:28} {str(old):9} -> {new}')
        if fixed:
            print(f'\n— {len(fixed)} derived from the logo —')
            for n, old, new in fixed:
                print(f'   {n:28} {str(old):9} -> {new}  (lum {luminance(new[1:]):.3f})')
        if failed:
            print(f'\n— {len(failed)} could not be resolved (no logo, or no branded pixels) —')
            for n, old, src in failed:
                print(f'   {n:28} {str(old):9} logo={src}')
            print('  These keep the template fallback, which is why it still exists.')

        print(f'\n{"WROTE" if WRITE else "DRY RUN — nothing written"}: '
              f'{len(from_alt) + len(fixed)} teams would get a real colour, {len(failed)} left.')
        if not WRITE:
            print('Re-run with --write to apply.')
    finally:
        conn.close()

    try:
        from cache_notify import notify_cache_clear
        if WRITE:
            notify_cache_clear()
    except Exception:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
