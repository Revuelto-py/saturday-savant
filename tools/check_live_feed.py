"""Self-check for lib/live_feed.py against a real game: ND at BYU, 2026 week 7.

Reads that game's stored ESPN summary, so it needs the database (.env):
python3 tools/check_live_feed.py
"""
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
import psycopg2  # noqa: E402
from lib import live_feed  # noqa: E402

conn = psycopg2.connect(os.environ['DATABASE_URL'])
cur = conn.cursor()
cur.execute('SELECT summary_gz FROM game_summaries WHERE game_id = 401856809')
d = json.loads(gzip.decompress(bytes(cur.fetchone()[0])))
cards = live_feed.key_plays(d, {'away': 'ND', 'home': 'BYU'})
by_head = {c['head']: c for c in cards}

walton = by_head['3-yd TD run, ND takes the lead']
assert walton['name'] == 'J. Walton' and walton['outcome'] == ('2-pt try is good', 'g') and walton['side'] == 'away'
pick = by_head['Interception']
assert pick['name'] == 'J. Sanders' and pick['line'] == '1 int' and pick['side'] == 'away', pick
assert pick['line2'].startswith('B. Bachmeier · ') and pick['gainer'] == 'away'
muff = next(c for c in cards if c['head'].startswith('Punt fumbled, ND recovers'))
assert muff['name'] == 'C. DeVries' and muff['side'] == 'home' and muff['outcome'] == ('Fumble lost', 'r')
assert by_head['39-yd field goal']['outcome'] == ('Field goal is good', 'g')
assert not any(h.startswith(('Timeout', 'End')) for h in by_head), 'timeouts and period ends are not plays'
assert [c['when'][:2] for c in cards][0] == 'Q4' and cards[-1]['when'].startswith('Q1'), 'newest first'
assert all(c['swing'] is None or c['gainer'] in ('home', 'away') for c in cards)
print(f'live feed: ok ({len(cards)} key plays)')
