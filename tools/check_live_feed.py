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

# The hero's field, replayed as if live after every play of the same game: the
# ball is always on the field with a side and a label, and the scores land.
drives = d['drives']['previous']
seen = {'td': set(), 'fg': set()}
for di, dr in enumerate(drives):
    for k in range(1, len(dr.get('plays') or []) + 1):
        dk = dict(d, drives={'previous': drives[:di], 'current': dict(dr, plays=dr['plays'][:k])})
        f = live_feed.field(dk, {'away': 'ND', 'home': 'BYU'})
        if f is None:
            continue
        assert f['poss'] in ('away', 'home') and 0 <= f['toGo'] <= 100 and f['label'], f
        assert f['driveToGo'] is None or 0 <= f['driveToGo'] <= 100, f
        if f['score'] in seen:
            seen[f['score']].add((f['poss'], f['toGo']))
assert ('away', 0) in seen['td'], 'Walton TD puts the ball in the end zone ND attacks'
assert seen['fg'] and all(t > 0 for _, t in seen['fg']), 'a field goal stays where it was kicked from'
print(f"live field: ok ({len(seen['td'])} touchdown spots, {len(seen['fg'])} field goals)")

# ESPN has sent yardsToEndzone 32 beside "SC 32" for South Carolina (home) at its
# own 32; the field reads the printed spot, so the ball lands 68 yards out.
glitch = {'header': {'competitions': [{'status': {'type': {'name': 'STATUS_IN_PROGRESS'}, 'period': 2},
          'competitors': [{'homeAway': 'home', 'team': {'id': '2579', 'abbreviation': 'SC'}},
                          {'homeAway': 'away', 'team': {'id': '245', 'abbreviation': 'TA&M'}}]}]},
          'drives': {'current': {'team': {'id': '2579'}, 'plays': [
              {'id': '1', 'type': {'text': 'Rush'}, 'start': {'team': {'id': '2579'}, 'possessionText': 'SC 37', 'yardsToEndzone': 63},
               'end': {'team': {'id': '2579'}, 'possessionText': 'SC 32', 'yardsToEndzone': 32, 'down': 2, 'distance': 15,
                       'downDistanceText': '2nd & 15 at SC 32'}}]}}}
g = live_feed.field(glitch, {'home': 'SC', 'away': 'TA&M'})
assert g['poss'] == 'home' and g['toGo'] == 68 and g['driveToGo'] == 63 and g['label'] == '2nd & 15 · SC 32', g
print('live field: ESPN spot glitch ok')

# A punt hands Notre Dame the ball, but ESPN kept Miami's down: "4th & Goal at ND 6".
# A change of possession is a first down by rule.
punt = {'header': {'competitions': [{'status': {'type': {'name': 'STATUS_IN_PROGRESS'}, 'period': 1},
        'competitors': [{'homeAway': 'home', 'team': {'id': '87', 'abbreviation': 'ND'}},
                        {'homeAway': 'away', 'team': {'id': '2390', 'abbreviation': 'MIA'}}]}]},
        'drives': {'current': {'team': {'id': '2390'}, 'plays': [
            {'id': '9', 'type': {'text': 'Punt Return'}, 'start': {'team': {'id': '2390'}, 'possessionText': 'MIA 44'},
             'end': {'team': {'id': '87'}, 'possessionText': 'ND 6', 'yardsToEndzone': 94, 'down': 4, 'distance': 6,
                     'downDistanceText': '4th & Goal at ND 6', 'shortDownDistanceText': '4th & Goal'}}]}}}
g = live_feed.field(punt, {'home': 'ND', 'away': 'MIA'})
assert g['poss'] == 'home' and g['toGo'] == 94 and g['down'] == 1 and g['distance'] == 10 and not g['goal'], g
assert g['label'] == '1st & 10 · ND 6' and g['driveToGo'] == 94, g
print('live field: change of possession ok')
