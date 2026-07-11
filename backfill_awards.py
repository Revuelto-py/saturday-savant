"""Build the awards table: player hardware + team championships, 2016-2025.

Player awards come from ESPN's awards API — winners are keyed by ESPN
athlete id, which IS our players.id (the same continuity backfill_nfl.py
relies on), so no name matching is involved. FCS/DII/DIII and coaching
awards are excluded.

Team awards are derived, not fetched:
  • National Champions — winner of the CFP title game in our games table.
  • {Conference} Champions — conference title games are discovered on
    ESPN's scoreboard for championship window (Nov 25-Dec 20; the wide
    window also covers 2020's COVID-shifted slate), whose event ids match
    our games ids, and the winner comes from our own scores. Conferences
    with no title game that season (Big 12 2016, Sun Belt pre-2018, the
    cancelled 2020 Sun Belt CCG) get the small curated list below —
    champions were decided by standings in those cases.

Rerunnable: the table is rebuilt in full each run (it's ~300 rows).

Usage:  python3 backfill_awards.py
"""
import os
import re
import json
import urllib.request
import psycopg2
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=True)

SEASONS = range(2016, 2026)

# ESPN award id -> display name (FBS player hardware only)
PLAYER_AWARDS = {
    9:  'Heisman Trophy',
    14: 'Maxwell Award',
    20: 'Walter Camp Award',
    2:  'Bednarik Award',
    15: 'Nagurski Trophy',
    30: 'Lott IMPACT Trophy',
    3:  "Davey O'Brien Award",
    12: 'Unitas Golden Arm Award',
    5:  'Doak Walker Award',
    6:  'Biletnikoff Award',
    11: 'Mackey Award',
    17: 'Outland Trophy',
    26: 'Rimington Trophy',
    19: 'Lombardi Award',
    4:  'Butkus Award',
    25: 'Hendricks Award',
    10: 'Thorpe Award',
    13: 'Lou Groza Award',
    18: 'Ray Guy Award',
    31: 'Paul Hornung Award',
    28: 'Campbell Trophy',
}

# Conference-name normalization from ESPN scoreboard headlines
# ("Dr Pepper ACC Championship Game" -> ACC)
CONF_PATTERNS = [
    (r'\bSEC\b', 'SEC'), (r'\bBig Ten\b', 'Big Ten'), (r'\bBig 12\b', 'Big 12'),
    (r'\bACC\b', 'ACC'), (r'\bPac-?12\b', 'Pac-12'),
    (r'\bAmerican\b', 'American Athletic'), (r'\bMountain West\b', 'Mountain West'),
    (r'\bMAC\b', 'MAC'), (r'\bConference USA\b|\bC-?USA\b', 'Conference USA'),
    (r'\bSun Belt\b', 'Sun Belt'),
]

# Standings-decided champions for seasons a conference held no title game.
CURATED_CONF_CHAMPS = [
    (2016, 'Big 12', 'Oklahoma'),                 # round-robin, no CCG until 2017
    (2016, 'Sun Belt', 'Appalachian State'),      # co-champions; CCG began 2018
    (2016, 'Sun Belt', 'Arkansas State'),
    (2017, 'Sun Belt', 'Appalachian State'),      # co-champions
    (2017, 'Sun Belt', 'Troy'),
    (2020, 'Sun Belt', 'Coastal Carolina'),       # CCG cancelled (COVID); co-champions
    (2020, 'Sun Belt', 'Louisiana'),
]

conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cur = conn.cursor()
cur.execute('''
    CREATE TABLE IF NOT EXISTS awards (
        season    INTEGER NOT NULL,
        award     TEXT NOT NULL,
        kind      TEXT NOT NULL,        -- 'player' | 'team'
        player_id INTEGER,
        team      TEXT
    )
''')
cur.execute('CREATE INDEX IF NOT EXISTS idx_awards_player ON awards(player_id)')
cur.execute('CREATE INDEX IF NOT EXISTS idx_awards_team ON awards(team, season)')
cur.execute('DELETE FROM awards')
conn.commit()


def fetch_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def player_team(pid, season):
    cur.execute('SELECT team FROM rosters WHERE player_id=%s AND season=%s', (pid, season))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute('SELECT team FROM players WHERE id=%s', (pid,))
    row = cur.fetchone()
    return row[0] if row else None


def ingest_player_awards():
    saved = missing = 0
    for season in SEASONS:
        for aid, name in PLAYER_AWARDS.items():
            url = (f'https://sports.core.api.espn.com/v2/sports/football/leagues/'
                   f'college-football/seasons/{season}/awards/{aid}?lang=en')
            try:
                d = fetch_json(url)
            except Exception:
                missing += 1
                continue
            for w in d.get('winners', []):
                ref = (w.get('athlete') or {}).get('$ref', '')
                m = re.search(r'athletes/(\d+)', ref)
                if not m:
                    continue
                pid = int(m.group(1))
                cur.execute('''INSERT INTO awards (season, award, kind, player_id, team)
                               VALUES (%s,%s,'player',%s,%s)''',
                            (season, name, pid, player_team(pid, season)))
                saved += 1
        conn.commit()
        print(f"{season}: player awards ingested", flush=True)
    print(f"player awards: {saved} saved, {missing} award-seasons unavailable", flush=True)


def ingest_team_awards():
    # National champions from our own games table
    cur.execute('''
        SELECT season, CASE WHEN home_points > away_points THEN home_team ELSE away_team END
        FROM games WHERE completed = 1 AND notes ILIKE '%%national championship%%'
        ORDER BY season''')
    for season, champ in cur.fetchall():
        cur.execute('''INSERT INTO awards (season, award, kind, team)
                       VALUES (%s, 'National Champions', 'team', %s)''', (season, champ))
        print(f"{season}: National Champions — {champ}", flush=True)

    # Conference champions: CCG events from ESPN, winners from our scores
    for season in SEASONS:
        url = ('https://site.api.espn.com/apis/site/v2/sports/football/college-football/'
               f'scoreboard?dates={season}1125-{season}1220&groups=80&limit=300')
        try:
            d = fetch_json(url)
        except Exception as e:
            print(f"{season}: scoreboard fetch failed — {type(e).__name__}", flush=True)
            continue
        found = []
        for e in d.get('events', []):
            comp = (e.get('competitions') or [{}])[0]
            headline = next((n.get('headline', '') for n in comp.get('notes', [])), '')
            if not re.search(r'championship', headline, re.I) or 'national' in headline.lower():
                continue
            conf = next((label for pat, label in CONF_PATTERNS if re.search(pat, headline, re.I)), None)
            if not conf:
                continue
            cur.execute('''SELECT CASE WHEN home_points > away_points THEN home_team ELSE away_team END
                           FROM games WHERE id = %s AND completed = 1
                           AND home_points IS NOT NULL AND away_points IS NOT NULL''',
                        (int(e['id']),))
            row = cur.fetchone()
            if row:
                found.append((conf, row[0]))
        for conf, champ in found:
            cur.execute('''INSERT INTO awards (season, award, kind, team)
                           VALUES (%s, %s, 'team', %s)''', (season, f'{conf} Champions', champ))
        print(f"{season}: {len(found)} conference champions "
              f"({', '.join(c for c, _ in sorted(found))})", flush=True)

    for season, conf, champ in CURATED_CONF_CHAMPS:
        cur.execute('''INSERT INTO awards (season, award, kind, team)
                       VALUES (%s, %s, 'team', %s)''', (season, f'{conf} Champions', champ))
    conn.commit()


def main():
    ingest_player_awards()
    ingest_team_awards()
    cur.execute("SELECT kind, COUNT(*) FROM awards GROUP BY kind")
    print('totals:', dict(cur.fetchall()), flush=True)
    conn.close()


if __name__ == '__main__':
    main()


# Data changed — tell the live site to drop its in-memory page cache so the
# update is visible immediately instead of after the cache TTL.
try:
    from cache_notify import notify_cache_clear
    notify_cache_clear()
except Exception:
    pass
