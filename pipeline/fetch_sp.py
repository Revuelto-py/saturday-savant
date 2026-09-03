# This script lives one directory below the repo root; ROOT points back at it so
# .env, the model artifacts and the shared modules resolve the same as before.
import os as _os, sys as _sys
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if ROOT not in _sys.path:
    _sys.path.insert(0, ROOT)
import cfbd
import psycopg2
import os
from dotenv import load_dotenv
from season_util import current_cfb_season

load_dotenv(_os.path.join(ROOT, '.env'))

SEASON = current_cfb_season()

configuration = cfbd.Configuration(access_token=os.getenv("CFBD_API_KEY"))
conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cursor = conn.cursor()

with cfbd.ApiClient(configuration) as api_client:
    ratings_api = cfbd.RatingsApi(api_client)
    sp = ratings_api.get_sp(year=SEASON)

# Multi-season table — only refresh the active season so prior years (loaded by
# backfill/backfill_history.py) survive.
cursor.execute('DELETE FROM sp_ratings WHERE season = %s', (SEASON,))
saved = 0
for s in sp:
    off = getattr(s, 'offense', None)
    def_ = getattr(s, 'defense', None)
    st = getattr(s, 'special_teams', None)
    cursor.execute('''
        INSERT INTO sp_ratings VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ''', (
        s.team,
        getattr(s, 'rating', None),
        getattr(s, 'ranking', None),
        getattr(off, 'rating', None) if off else None,
        getattr(off, 'ranking', None) if off else None,
        getattr(def_, 'rating', None) if def_ else None,
        getattr(def_, 'ranking', None) if def_ else None,
        getattr(st, 'rating', None) if st else None,
    ))
    saved += 1

# Positional INSERT skips the trailing `season` column — tag new rows.
cursor.execute('UPDATE sp_ratings SET season = %s WHERE season IS NULL', (SEASON,))
conn.commit()
conn.close()
print(f"Saved {saved} SP+ ratings")

conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cursor = conn.cursor()
cursor.execute("SELECT * FROM sp_ratings WHERE team IN ('Penn State','Alabama','Georgia')")
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
