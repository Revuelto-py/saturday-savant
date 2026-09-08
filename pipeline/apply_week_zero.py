"""Relabel Week 0 games, which no upstream source labels for us.

College football opens with a handful of games the Saturday before the real
opening weekend. CFBD returns them as week 1 — and ignores a `week=0` argument
entirely — so every season in this database arrived with those games folded into
week 1. A team that played both showed two "WK 1" rows on its schedule, and the
same duplication ran through every player's game log.

This derives Week 0 from the calendar (see `season_util.week_zero_dates`) and
writes it onto `games` plus every table that carries its own copy of the week.
`player_game_logs` stores a JSON array per player-season, so its `week` and
`game_label` fields are rewritten in place for the affected games.

Idempotent by construction: it computes the target set, updates only rows that
disagree, and skips the site's cache clear entirely when nothing moved. Safe to
run on every pipeline pass, which is the point — a new season's Week 0 gets
labelled the first time the chain runs after those games are ingested.

Usage:  python3 pipeline/apply_week_zero.py             # every loaded season
        python3 pipeline/apply_week_zero.py 2026        # one season
        python3 pipeline/apply_week_zero.py 2016 2026   # a range
        python3 pipeline/apply_week_zero.py --dry-run   # report, change nothing
"""

import os as _os, sys as _sys
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if ROOT not in _sys.path:
    _sys.path.insert(0, ROOT)

import json
import os
import sys

import psycopg2
from dotenv import load_dotenv

from season_util import week_zero_dates

load_dotenv(_os.path.join(ROOT, '.env'))

DRY = '--dry-run' in sys.argv
args = [a for a in sys.argv[1:] if not a.startswith('-')]

# Tables that keep their own `week` alongside a `game_id`. `ap_rankings` is
# deliberately absent: its `week` is a POLL week, not a game week, and the
# preseason poll is week 1 by AP's own numbering.
SATELLITES = ('betting_lines', 'game_predictions', 'game_weather', 'passing_plays')

conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cursor = conn.cursor()

if args:
    SEASONS = range(int(args[0]), int(args[-1]) + 1)
else:
    cursor.execute('SELECT DISTINCT season FROM games WHERE season IS NOT NULL ORDER BY season')
    SEASONS = [r[0] for r in cursor.fetchall()]


def week_zero_game_ids(season):
    """Ids of this season's Week 0 games, derived from kickoff dates.

    Reads weeks 0 AND 1 so a season already relabelled still resolves to the
    same set — that is what makes re-running a no-op rather than a slow drift.
    """
    cursor.execute("""
        SELECT id, (start_date::timestamptz AT TIME ZONE 'America/New_York')::date
          FROM games
         WHERE season = %s AND season_type ILIKE %s AND week IN (0, 1)
           AND start_date IS NOT NULL
    """, (season, '%regular%'))
    rows = cursor.fetchall()
    if not rows:
        return set(), 0
    zero_days = week_zero_dates(d for _, d in rows)
    return {gid for gid, d in rows if d in zero_days}, len(rows)


def fix_game_logs(season, ids):
    """Rewrite `week` and `game_label` inside player_game_logs for these games.

    The column is TEXT holding a JSON array, so there is no server-side way to
    do this. A LIKE prefilter on the game ids keeps it to the handful of players
    who actually appeared in a Week 0 game instead of parsing the whole season
    (~15k rows, several seconds).
    """
    if not ids:
        return 0
    like = ' OR '.join(['log LIKE %s'] * len(ids))
    cursor.execute(f'SELECT player_id, season, log FROM player_game_logs '
                   f'WHERE season = %s AND ({like})',
                   [season] + [f'%{gid}%' for gid in ids])
    changed = 0
    for player_id, szn, raw in cursor.fetchall():
        try:
            log = json.loads(raw)
        except Exception:
            continue
        touched = False
        for entry in log:
            if entry.get('game_id') in ids and entry.get('week') != 0:
                entry['week'] = 0
                # game_label is the bare week number for regular-season rows;
                # postseason rows carry a bowl/round name and must be left be.
                if str(entry.get('game_label', '')).isdigit():
                    entry['game_label'] = '0'
                touched = True
        if touched:
            changed += 1
            if not DRY:
                cursor.execute('UPDATE player_game_logs SET log = %s '
                               'WHERE player_id = %s AND season = %s',
                               (json.dumps(log), player_id, szn))
    return changed


total_moved = 0
for season in SEASONS:
    ids, considered = week_zero_game_ids(season)
    if not ids:
        print(f'{season}: no week 0 ({considered} opening games considered)', flush=True)
        continue

    cursor.execute('SELECT count(*) FROM games WHERE id = ANY(%s) AND week <> 0', (list(ids),))
    stale_games = cursor.fetchone()[0]

    per_table = {}
    for tbl in SATELLITES:
        try:
            cursor.execute(f'SELECT count(*) FROM {tbl} WHERE game_id = ANY(%s) AND week <> 0',
                           (list(ids),))
            per_table[tbl] = cursor.fetchone()[0]
        except Exception:
            conn.rollback()          # table absent on a fresh DB
            per_table[tbl] = 0

    logs = fix_game_logs(season, ids)

    if not (stale_games or any(per_table.values()) or logs):
        print(f'{season}: {len(ids)} week-0 game(s), already labelled', flush=True)
        continue

    if not DRY:
        cursor.execute('UPDATE games SET week = 0 WHERE id = ANY(%s) AND week <> 0', (list(ids),))
        for tbl in SATELLITES:
            if per_table[tbl]:
                try:
                    cursor.execute(f'UPDATE {tbl} SET week = 0 '
                                   f'WHERE game_id = ANY(%s) AND week <> 0', (list(ids),))
                except Exception:
                    conn.rollback()
        conn.commit()

    total_moved += stale_games
    sats = ', '.join(f'{t}={n}' for t, n in per_table.items() if n) or 'none'
    print(f'{season}: {len(ids)} week-0 game(s) — games={stale_games}, '
          f'{sats}, player logs={logs}{" [dry run]" if DRY else ""}', flush=True)

conn.close()
print('week 0 pass complete')

if total_moved and not DRY:
    try:
        from cache_notify import notify_cache_clear
        notify_cache_clear()
        print('cache cleared', flush=True)
    except Exception:
        pass
