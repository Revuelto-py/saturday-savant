"""Season totals, refreshed when a game ends instead of once a week.

`player_stats` holds each player's SEASON TOTALS, and the only thing that
rewrote it was the weekly destructive chain (pipeline/fetch_data.py, Sundays).
So a Thursday, Friday or Saturday game left every player who appeared in it
carrying last week's totals until the following Sunday — for most of the week
the leaderboards, the player pages and the home-page leaders showed numbers
that were simply out of date.

The site already knew about this from the other side: the player page rebuilds
a cached game log whenever a game has completed since the log was written,
precisely because a player's log went stale "while his season totals — which
come from player_stats, refreshed weekly — kept climbing" (see the comment at
main.py's game-log cache). This closes the other half, against the same signal.

Why this is a separate script from pipeline/fetch_data.py: that one DELETEs the
season's games, player_stats and player_ppa and re-inserts them. Correct once a
week, far too heavy and too dangerous to run on a game-day cadence — and it
also rewrites `games`, which pipeline/fetch_scores.py owns between chains.

Safe to run every few minutes, by construction:

  * UPSERT, never DELETE-then-INSERT. The key is
    (player_id, season, team, category, stat_type), which is unique across all
    1.2M stored rows. `team` belongs in it because a player who changes teams
    mid-season legitimately carries one row per team — 112 such groups exist,
    and keying without `team` would silently collapse them.
  * A short-payload guard. If CFBD returns implausibly few rows the run aborts
    having written nothing, so a bad upstream response cannot hollow out the
    table the way the 2026-07-21 incident did.
  * A cheap no-op when no game has finished since the last successful run: one
    SELECT and one small read, no API call. That is what makes a 10-minute
    cadence free for the ~160 runs a week with nothing to do.
  * The cache clear is skipped unless a row actually changed, so an idle run
    costs the site nothing. A tighter loop that always cleared would keep every
    page permanently cold.

Usage:  python3 pipeline/refresh_player_stats.py           # active season
        python3 pipeline/refresh_player_stats.py --force   # ignore the gate
        python3 pipeline/refresh_player_stats.py 2025      # a specific season
"""

# This script lives one directory below the repo root; ROOT points back at it so
# .env, the model artifacts and the shared modules resolve the same as before.
import os as _os, sys as _sys
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if ROOT not in _sys.path:
    _sys.path.insert(0, ROOT)

import gzip
import json
import os
import sys

import cfbd
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

from season_util import current_cfb_season

load_dotenv(_os.path.join(ROOT, '.env'))

FORCE = '--force' in sys.argv
_args = [a for a in sys.argv[1:] if not a.startswith('-')]
SEASON = int(_args[0]) if _args else current_cfb_season()

# Refuse to write when the payload is this much smaller than what is already
# stored. A real season only grows; a response that shrinks it by more than a
# fifth is an upstream problem, not a correction.
MIN_PAYLOAD_RATIO = 0.80

# Rows per INSERT. Large enough that the round trips stop mattering, small
# enough that one statement's parameters stay well inside libpq's limits.
BATCH = 2000

MARKER_KEY = f'statsrefresh:{SEASON}'


def _marker_read(cur):
    """The kickoff this season's totals were last refreshed against."""
    try:
        cur.execute('SELECT payload FROM pool_store WHERE key = %s', (MARKER_KEY,))
        row = cur.fetchone()
        if row and row[0]:
            return json.loads(gzip.decompress(bytes(row[0])).decode()).get('last_kickoff')
    except Exception:
        pass
    return None


def _marker_write(cur, last_kickoff):
    blob = gzip.compress(json.dumps({'last_kickoff': last_kickoff}).encode())
    cur.execute('''
        INSERT INTO pool_store (key, season, payload, updated_at)
        VALUES (%s, %s, %s, now())
        ON CONFLICT (key) DO UPDATE SET payload = EXCLUDED.payload,
                                        season = EXCLUDED.season,
                                        updated_at = now()
    ''', (MARKER_KEY, SEASON, psycopg2.Binary(blob)))


conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cursor = conn.cursor()

# The UPSERT needs this to exist. Idempotent, and verified safe to add: the key
# is unique across all 1.2M stored rows and none of its columns is ever NULL,
# which matters because Postgres treats NULLs as distinct in a unique index and
# would let ON CONFLICT silently miss.
cursor.execute('''
    CREATE UNIQUE INDEX IF NOT EXISTS idx_player_stats_upsert_key
        ON player_stats (player_id, season, team, category, stat_type)
''')
conn.commit()

# ── The gate ──────────────────────────────────────────────────────────────
# Same signal the player page uses to decide a cached game log is stale: the
# kickoff of the most recent completed game. If nothing has finished since the
# last successful refresh there is nothing new to total up.
cursor.execute("SELECT max(start_date::timestamptz) FROM games "
               "WHERE season = %s AND completed = 1", (SEASON,))
row = cursor.fetchone()
last_kickoff = row[0].isoformat() if row and row[0] else None

if last_kickoff is None:
    print(f'{SEASON}: no completed games yet — nothing to total')
    conn.close()
    sys.exit(0)

seen = _marker_read(cursor)
if seen == last_kickoff and not FORCE:
    print(f'{SEASON}: no game has finished since the last refresh — skipped')
    conn.close()
    sys.exit(0)

# ── The read ──────────────────────────────────────────────────────────────
cursor.execute('SELECT count(*) FROM player_stats WHERE season = %s', (SEASON,))
stored = cursor.fetchone()[0]

configuration = cfbd.Configuration(access_token=os.getenv('CFBD_API_KEY'))
with cfbd.ApiClient(configuration) as api_client:
    stats_api = cfbd.StatsApi(api_client)
    # 'both' = regular + postseason combined, matching pipeline/fetch_data.py,
    # so bowl and CFP production counts toward the season total.
    stats = stats_api.get_player_season_stats(year=SEASON, season_type='both')

# Collapse the payload on the storage key first. CFBD has not been observed to
# repeat a key, but an UPSERT that hits the same row twice in one statement is
# a hard error, so this makes a duplicate harmless instead of fatal.
rows = {}
for s in stats:
    if s.player_id is None:
        continue
    rows[(str(s.player_id), SEASON, s.team, s.category, s.stat_type)] = (
        s.player, s.conference, s.position, s.stat)

if not rows:
    print(f'{SEASON}: CFBD returned no player stats — nothing written')
    conn.close()
    sys.exit(1)

if stored and len(rows) < stored * MIN_PAYLOAD_RATIO:
    print(f'{SEASON}: payload is {len(rows)} rows against {stored} stored '
          f'({len(rows) / stored:.0%}) — refusing to write. Nothing changed.')
    conn.close()
    sys.exit(1)

# ── The write ─────────────────────────────────────────────────────────────
# Batched, because this is ~80k rows against a remote Postgres and one
# statement per row meant ~80k network round trips — minutes of wall clock for
# a job that has to finish inside a 15-minute window. execute_values sends them
# a few thousand at a time in a single statement.
#
# RETURNING plus the `WHERE ... IS DISTINCT FROM` below is what makes the
# changed count real: a row whose numbers have not moved is not written and
# does not come back, so an idle run reports 0 and leaves the cache warm.
changed = 0
payload = [(pid, name, team, conf, pos, category, stat_type, stat, season)
           for (pid, season, team, category, stat_type), (name, conf, pos, stat)
           in rows.items()]

for i in range(0, len(payload), BATCH):
    got = execute_values(cursor, '''
        INSERT INTO player_stats
            (player_id, player_name, team, conference, position,
             category, stat_type, stat, season)
        VALUES %s
        ON CONFLICT (player_id, season, team, category, stat_type) DO UPDATE
           SET stat        = EXCLUDED.stat,
               player_name = EXCLUDED.player_name,
               conference  = EXCLUDED.conference,
               position    = EXCLUDED.position
         WHERE player_stats.stat        IS DISTINCT FROM EXCLUDED.stat
            OR player_stats.player_name IS DISTINCT FROM EXCLUDED.player_name
            OR player_stats.conference  IS DISTINCT FROM EXCLUDED.conference
            OR player_stats.position    IS DISTINCT FROM EXCLUDED.position
        RETURNING 1
    ''', payload[i:i + BATCH], page_size=BATCH, fetch=True)
    changed += len(got)

_marker_write(cursor, last_kickoff)
conn.commit()
conn.close()

print(f'{SEASON}: {len(rows)} stat rows read, {changed} written '
      f'(stored was {stored})')

# ── The cache ─────────────────────────────────────────────────────────────
# Only when something actually moved. An idle run must not cost the site a
# cold cache, which is the whole reason this is safe to schedule tightly.
if changed:
    try:
        from cache_notify import notify_cache_clear
        notify_cache_clear()
        print('cache cleared')
    except Exception as e:
        print(f'cache clear failed ({e}) — data is in Postgres, pages will '
              f'refresh at their TTL')
else:
    print('nothing changed — cache left warm')
