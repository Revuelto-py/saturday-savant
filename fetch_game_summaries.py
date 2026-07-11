"""Fetch and store ESPN game summaries for completed games.

The /game/<id> page renders box scores, drives, win probability, and
play-by-play from ESPN's summary API. Completed games never change, so
each summary is fetched once and stored gzip-compressed in Postgres —
pages then serve from the database with no ESPN dependency at request
time. Re-running this script only fetches games missing a summary, so
it can run after each week's games are loaded.
"""
import gzip
import json
import os
from concurrent.futures import ThreadPoolExecutor

import psycopg2
import requests
from dotenv import load_dotenv

load_dotenv()
conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cursor = conn.cursor()

cursor.execute('''
    CREATE TABLE IF NOT EXISTS game_summaries (
        game_id BIGINT PRIMARY KEY,
        summary_gz BYTEA NOT NULL,
        fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
''')
conn.commit()

cursor.execute('''
    SELECT g.id FROM games g
    LEFT JOIN game_summaries s ON s.game_id = g.id
    WHERE g.completed = 1 AND s.game_id IS NULL
''')
todo = [r[0] for r in cursor.fetchall()]
print(f"{len(todo)} completed games missing summaries")

# ESPN keys the site never reads — betting, editorial, and video content
# make up ~30% of the payload
CRUFT = ['news', 'article', 'videos', 'standings', 'pickcenter',
         'againstTheSpread', 'odds', 'predictor', 'shop', 'ads', 'meta', 'format']

session = requests.Session()

def fetch(gid):
    try:
        r = session.get(
            'https://site.api.espn.com/apis/site/v2/sports/football/college-football/summary',
            params={'event': gid}, timeout=10)
        if r.status_code != 200:
            return gid, None, f'HTTP {r.status_code}'
        data = r.json()
        if not data.get('header', {}).get('competitions'):
            return gid, None, 'no header in response'
        for k in CRUFT:
            data.pop(k, None)
        blob = gzip.compress(json.dumps(data, separators=(',', ':')).encode(), 6)
        return gid, blob, None
    except Exception as e:
        return gid, None, type(e).__name__

ok = failed = 0
with ThreadPoolExecutor(max_workers=8) as ex:
    for gid, blob, err in ex.map(fetch, todo):
        if blob is None:
            print(f"  FAILED {gid}: {err}")
            failed += 1
            continue
        cursor.execute(
            'INSERT INTO game_summaries (game_id, summary_gz) VALUES (%s, %s) ON CONFLICT (game_id) DO NOTHING',
            (gid, blob))
        ok += 1
        if ok % 100 == 0:
            conn.commit()
            print(f"  {ok}/{len(todo)} stored")
conn.commit()

print(f"stored {ok}, failed {failed}")
cursor.execute("SELECT count(*), pg_size_pretty(pg_total_relation_size('game_summaries')) FROM game_summaries")
count, size = cursor.fetchone()
print(f"game_summaries: {count} rows, {size}")
conn.close()


# Data changed — tell the live site to drop its in-memory page cache so the
# update is visible immediately instead of after the cache TTL.
try:
    from cache_notify import notify_cache_clear
    notify_cache_clear()
except Exception:
    pass
