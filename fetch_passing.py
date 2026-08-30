"""Fetch CFBD's play-level passing data into the passing_plays table.

What this adds that the site did not have: where a pass went and who created
the yards. Each attempt carries air yards (how far downfield the ball travelled
from the line of scrimmage, negative behind it), a short/deep x left/middle/right
location, yards after catch, and BOTH the passer and the target — so this is
receiver data as much as quarterback data.

Why plays and not the season/game aggregates: CFBD also exposes
/passing/players/season, /passing/teams/season and their per-game variants, and
every number in them can be recomputed from these rows. Only the play rows carry
the location grid, so storing aggregates instead would mean re-fetching a whole
season the first time we want a split the API does not pre-compute. The volume
is trivial by this site's standards — roughly 7k pass plays a full week, ~90k a
season, a fraction of player_stats.

Why REST and not the cfbd package: the installed client (5.14.2) has no
PassingApi. Upgrading it would touch every fetch script in the weekly chain;
calling these endpoints directly touches nothing else.

COVERAGE — this is the part that matters, and it is not uniform:

    2021-2024   zero rows. Not a backfillable metric.
    2025        partial AND lopsided: week 5 carried ONE attempt with air-yard
                data out of 3,175, while weeks 10 and 14 ran at 98%. A 2025
                season aggregate would therefore be a late-season sample wearing
                a full-season label. Store it week-scoped, never roll it up.
    2026 on     ~98% of attempts on games that have been played.

Every aggregate the site derives from this must gate on attempt counts rather
than assume a row's presence means the row is complete.

Usage:  python3 fetch_passing.py                 # active season, all weeks
        python3 fetch_passing.py --week 3        # one week
        python3 fetch_passing.py --season 2025   # a specific season

Budget: one call per (season, week).
"""
import os
import sys

import psycopg2
import requests
from psycopg2.extras import execute_values
from dotenv import load_dotenv

from season_util import current_cfb_season

# No override: a DATABASE_URL already in the environment wins, matching the rest
# of the weekly chain.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

BASE = 'https://api.collegefootballdata.com'
# Air-yard data begins here. Earlier seasons return zero rows, so asking for
# them is a wasted call rather than a backfill.
FIRST_COVERED_SEASON = 2025
MAX_WEEK = 16

DDL = '''
    CREATE TABLE IF NOT EXISTS passing_plays (
        play_id          TEXT PRIMARY KEY,
        game_id          BIGINT,
        drive_id         TEXT,
        season           INTEGER NOT NULL,
        week             INTEGER,
        season_type      TEXT,
        offense          TEXT,
        offense_id       INTEGER,
        defense          TEXT,
        defense_id       INTEGER,
        passer_id        INTEGER,
        passer           TEXT,
        target_id        INTEGER,
        target           TEXT,
        outcome          TEXT,          -- completion | incompletion | interception
        air_yards        INTEGER,       -- negative = thrown behind the LOS
        pass_depth       TEXT,          -- short | deep
        pass_direction   TEXT,          -- left | middle | right
        total_yards      INTEGER,
        yards_after_catch INTEGER,
        down             INTEGER,
        distance         INTEGER,
        yards_to_goal    INTEGER,
        period           INTEGER,
        -- 'partial' means CFBD could not fully parse the play text. Carried
        -- through so a gap in the data is knowable rather than silent.
        parse_status     TEXT,
        updated_at       TIMESTAMPTZ DEFAULT now()
    )
'''

# passer_id / target_id are the CFBD athlete ids, which match players.id exactly
# (verified: every passer, target and game in 2026 week 1 joined). game_id joins
# games.id. So no name reconciliation anywhere.
INDEXES = [
    'CREATE INDEX IF NOT EXISTS idx_passing_season_passer ON passing_plays (season, passer_id)',
    'CREATE INDEX IF NOT EXISTS idx_passing_season_target ON passing_plays (season, target_id)',
    'CREATE INDEX IF NOT EXISTS idx_passing_season_offense ON passing_plays (season, offense)',
    'CREATE INDEX IF NOT EXISTS idx_passing_season_defense ON passing_plays (season, defense)',
    'CREATE INDEX IF NOT EXISTS idx_passing_game ON passing_plays (game_id)',
]


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def fetch_week(key, season, week):
    r = requests.get(f'{BASE}/passing/plays',
                     headers={'Authorization': f'Bearer {key}'},
                     params={'year': season, 'week': week}, timeout=60)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return r.json()


def main():
    key = os.getenv('CFBD_API_KEY')
    if not key:
        print('CFBD_API_KEY not set — cannot fetch passing plays', flush=True)
        return 1

    season = current_cfb_season()
    week = None
    if '--season' in sys.argv:
        season = int(sys.argv[sys.argv.index('--season') + 1])
    if '--week' in sys.argv:
        week = int(sys.argv[sys.argv.index('--week') + 1])

    if season < FIRST_COVERED_SEASON:
        print(f'{season}: before air-yard coverage begins ({FIRST_COVERED_SEASON}) — nothing to fetch',
              flush=True)
        return 0

    conn = psycopg2.connect(os.getenv('DATABASE_URL'))
    try:
        cur = conn.cursor()
        cur.execute(DDL)
        for stmt in INDEXES:
            cur.execute(stmt)
        conn.commit()

        weeks = [week] if week else range(1, MAX_WEEK + 1)
        total = air = yac = 0
        for wk in weeks:
            try:
                plays = fetch_week(key, season, wk)
            except Exception as exc:
                # A single bad week must not lose the ones already stored.
                print(f'  week {wk}: fetch failed ({type(exc).__name__}) — skipped', flush=True)
                continue
            if not plays:
                continue
            # One statement per week rather than per play: a full week is ~7k
            # attempts, and row-by-row inserts ran at ~80/sec, which is minutes
            # of the weekly chain spent on a table this small.
            rows = []
            for p in plays:
                rows.append((
                    str(p.get('playId')), _int(p.get('gameId')), str(p.get('driveId') or ''),
                    season, p.get('week'), p.get('seasonType'),
                    p.get('offense'), _int(p.get('offenseId')),
                    p.get('defense'), _int(p.get('defenseId')),
                    _int(p.get('passerId')), p.get('passer'),
                    _int(p.get('targetId')), p.get('target'), p.get('outcome'),
                    _int(p.get('airYards')), p.get('passDepth'), p.get('passDirection'),
                    _int(p.get('totalYards')), _int(p.get('yardsAfterCatch')),
                    _int(p.get('down')), _int(p.get('distance')),
                    _int(p.get('startYardsToGoal')), _int(p.get('period')),
                    p.get('parseStatus'),
                ))
                total += 1
                if p.get('airYards') is not None:
                    air += 1
                if p.get('yardsAfterCatch') is not None:
                    yac += 1
            execute_values(cur, '''
                INSERT INTO passing_plays
                    (play_id, game_id, drive_id, season, week, season_type,
                     offense, offense_id, defense, defense_id,
                     passer_id, passer, target_id, target, outcome,
                     air_yards, pass_depth, pass_direction, total_yards,
                     yards_after_catch, down, distance, yards_to_goal,
                     period, parse_status)
                VALUES %s
                ON CONFLICT (play_id) DO UPDATE SET
                    outcome = EXCLUDED.outcome,
                    air_yards = EXCLUDED.air_yards,
                    pass_depth = EXCLUDED.pass_depth,
                    pass_direction = EXCLUDED.pass_direction,
                    total_yards = EXCLUDED.total_yards,
                    yards_after_catch = EXCLUDED.yards_after_catch,
                    target_id = EXCLUDED.target_id,
                    target = EXCLUDED.target,
                    parse_status = EXCLUDED.parse_status,
                    updated_at = now()
            ''', rows, page_size=1000)
            conn.commit()

        scope = f'week {week}' if week else 'all weeks'
        pct = f'{air / total * 100:.0f}%' if total else 'n/a'
        print(f'{season} {scope}: {total} pass plays stored '
              f'({air} with air yards = {pct}, {yac} with YAC)', flush=True)
    finally:
        conn.close()

    # Only poke the cache when something landed; an off-season no-op costs nothing.
    if total:
        try:
            from cache_notify import notify_cache_clear
            notify_cache_clear()
        except Exception:
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
