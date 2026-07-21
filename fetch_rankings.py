import cfbd
import psycopg2
import os
from dotenv import load_dotenv
from season_util import current_cfb_season

load_dotenv()

SEASON = current_cfb_season()

configuration = cfbd.Configuration(access_token=os.getenv("CFBD_API_KEY"))
conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cursor = conn.cursor()

try:
    cursor.execute('ALTER TABLE ap_rankings ADD COLUMN prev_rank INTEGER')
    conn.commit()
except Exception:
    conn.rollback()

try:
    cursor.execute('ALTER TABLE ap_rankings ADD COLUMN season_type TEXT')
    conn.commit()
except Exception:
    conn.rollback()

with cfbd.ApiClient(configuration) as api_client:
    rankings_api = cfbd.RankingsApi(api_client)
    rankings = rankings_api.get_rankings(year=SEASON)

# Collect all AP poll weeks
ap_weeks = []
for week_data in rankings:
    for poll in week_data.polls:
        if poll.poll == 'AP Top 25':
            stype = str(week_data.season_type) if week_data.season_type else ''
            sort_val = (0 if 'post' in stype.lower() else 1, -week_data.week)
            ap_weeks.append((sort_val, week_data.week, stype, poll.ranks))

ap_weeks.sort(key=lambda x: x[0])

if not ap_weeks:
    print("No AP poll data found")
else:
    # Most recent week
    _, cur_week, cur_type, cur_ranks = ap_weeks[0]
    print(f"Current week: {cur_week} ({cur_type})")

    # Previous week
    prev_rank_map = {}
    if len(ap_weeks) > 1:
        _, prev_week, prev_type, prev_ranks = ap_weeks[1]
        print(f"Previous week: {prev_week} ({prev_type})")
        for r in prev_ranks:
            prev_rank_map[r.school] = r.rank

    season_type = 'postseason' if 'post' in cur_type.lower() else 'regular'

    # Multi-season table — only refresh the active season so prior years' final
    # polls (loaded by backfill_history.py) survive.
    cursor.execute('DELETE FROM ap_rankings WHERE season = %s', (SEASON,))
    for r in cur_ranks:
        prev = prev_rank_map.get(r.school)
        cursor.execute('''
            INSERT INTO ap_rankings
            (team, rank, points, first_place_votes, week, season, prev_rank, season_type)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ''', (r.school, r.rank, getattr(r, 'points', None),
              getattr(r, 'first_place_votes', None), cur_week, SEASON, prev, season_type))

    conn.commit()
    print(f"Saved {len(cur_ranks)} teams")

    cursor.execute('SELECT rank, team, points, prev_rank FROM ap_rankings ORDER BY rank LIMIT 5')
    for r in cursor.fetchall():
        print(r)

conn.close()


# Data changed — tell the live site to drop its in-memory page cache so the
# update is visible immediately instead of after the cache TTL.
try:
    from cache_notify import notify_cache_clear
    notify_cache_clear()
except Exception:
    pass
