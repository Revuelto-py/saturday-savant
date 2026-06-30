import cfbd
import psycopg2
import os
from dotenv import load_dotenv

load_dotenv()

configuration = cfbd.Configuration(access_token=os.getenv("CFBD_API_KEY"))
conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cursor = conn.cursor()

try:
    cursor.execute('ALTER TABLE ap_rankings ADD COLUMN prev_rank INTEGER')
    conn.commit()
except Exception:
    conn.rollback()

with cfbd.ApiClient(configuration) as api_client:
    rankings_api = cfbd.RankingsApi(api_client)
    rankings = rankings_api.get_rankings(year=2025)

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

    cursor.execute('DELETE FROM ap_rankings')
    for r in cur_ranks:
        prev = prev_rank_map.get(r.school)
        cursor.execute('''
            INSERT INTO ap_rankings
            (team, rank, points, first_place_votes, week, season, prev_rank)
            VALUES (%s, %s, %s, %s, %s, 2025, %s)
        ''', (r.school, r.rank, getattr(r, 'points', None),
              getattr(r, 'first_place_votes', None), cur_week, prev))

    conn.commit()
    print(f"Saved {len(cur_ranks)} teams")

    cursor.execute('SELECT rank, team, points, prev_rank FROM ap_rankings ORDER BY rank LIMIT 5')
    for r in cursor.fetchall():
        print(r)

conn.close()
