"""One-time correction: re-fetch player_stats with season_type='both' so bowl/
CFP production counts toward season totals.

CFBD's season-stats endpoint defaults to regular-season only, and the ingest
scripts fetched 'regular' — so postseason production was dropped (e.g. David
Bailey showed 13.5 sacks instead of a nation-leading 14.5; Trinidad Chambliss
3016 passing yards instead of 3937). The fetch scripts now use 'both'; this
corrects the seasons already loaded.

Safety: for each season it FETCHES first and refuses to touch the table unless
the fetch looks complete (>1000 rows), then does a season-scoped
DELETE-then-INSERT in one transaction (never an unscoped delete). It also drops
that season's cached percentile/returning pools so they recompute from the new
totals, and clears the live page cache at the end.

Usage:  python3 refetch_player_stats.py 2025
        python3 refetch_player_stats.py 2016 2017 2018 2019 2020 2021 2022 2023 2024 2025
"""
import os
import sys

import cfbd
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=True)

years = []
for a in sys.argv[1:]:
    try:
        years.append(int(a))
    except ValueError:
        pass
if not years:
    print("usage: refetch_player_stats.py <season> [season ...]")
    sys.exit(1)

cfg = cfbd.Configuration(access_token=os.getenv('CFBD_API_KEY'))
conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cur = conn.cursor()

with cfbd.ApiClient(cfg) as api:
    stats_api = cfbd.StatsApi(api)
    for y in years:
        stats = stats_api.get_player_season_stats(year=y, season_type='both')
        if len(stats) < 1000:
            print(f"{y}: fetch returned only {len(stats)} rows — REFUSING "
                  f"(won't wipe a season on a suspect fetch)")
            continue
        cur.execute('SELECT count(*) FROM player_stats WHERE season = %s', (y,))
        before = cur.fetchone()[0]
        cur.execute('DELETE FROM player_stats WHERE season = %s', (y,))
        execute_values(cur, '''
            INSERT INTO player_stats (player_id, player_name, team, conference, position,
                                      category, stat_type, stat, season)
            VALUES %s
        ''', [(s.player_id, s.player, s.team, s.conference, s.position,
               s.category, s.stat_type, s.stat, y) for s in stats], page_size=2000)
        # Invalidate this season's cached pools (percentile peer pools, returning
        # production) so player pages recompute from the corrected totals.
        cur.execute("DELETE FROM pool_store WHERE key LIKE %s", (f'%:{y}',))
        conn.commit()
        print(f"{y}: player_stats {before} -> {len(stats)} rows (regular+postseason); pools invalidated")

conn.close()

try:
    from cache_notify import notify_cache_clear
    notify_cache_clear()
    print("live page cache cleared")
except Exception as e:
    print(f"cache clear skipped: {e}")
