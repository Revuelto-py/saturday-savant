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

Usage:  python3 fetch_rankings.py             # active season, all weeks
        python3 fetch_rankings.py 2016 2025   # backfill a season range
"""
import os
import sys

import cfbd
import psycopg2
from dotenv import load_dotenv

from season_util import current_cfb_season

load_dotenv()

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


with cfbd.ApiClient(configuration) as api_client:
    rankings_api = cfbd.RankingsApi(api_client)
    for season in SEASONS:
        rankings = rankings_api.get_rankings(year=season)
        polls = ap_polls(rankings)
        if not polls:
            print(f"{season}: no AP poll data", flush=True)
            continue

        cursor.execute('DELETE FROM ap_rankings WHERE season = %s', (season,))
        prev_map = {}          # team -> rank in the previous poll
        total = 0
        for week, stype, ranks in polls:
            for r in ranks:
                cursor.execute('''
                    INSERT INTO ap_rankings
                        (team, rank, points, first_place_votes, week, season, prev_rank, season_type)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ''', (r.school, r.rank, getattr(r, 'points', None),
                      getattr(r, 'first_place_votes', None), week, season,
                      prev_map.get(r.school), stype))
                total += 1
            prev_map = {r.school: r.rank for r in ranks}
        conn.commit()
        print(f"{season}: {len(polls)} polls, {total} rows "
              f"(final: {polls[-1][1]} week {polls[-1][0]})", flush=True)

conn.close()
print("AP rankings fetch complete")


# Data changed — tell the live site to drop its in-memory page cache so the
# update is visible immediately instead of after the cache TTL.
try:
    from cache_notify import notify_cache_clear
    notify_cache_clear()
except Exception:
    pass
