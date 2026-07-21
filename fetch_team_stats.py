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

with cfbd.ApiClient(configuration) as api_client:
    stats_api = cfbd.StatsApi(api_client)
    advanced = stats_api.get_advanced_season_stats(year=SEASON, exclude_garbage_time=True)

# Multi-season table — only refresh the active season so prior years (loaded by
# backfill_history.py) survive.
cursor.execute('DELETE FROM team_stats WHERE season = %s', (SEASON,))
saved = 0
for s in advanced:
    try:
        o = s.offense
        d = s.defense
        cursor.execute('''
            INSERT INTO team_stats VALUES (
                %s,
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
            )
        ''', (
            s.team,
            getattr(o, 'plays', None), getattr(o, 'drives', None),
            getattr(o, 'ppa', None), getattr(o, 'total_ppa', None),
            getattr(o, 'success_rate', None), getattr(o, 'explosiveness', None),
            getattr(o, 'power_success', None), getattr(o, 'stuff_rate', None),
            getattr(o, 'line_yards', None), getattr(o, 'open_field_yards', None),
            getattr(o, 'second_level_yards', None),
            getattr(o, 'rushing_plays', None) and getattr(o.rushing_plays, 'ppa', None),
            getattr(o, 'passing_plays', None) and getattr(o.passing_plays, 'ppa', None),
            getattr(o, 'rushing_plays', None) and getattr(o.rushing_plays, 'success_rate', None),
            getattr(o, 'passing_plays', None) and getattr(o.passing_plays, 'success_rate', None),
            getattr(o, 'rushing_plays', None) and getattr(o.rushing_plays, 'explosiveness', None),
            getattr(o, 'passing_plays', None) and getattr(o.passing_plays, 'explosiveness', None),

            getattr(d, 'plays', None), getattr(d, 'drives', None),
            getattr(d, 'ppa', None), getattr(d, 'total_ppa', None),
            getattr(d, 'success_rate', None), getattr(d, 'explosiveness', None),
            getattr(d, 'power_success', None), getattr(d, 'stuff_rate', None),
            getattr(d, 'line_yards', None), getattr(d, 'open_field_yards', None),
            getattr(d, 'second_level_yards', None),
            getattr(d, 'rushing_plays', None) and getattr(d.rushing_plays, 'ppa', None),
            getattr(d, 'passing_plays', None) and getattr(d.passing_plays, 'ppa', None),
            getattr(d, 'rushing_plays', None) and getattr(d.rushing_plays, 'success_rate', None),
            getattr(d, 'passing_plays', None) and getattr(d.passing_plays, 'success_rate', None),
            getattr(d, 'rushing_plays', None) and getattr(d.rushing_plays, 'explosiveness', None),
            getattr(d, 'passing_plays', None) and getattr(d.passing_plays, 'explosiveness', None),
        ))
        saved += 1
    except Exception as e:
        conn.rollback()
        print(f"Error on {s.team}: {e}")

# The positional INSERT above fills every column except the trailing `season`
# (added later by migrate_seasons.py), so tag the freshly-inserted rows.
cursor.execute('UPDATE team_stats SET season = %s WHERE season IS NULL', (SEASON,))
conn.commit()
conn.close()
print(f"Saved {saved} teams")

# Verify
conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cursor = conn.cursor()
cursor.execute("SELECT team, off_ppa, def_ppa, off_success_rate, def_success_rate FROM team_stats WHERE team IN ('Penn State','Alabama','Georgia')")
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
