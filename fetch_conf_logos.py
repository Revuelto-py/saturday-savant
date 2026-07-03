"""Populate a conference_logos table with a logo URL for every FBS
conference in the teams table.

Source — ESPN's conference-logo CDN, the same host already used for team
logos in fetch_dark_logos.py:
    https://a.espncdn.com/i/teamlogos/ncaa_conf/500/{espn_id}.png
ESPN does not publish dark/white conference variants (every
`ncaa_conf/500-dark/{id}` 404s except SEC), so the UI renders these on the
site's existing white logo chip (.team-logo-bg) for consistent visibility
on the dark theme — the same treatment used for colored logos elsewhere.

Pac-12 exception — ESPN still serves the retired pre-2024 blue-shield-with-
wave mark. The Pac-12 unveiled a black monochrome shield in April 2026 as
part of its post-realignment rebuild, so its logo is sourced from Wikimedia
(the current mark) instead of ESPN.

Follows the same standalone structure as the other fetch_*.py scripts
(direct psycopg2 connection, commit, try/finally).
"""

import os

import psycopg2
import requests as req
from dotenv import load_dotenv

load_dotenv()

# DB conference name -> ESPN group id (FBS ids from CFBD get_conferences()).
ESPN_CONF_ID = {
    'ACC': 1,
    'Big Ten': 5,
    'Big 12': 4,
    'SEC': 8,
    'American Athletic': 151,
    'Sun Belt': 37,
    'Mid-American': 15,
    'Conference USA': 12,
    'Mountain West': 17,
    'Pac-12': 9,
    'FBS Independents': 18,
}
ESPN_STD  = 'https://a.espncdn.com/i/teamlogos/ncaa_conf/500/{id}.png'
ESPN_DARK = 'https://a.espncdn.com/i/teamlogos/ncaa_conf/500-dark/{id}.png'
# Current (April 2026) Pac-12 rebrand — black monochrome shield, no border.
# Sourced from Wikimedia (Pac-12_2026_Logo.svg) and self-hosted in /static
# because Wikimedia blocks hotlinking, so we serve it same-origin instead.
PAC12_2026 = '/static/pac12-2026.png'


def reachable(url):
    """True if the URL serves an image (GET — ESPN doesn't answer HEAD
    reliably). Local same-origin paths (/static/...) are assumed present."""
    if not url:
        return False
    if url.startswith('/'):
        return os.path.exists(os.path.join(os.path.dirname(__file__), url.lstrip('/')))
    try:
        r = req.get(url, timeout=10)
        return r.status_code == 200 and r.headers.get('Content-Type', '').startswith('image/')
    except Exception:
        return False


conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cursor = conn.cursor()
try:
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS conference_logos (
            conference TEXT PRIMARY KEY,
            logo       TEXT,
            logo_dark  TEXT,
            source     TEXT,
            updated_at TIMESTAMPTZ DEFAULT now()
        )
    ''')
    conn.commit()

    cursor.execute('SELECT DISTINCT conference FROM teams WHERE conference IS NOT NULL ORDER BY conference')
    conferences = [r[0] for r in cursor.fetchall()]

    stored = 0
    for conf in conferences:
        cid = ESPN_CONF_ID.get(conf)
        if cid is None:
            print(f'  SKIP (no ESPN id mapped): {conf}')
            continue

        if conf == 'Pac-12':
            logo, source = PAC12_2026, 'Wikimedia (2026 rebrand)'
        else:
            logo, source = ESPN_STD.format(id=cid), 'ESPN'

        dark_url = ESPN_DARK.format(id=cid)
        logo_dark = dark_url if reachable(dark_url) else None

        if not reachable(logo):
            print(f'  WARN: logo not reachable for {conf}: {logo}')

        cursor.execute('''
            INSERT INTO conference_logos (conference, logo, logo_dark, source, updated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (conference) DO UPDATE
              SET logo = EXCLUDED.logo, logo_dark = EXCLUDED.logo_dark,
                  source = EXCLUDED.source, updated_at = now()
        ''', (conf, logo, logo_dark, source))
        stored += 1
        print(f'  {conf:<20} {source}{"  (+dark)" if logo_dark else ""}')

    conn.commit()
    print(f'\nStored {stored} conference logos.')
finally:
    cursor.close()
    conn.close()
