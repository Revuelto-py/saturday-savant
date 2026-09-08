"""Fetch AP Top 25 polls into ap_rankings — EVERY weekly poll, not just the final.

CFBD's get_rankings(year) returns one entry per ranking week: the regular-season
polls (week 1 = preseason, then weekly) plus the postseason final. We store all
of them so the site can show the poll as-of any week (rankings page week
selector) and each game's teams at their rank when they played.

prev_rank is each team's rank in the immediately preceding poll (chronological:
regular weeks ascending, then the postseason final), so the rankings page can
show week-over-week movement.

Multi-season table: each run refreshes only the seasons it fetches (DELETE that
season then insert), so other years survive.

Cheap to re-run. The rows CFBD returns are compared against what is stored and
a season is only rewritten when something actually differs, so a run that finds
nothing new touches no rows and does NOT clear the site's page cache. That is
what makes it safe on an hourly schedule: the AP poll's release day moves around
(Sunday most weeks, Tuesday when week 1 runs through Labor Day, January for the
final), so polling often beats trying to guess the day.

Usage:  python3 pipeline/fetch_rankings.py             # active season, all weeks
        python3 pipeline/fetch_rankings.py 2016 2025   # backfill a season range
"""

# This script lives one directory below the repo root; ROOT points back at it so
# .env, the model artifacts and the shared modules resolve the same as before.
import os as _os, sys as _sys
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if ROOT not in _sys.path:
    _sys.path.insert(0, ROOT)
import os
import sys

import cfbd
import psycopg2
from dotenv import load_dotenv

from season_util import current_cfb_season

load_dotenv(_os.path.join(ROOT, '.env'))

if len(sys.argv) >= 3:
    SEASONS = range(int(sys.argv[1]), int(sys.argv[2]) + 1)
elif len(sys.argv) == 2:
    SEASONS = [int(sys.argv[1])]
else:
    SEASONS = [current_cfb_season()]

configuration = cfbd.Configuration(access_token=os.getenv("CFBD_API_KEY"))
conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cursor = conn.cursor()

for col in ('prev_rank INTEGER', 'season_type TEXT'):
    try:
        cursor.execute(f'ALTER TABLE ap_rankings ADD COLUMN {col.split()[0]} {col.split()[1]}')
        conn.commit()
    except Exception:
        conn.rollback()
# One poll row per team, keyed so a re-run can't duplicate.
cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_ap_rankings '
               'ON ap_rankings (season, season_type, week, team)')
conn.commit()


def ap_polls(rankings):
    """All AP Top 25 weeks for one season, chronological (regular ascending,
    then the postseason final): [(week, 'regular'|'postseason', ranks)]."""
    out = []
    for wd in rankings:
        stype = 'postseason' if 'post' in str(wd.season_type or '').lower() else 'regular'
        for poll in wd.polls:
            if poll.poll == 'AP Top 25':
                out.append((wd.week, stype, poll.ranks))
    out.sort(key=lambda x: (0 if x[1] == 'regular' else 1, x[0]))
    return out


def poll_rows(season, polls):
    """Every row this season should hold, in insert order. prev_rank is the
    team's rank in the immediately preceding poll, so it is built by walking the
    polls chronologically."""
    rows = []
    prev_map = {}              # team -> rank in the previous poll
    for week, stype, ranks in polls:
        for r in ranks:
            rows.append((r.school, r.rank, getattr(r, 'points', None),
                         getattr(r, 'first_place_votes', None), week, season,
                         prev_map.get(r.school), stype))
        prev_map = {r.school: r.rank for r in ranks}
    return rows


def stored_rows(cursor, season):
    cursor.execute('''SELECT team, rank, points, first_place_votes, week, season,
                             prev_rank, season_type
                        FROM ap_rankings WHERE season = %s''', (season,))
    return cursor.fetchall()


changed_seasons = []

with cfbd.ApiClient(configuration) as api_client:
    rankings_api = cfbd.RankingsApi(api_client)
    for season in SEASONS:
        rankings = rankings_api.get_rankings(year=season)
        polls = ap_polls(rankings)
        if not polls:
            # Never delete on an empty response — a CFBD hiccup in the preseason
            # must leave last week's poll standing rather than blank the page.
            print(f"{season}: no AP poll data", flush=True)
            continue

        wanted = poll_rows(season, polls)
        if set(wanted) == set(stored_rows(cursor, season)):
            print(f"{season}: unchanged ({len(polls)} polls, "
                  f"latest {polls[-1][1]} week {polls[-1][0]}) — no write", flush=True)
            continue

        cursor.execute('DELETE FROM ap_rankings WHERE season = %s', (season,))
        cursor.executemany('''
            INSERT INTO ap_rankings
                (team, rank, points, first_place_votes, week, season, prev_rank, season_type)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ''', wanted)
        conn.commit()
        changed_seasons.append(season)
        print(f"{season}: UPDATED — {len(polls)} polls, {len(wanted)} rows "
              f"(latest: {polls[-1][1]} week {polls[-1][0]})", flush=True)

conn.close()
print("AP rankings fetch complete")


# Only when something actually moved. An hourly run that finds no new poll must
# not clear the page cache — that would keep every page permanently cold for the
# six days a week when the poll doesn't change.
if changed_seasons:
    try:
        from cache_notify import notify_cache_clear
        notify_cache_clear()
        print(f"cache cleared for {changed_seasons}", flush=True)
    except Exception:
        pass
