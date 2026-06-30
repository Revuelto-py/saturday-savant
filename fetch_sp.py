import cfbd
import sqlite3
import os
from dotenv import load_dotenv

load_dotenv()

configuration = cfbd.Configuration(access_token=os.getenv("CFBD_API_KEY"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
conn = sqlite3.connect(os.path.join(BASE_DIR, 'cfb_data.db'))
cursor = conn.cursor()

cursor.execute('''
    CREATE TABLE IF NOT EXISTS sp_ratings (
        team TEXT PRIMARY KEY,
        rating REAL,
        ranking INTEGER,
        offense_rating REAL,
        offense_ranking INTEGER,
        defense_rating REAL,
        defense_ranking INTEGER,
        special_teams_rating REAL
    )
''')

with cfbd.ApiClient(configuration) as api_client:
    ratings_api = cfbd.RatingsApi(api_client)
    sp = ratings_api.get_sp(year=2025)    
    
saved = 0
for s in sp:
    try:
        off = getattr(s, 'offense', None)
        def_ = getattr(s, 'defense', None)
        st = getattr(s, 'special_teams', None)
        cursor.execute('''
            INSERT OR REPLACE INTO sp_ratings VALUES (?,?,?,?,?,?,?,?)
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
    except Exception as e:
        print(f"Error {s.team}: {e}")

conn.commit()
conn.close()
print(f"Saved {saved} SP+ ratings")

conn = sqlite3.connect(os.path.join(BASE_DIR, 'cfb_data.db'))
cursor = conn.cursor()
cursor.execute("SELECT * FROM sp_ratings WHERE team IN ('Penn State','Alabama','Georgia')")
for r in cursor.fetchall():
    print(r)
conn.close()