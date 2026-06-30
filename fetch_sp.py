import cfbd
import psycopg2
import os
from dotenv import load_dotenv

load_dotenv()

configuration = cfbd.Configuration(access_token=os.getenv("CFBD_API_KEY"))
conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cursor = conn.cursor()

with cfbd.ApiClient(configuration) as api_client:
    ratings_api = cfbd.RatingsApi(api_client)
    sp = ratings_api.get_sp(year=2025)

cursor.execute('DELETE FROM sp_ratings')
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

conn.commit()
conn.close()
print(f"Saved {saved} SP+ ratings")

conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cursor = conn.cursor()
cursor.execute("SELECT * FROM sp_ratings WHERE team IN ('Penn State','Alabama','Georgia')")
for r in cursor.fetchall():
    print(r)
conn.close()
