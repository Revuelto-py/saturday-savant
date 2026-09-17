"""Fetch CFBD's enriched rushing data into rushing_player_season / rushing_team_season.

What this adds that the site did not have: what happened to a run before and
after the line, and which way it went. Each rusher and each team carries success
rate, PPA, line yards (credited to the blocking), second-level and open-field
yards (credited to the back), stuff rate, power success and explosiveness —
split left / middle / right.

Why the season aggregates and not /rushing/plays: unlike passing, where only the
play rows carried the location grid, these endpoints already return the
directional split pre-computed, and every surface here is season-level. A season
is ~1,050 rusher rows and 138 team rows against ~30k rush plays, so storing the
aggregates costs a thousandth of the volume and loses nothing the site shows.

COVERAGE — not uniform, and the reason `attempts` is stored on every row:

    2024 and earlier   zero rows. Not a backfillable metric.
    2025               1,622 rushers, but lopsided by game: measured against
                       player_stats carries, the median rusher with 20+ carries
                       has 96% of them enriched and the worst has 3%.
    2026 on            1,055 rushers so far, median 100% enriched.

`attempts` is the count of ENRICHED attempts, and it is the denominator for
every rate in the row — it is not the player's carry total. It can even exceed
his carries (max seen: 114%), because the two sources attribute multi-carrier
and team rushes differently. So anything the site derives from this gates on
`attempts`, and never presents it as "carries".

Usage:  python3 pipeline/fetch_rushing.py                 # active season
        python3 pipeline/fetch_rushing.py --season 2025   # a specific season

Budget: two calls per season (players, teams).
"""

# This script lives one directory below the repo root; ROOT points back at it so
# .env, the model artifacts and the shared modules resolve the same as before.
import os as _os, sys as _sys
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if ROOT not in _sys.path:
    _sys.path.insert(0, ROOT)
import json
import os
import sys

import psycopg2
import requests
from psycopg2.extras import execute_values
from dotenv import load_dotenv

from season_util import current_cfb_season

# No override: a DATABASE_URL already in the environment wins, matching the rest
# of the weekly chain.
load_dotenv(os.path.join(ROOT, '.env'))

BASE = 'https://api.collegefootballdata.com'
# Enriched rushing begins here. 2024 and earlier return zero rows, so asking for
# them is a wasted call rather than a backfill.
FIRST_COVERED_SEASON = 2025
# A season's rows only grow. A response that shrinks the stored set by more than
# a fifth is an upstream fault, not a correction — same guard the team scripts use.
MIN_PAYLOAD_RATIO = 0.80

PLAYER_DDL = '''
    CREATE TABLE IF NOT EXISTS rushing_player_season (
        player_id                INTEGER NOT NULL,
        season                   INTEGER NOT NULL,
        team                     TEXT    NOT NULL,
        player                   TEXT,
        conference               TEXT,
        -- ENRICHED attempts: the denominator for every rate below, and NOT the
        -- player's carry total. See the coverage note in this file's docstring.
        attempts                 INTEGER,
        yards                    INTEGER,
        ypc                      REAL,
        success_rate             REAL,
        ppa                      REAL,
        total_ppa                REAL,
        line_yards               REAL,
        line_yards_total         REAL,
        second_level_yards       REAL,
        second_level_yards_total REAL,
        open_field_yards         REAL,
        open_field_yards_total   REAL,
        stuff_rate               REAL,
        power_success            REAL,
        explosiveness            REAL,
        -- How many of those attempts have a known direction, so a split can say
        -- what it is missing instead of implying the whole season.
        direction_eligible       INTEGER,
        direction_available      INTEGER,
        -- left / middle / right / unknown, each the same metric block. JSONB
        -- rather than 60 flat columns: only the overall values are ever ranked.
        directions               JSONB,
        updated_at               TIMESTAMPTZ DEFAULT now(),
        PRIMARY KEY (player_id, season, team)
    )
'''

TEAM_DDL = '''
    CREATE TABLE IF NOT EXISTS rushing_team_season (
        team                     TEXT    NOT NULL,
        season                   INTEGER NOT NULL,
        -- 'offense' is what the team did running the ball; 'defense' is what it
        -- allowed. The API returns both in one row; they are stored as two.
        side                     TEXT    NOT NULL,
        conference               TEXT,
        attempts                 INTEGER,
        yards                    INTEGER,
        ypc                      REAL,
        success_rate             REAL,
        ppa                      REAL,
        total_ppa                REAL,
        line_yards               REAL,
        line_yards_total         REAL,
        second_level_yards       REAL,
        second_level_yards_total REAL,
        open_field_yards         REAL,
        open_field_yards_total   REAL,
        stuff_rate               REAL,
        power_success            REAL,
        explosiveness            REAL,
        rushing_touchdowns       INTEGER,
        direction_eligible       INTEGER,
        direction_available      INTEGER,
        directions               JSONB,
        updated_at               TIMESTAMPTZ DEFAULT now(),
        PRIMARY KEY (team, season, side)
    )
'''

INDEXES = [
    'CREATE INDEX IF NOT EXISTS idx_rush_player_season ON rushing_player_season (season, attempts DESC)',
    'CREATE INDEX IF NOT EXISTS idx_rush_player_team ON rushing_player_season (season, team)',
    'CREATE INDEX IF NOT EXISTS idx_rush_team_season ON rushing_team_season (season, side)',
]


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _get(key, path, **params):
    r = requests.get(f'{BASE}{path}', headers={'Authorization': f'Bearer {key}'},
                     params=params, timeout=120)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return r.json()


def _metrics(d):
    """The metric block shared by players, teams and every direction."""
    return (
        _int(d.get('attempts') if 'attempts' in d else d.get('carries')),
        _int(d.get('totalRushingYards') if 'totalRushingYards' in d else d.get('yards')),
        _num(d.get('yardsPerCarry')),
        _num(d.get('successRate')), _num(d.get('ppa')), _num(d.get('totalPpa')),
        _num(d.get('lineYards')), _num(d.get('lineYardsTotal')),
        _num(d.get('secondLevelYards')), _num(d.get('secondLevelYardsTotal')),
        _num(d.get('openFieldYards')), _num(d.get('openFieldYardsTotal')),
        _num(d.get('stuffRate')), _num(d.get('powerSuccess')), _num(d.get('explosiveness')),
    )


def _stored(cur, table, season):
    cur.execute(f'SELECT count(*) FROM {table} WHERE season = %s', (season,))
    return cur.fetchone()[0]


def load_players(cur, key, season):
    rows = _get(key, '/rushing/players/season', year=season)
    if not rows:
        print(f'  players: nothing returned for {season}', flush=True)
        return 0
    have = _stored(cur, 'rushing_player_season', season)
    if have and len(rows) < have * MIN_PAYLOAD_RATIO:
        raise RuntimeError(f'players: {len(rows)} rows against {have} stored — refusing to write')

    payload = []
    for r in rows:
        pid = _int(r.get('playerId'))
        if pid is None or not r.get('team'):
            continue
        payload.append((pid, season, r['team'], r.get('player'), r.get('conference'),
                        *_metrics(r),
                        _int(r.get('directionEligibleAttempts')),
                        _int(r.get('directionAvailableAttempts')),
                        json.dumps(r.get('directions') or {})))
    execute_values(cur, '''
        INSERT INTO rushing_player_season
            (player_id, season, team, player, conference,
             attempts, yards, ypc, success_rate, ppa, total_ppa,
             line_yards, line_yards_total, second_level_yards, second_level_yards_total,
             open_field_yards, open_field_yards_total, stuff_rate, power_success,
             explosiveness, direction_eligible, direction_available, directions)
        VALUES %s
        ON CONFLICT (player_id, season, team) DO UPDATE SET
            player = EXCLUDED.player, conference = EXCLUDED.conference,
            attempts = EXCLUDED.attempts, yards = EXCLUDED.yards, ypc = EXCLUDED.ypc,
            success_rate = EXCLUDED.success_rate, ppa = EXCLUDED.ppa,
            total_ppa = EXCLUDED.total_ppa, line_yards = EXCLUDED.line_yards,
            line_yards_total = EXCLUDED.line_yards_total,
            second_level_yards = EXCLUDED.second_level_yards,
            second_level_yards_total = EXCLUDED.second_level_yards_total,
            open_field_yards = EXCLUDED.open_field_yards,
            open_field_yards_total = EXCLUDED.open_field_yards_total,
            stuff_rate = EXCLUDED.stuff_rate, power_success = EXCLUDED.power_success,
            explosiveness = EXCLUDED.explosiveness,
            direction_eligible = EXCLUDED.direction_eligible,
            direction_available = EXCLUDED.direction_available,
            directions = EXCLUDED.directions, updated_at = now()
    ''', payload, page_size=500)
    return len(payload)


def load_teams(cur, key, season):
    rows = _get(key, '/rushing/teams/season', year=season)
    if not rows:
        print(f'  teams: nothing returned for {season}', flush=True)
        return 0
    have = _stored(cur, 'rushing_team_season', season)
    if have and len(rows) * 2 < have * MIN_PAYLOAD_RATIO:
        raise RuntimeError(f'teams: {len(rows)} teams against {have} stored rows — refusing to write')

    payload = []
    for r in rows:
        if not r.get('team'):
            continue
        for side in ('offense', 'defense'):
            d = r.get(side) or {}
            if not d:
                continue
            payload.append((r['team'], season, side, r.get('conference'),
                            *_metrics(d),
                            _int(d.get('rushingTouchdowns')),
                            _int(d.get('directionEligibleAttempts')),
                            _int(d.get('directionAvailableAttempts')),
                            json.dumps(d.get('directions') or {})))
    execute_values(cur, '''
        INSERT INTO rushing_team_season
            (team, season, side, conference,
             attempts, yards, ypc, success_rate, ppa, total_ppa,
             line_yards, line_yards_total, second_level_yards, second_level_yards_total,
             open_field_yards, open_field_yards_total, stuff_rate, power_success,
             explosiveness, rushing_touchdowns, direction_eligible,
             direction_available, directions)
        VALUES %s
        ON CONFLICT (team, season, side) DO UPDATE SET
            conference = EXCLUDED.conference, attempts = EXCLUDED.attempts,
            yards = EXCLUDED.yards, ypc = EXCLUDED.ypc,
            success_rate = EXCLUDED.success_rate, ppa = EXCLUDED.ppa,
            total_ppa = EXCLUDED.total_ppa, line_yards = EXCLUDED.line_yards,
            line_yards_total = EXCLUDED.line_yards_total,
            second_level_yards = EXCLUDED.second_level_yards,
            second_level_yards_total = EXCLUDED.second_level_yards_total,
            open_field_yards = EXCLUDED.open_field_yards,
            open_field_yards_total = EXCLUDED.open_field_yards_total,
            stuff_rate = EXCLUDED.stuff_rate, power_success = EXCLUDED.power_success,
            explosiveness = EXCLUDED.explosiveness,
            rushing_touchdowns = EXCLUDED.rushing_touchdowns,
            direction_eligible = EXCLUDED.direction_eligible,
            direction_available = EXCLUDED.direction_available,
            directions = EXCLUDED.directions, updated_at = now()
    ''', payload, page_size=500)
    return len(payload)


def main():
    key = os.getenv('CFBD_API_KEY')
    if not key:
        print('CFBD_API_KEY not set — cannot fetch rushing data', flush=True)
        return 1

    season = current_cfb_season()
    if '--season' in sys.argv:
        season = int(sys.argv[sys.argv.index('--season') + 1])

    if season < FIRST_COVERED_SEASON:
        print(f'{season}: before enriched rushing coverage begins '
              f'({FIRST_COVERED_SEASON}) — nothing to fetch', flush=True)
        return 0

    conn = psycopg2.connect(os.getenv('DATABASE_URL'))
    players = teams = 0
    try:
        cur = conn.cursor()
        cur.execute(PLAYER_DDL)
        cur.execute(TEAM_DDL)
        for stmt in INDEXES:
            cur.execute(stmt)
        conn.commit()

        players = load_players(cur, key, season)
        teams = load_teams(cur, key, season)
        conn.commit()
        print(f'{season}: rushing — {players} rusher rows, {teams} team rows', flush=True)
    finally:
        conn.close()

    # Only poke the cache when something landed; an off-season no-op costs nothing.
    if players or teams:
        try:
            from cache_notify import notify_cache_clear
            notify_cache_clear()
        except Exception:
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
