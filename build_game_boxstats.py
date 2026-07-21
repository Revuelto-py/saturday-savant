"""Extract per-game team box stats from stored ESPN summaries into game_boxstats.

Turnovers, penalties, and time of possession are already inside the summaries
the site stores for every completed game (2016–2025) — this pulls them into a
flat table so rolling-window "recent form" features (e.g. turnover margin over
a team's last 3 games) can be built without re-parsing gzip JSON each time.

Point-in-time safe by construction: one row per completed game, consumed only
by feature code that restricts itself to games finishing before a kickoff.

Usage:  python3 build_game_boxstats.py
"""
import gzip
import json
import os

import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=True)

conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cur = conn.cursor()
cur.execute('''
    CREATE TABLE IF NOT EXISTS game_boxstats (
        game_id    BIGINT NOT NULL,
        team       TEXT NOT NULL,
        is_home    INTEGER,
        turnovers  REAL,
        penalties  REAL,
        penalty_yards REAL,
        poss_seconds  REAL,
        total_yards   REAL,
        PRIMARY KEY (game_id, team))''')
conn.commit()


def num(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def poss_to_seconds(v):
    try:
        m, s = str(v).split(':')
        return int(m) * 60 + int(s)
    except Exception:
        return None


cur.execute('''SELECT s.game_id, s.summary_gz FROM game_summaries s
               JOIN games g ON g.id = s.game_id WHERE g.completed = 1''')
rows_out, scanned = [], 0
for gid, gz in cur.fetchall():
    scanned += 1
    try:
        summary = json.loads(gzip.decompress(gz))
    except Exception:
        continue
    teams = ((summary.get('boxscore') or {}).get('teams') or [])
    for t in teams:
        name = ((t.get('team') or {}).get('displayName')
                or (t.get('team') or {}).get('location'))
        if not name:
            continue
        stats = {x.get('name'): x.get('displayValue') for x in (t.get('statistics') or [])}
        pen = stats.get('totalPenaltiesYards') or ''
        pn = py = None
        if '-' in str(pen):
            a, b = str(pen).split('-', 1)
            pn, py = num(a), num(b)
        rows_out.append((
            gid, name,
            1 if t.get('homeAway') == 'home' else 0,
            num(stats.get('turnovers')), pn, py,
            poss_to_seconds(stats.get('possessionTime')),
            num(stats.get('totalYards')),
        ))

execute_values(cur, '''
    INSERT INTO game_boxstats (game_id, team, is_home, turnovers, penalties,
                               penalty_yards, poss_seconds, total_yards) VALUES %s
    ON CONFLICT (game_id, team) DO UPDATE SET
        turnovers=EXCLUDED.turnovers, penalties=EXCLUDED.penalties,
        penalty_yards=EXCLUDED.penalty_yards, poss_seconds=EXCLUDED.poss_seconds,
        total_yards=EXCLUDED.total_yards''', rows_out, page_size=1000)
conn.commit()
print(f"scanned {scanned} summaries -> {len(rows_out)} team-game rows")
cur.execute('SELECT count(*), count(turnovers) FROM game_boxstats')
print("game_boxstats rows / with turnovers:", cur.fetchone())
conn.close()
