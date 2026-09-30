"""Per-game offensive-line unit stats: what each offense's line allowed.

Individual lineman stats (snaps, pressures allowed, pancakes) are charted by
PFF and team staffs and are not in any public feed. What IS public, for every
game since 2016, is the opposing defense's box score — sacks, QB hurries and
tackles for loss — which is exactly what an offensive line allowed. CFBD's
/games/teams endpoint carries it per team per game; this stores it from the
offense's side, next to the attempts it happened on.

    line_game_stats: one row per (game, offense)

Usage:
    python3 pipeline/fetch_line_stats.py            # active season (weekly chain)
    python3 pipeline/fetch_line_stats.py --all      # backfill 2016 → active season

QB hurries are only as complete as each stat crew's charting; some crews
record few or none. The player page says so in its glossary.
"""
# This script lives one directory below the repo root; ROOT points back at it so
# .env and the shared modules resolve the same as the other pipeline scripts.
import os as _os, sys as _sys
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if ROOT not in _sys.path:
    _sys.path.insert(0, ROOT)
import os
import sys

import cfbd
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv
from cfbd_retry import call_with_retry
from season_util import current_cfb_season

load_dotenv(_os.path.join(ROOT, '.env'))

FIRST_SEASON = 2016
ACTIVE = current_cfb_season()
SEASONS = range(FIRST_SEASON, ACTIVE + 1) if '--all' in sys.argv else [ACTIVE]
# CFBD numbers regular-season weeks 1-16 (week 0 arrives as week 1) and puts
# every bowl and playoff game in postseason week 1.
WEEKS = [(w, 'regular') for w in range(1, 17)] + [(1, 'postseason')]


def num(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def attempts(v):
    """'20-28' or '20/28' → 28."""
    s = str(v or '').replace('/', '-')
    return num(s.split('-', 1)[1]) if '-' in s else None


conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cur = conn.cursor()
cur.execute('''
    CREATE TABLE IF NOT EXISTS line_game_stats (
        game_id         BIGINT NOT NULL,
        season          INTEGER NOT NULL,
        week            INTEGER,
        season_type     TEXT,
        team            TEXT NOT NULL,
        opponent        TEXT,
        pass_att        REAL,
        rush_att        REAL,
        rush_yds        REAL,
        sacks_allowed   REAL,
        hurries_allowed REAL,
        tfl_allowed     REAL,
        PRIMARY KEY (game_id, team))''')
cur.execute('CREATE INDEX IF NOT EXISTS idx_line_game_stats_season_team ON line_game_stats (season, team)')
conn.commit()

configuration = cfbd.Configuration(access_token=os.getenv('CFBD_API_KEY'))
total = 0
with cfbd.ApiClient(configuration) as api_client:
    games_api = cfbd.GamesApi(api_client)
    for season in SEASONS:
        rows = []
        for week, stype in WEEKS:
            res = call_with_retry(f'team game stats {season} wk{week} {stype}',
                                  games_api.get_game_team_stats,
                                  year=season, week=week, season_type=stype)
            for g in res or []:
                teams = list(g.teams or [])
                if len(teams) != 2:
                    continue
                stats = [{s.category: s.stat for s in (t.stats or [])} for t in teams]
                for i in (0, 1):
                    off, dfn = stats[i], stats[1 - i]
                    rows.append((
                        g.id, season, week, stype, teams[i].team, teams[1 - i].team,
                        attempts(off.get('completionAttempts')),
                        num(off.get('rushingAttempts')), num(off.get('rushingYards')),
                        num(dfn.get('sacks')), num(dfn.get('qbHurries')),
                        num(dfn.get('tacklesForLoss')),
                    ))
        if rows:
            execute_values(cur, '''
                INSERT INTO line_game_stats (game_id, season, week, season_type, team, opponent,
                    pass_att, rush_att, rush_yds, sacks_allowed, hurries_allowed, tfl_allowed)
                VALUES %s
                ON CONFLICT (game_id, team) DO UPDATE SET
                    week = EXCLUDED.week, season_type = EXCLUDED.season_type,
                    opponent = EXCLUDED.opponent, pass_att = EXCLUDED.pass_att,
                    rush_att = EXCLUDED.rush_att, rush_yds = EXCLUDED.rush_yds,
                    sacks_allowed = EXCLUDED.sacks_allowed,
                    hurries_allowed = EXCLUDED.hurries_allowed,
                    tfl_allowed = EXCLUDED.tfl_allowed''', rows)
            conn.commit()
        total += len(rows)
        print(f'{season}: {len(rows)} team-games', flush=True)

conn.close()
print(f'Saved {total} line game rows')

try:
    from cache_notify import notify_cache_clear
    notify_cache_clear()
except Exception:
    pass
