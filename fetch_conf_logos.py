"""Populate a conference_logos table with a logo URL for every FBS
conference in the teams table, rendered bare on the site's dark theme
(no background chip) to match how team logos display.

Source — ESPN's conference-logo CDN, the same host used for team logos in
fetch_dark_logos.py:  https://a.espncdn.com/i/teamlogos/ncaa_conf/500/{id}.png
These are transparent, full-colour marks made for light backgrounds. Most
read fine on the dark UI, but a handful are dark (dark-blue ACC, black
2026 Pac-12, dark-red C-USA, near-black Big Ten/FBS Independents) and would
disappear. ESPN publishes no dark/white conference variants, so for those we
generate a white monochrome version (recolour the transparent mark white,
preserving its alpha) and self-host it — the same idea as team `logo_dark`.
Only wordmark-style marks are recoloured; the colour-detailed shields (SEC,
Big 12, MAC) already read on dark and keep their colour logo.

Pac-12 exception — ESPN still serves the retired blue-shield mark. The
current April-2026 rebrand (black monochrome shield) lives at
static/pac12-2026.png and is recoloured white here for the dark UI.

Standalone structure matching the other fetch_*.py scripts.
"""

import os

import psycopg2
import requests as req
from PIL import Image
from dotenv import load_dotenv

load_dotenv()

HERE = os.path.dirname(os.path.abspath(__file__))
WHITE_DIR = os.path.join(HERE, 'static', 'conf')

# DB conference name -> ESPN group id (FBS ids from CFBD get_conferences()).
ESPN_CONF_ID = {
    'ACC': 1, 'Big Ten': 5, 'Big 12': 4, 'SEC': 8,
    'American Athletic': 151, 'Sun Belt': 37, 'Mid-American': 15,
    'Conference USA': 12, 'Mountain West': 17, 'Pac-12': 9,
    'FBS Independents': 18,
}
ESPN_STD = 'https://a.espncdn.com/i/teamlogos/ncaa_conf/500/{id}.png'

# Conferences whose colour mark is too dark to read on the dark theme — we
# serve a white monochrome version instead (their marks are wordmark-style,
# so a white silhouette stays legible).
DARK_CONFS = {'ACC', 'Big Ten', 'Conference USA', 'FBS Independents', 'Pac-12'}

# Local source for the current Pac-12 mark (ESPN's is out of date).
PAC12_SRC = os.path.join(HERE, 'static', 'pac12-2026.png')


def slug(name):
    return name.lower().replace(' ', '-')


def make_white(src_bytes_or_path, out_path):
    """Recolour a transparent logo to white, keeping its alpha (and smooth
    edges), and save it. Accepts raw bytes or a file path."""
    if isinstance(src_bytes_or_path, (bytes, bytearray)):
        import io
        im = Image.open(io.BytesIO(src_bytes_or_path))
    else:
        im = Image.open(src_bytes_or_path)
    im = im.convert('RGBA')
    alpha = im.split()[3]
    white = Image.new('RGBA', im.size, (255, 255, 255, 0))
    white.putalpha(alpha)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    white.save(out_path)


conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cursor = conn.cursor()
try:
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS conference_logos (
            conference TEXT PRIMARY KEY,
            logo       TEXT,
            source     TEXT,
            updated_at TIMESTAMPTZ DEFAULT now()
        )
    ''')
    # older schema had a logo_dark column; harmless if it lingers
    conn.commit()

    cursor.execute('SELECT DISTINCT conference FROM teams WHERE conference IS NOT NULL ORDER BY conference')
    conferences = [r[0] for r in cursor.fetchall()]

    stored = 0
    for conf in conferences:
        cid = ESPN_CONF_ID.get(conf)
        if cid is None:
            print(f'  SKIP (no ESPN id mapped): {conf}')
            continue

        if conf in DARK_CONFS:
            out = os.path.join(WHITE_DIR, f'{slug(conf)}.png')
            if conf == 'Pac-12':
                make_white(PAC12_SRC, out)
                source = 'Wikimedia 2026 rebrand, recoloured white'
            else:
                r = req.get(ESPN_STD.format(id=cid), timeout=15)
                make_white(r.content, out)
                source = 'ESPN, recoloured white for dark UI'
            logo = '/static/conf/' + f'{slug(conf)}.png'
        else:
            logo = ESPN_STD.format(id=cid)
            source = 'ESPN'

        cursor.execute('''
            INSERT INTO conference_logos (conference, logo, source, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (conference) DO UPDATE
              SET logo = EXCLUDED.logo, source = EXCLUDED.source, updated_at = now()
        ''', (conf, logo, source))
        stored += 1
        print(f'  {conf:<20} {source}')

    conn.commit()
    print(f'\nStored {stored} conference logos.')
finally:
    cursor.close()
    conn.close()
