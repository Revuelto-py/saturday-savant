"""Everything a finished game changes, refreshed when it finishes.

Two gaps, closed by one job because they have to stay consistent with each
other.

**Totals.** `player_stats` holds season totals and only the weekly chain
rewrote it, so a Thursday, Friday or Saturday game left everyone who played
carrying last week's numbers until Sunday. Scores were live; the totals under
them were days old.

**Percentiles.** Fixing the totals alone bought a worse problem: a player's
raw numbers would be current while his standing against the field — and every
team percentile on the team page and the game preview — still moved on
Sundays. A current value next to a stale rank is harder to trust than two
stale ones. So this refreshes the inputs AND rebuilds the derived stores in
the same pass, and clears the cache once, at the end.

What it touches, in dependency order:

  player_stats    season totals            UPSERT (this file)
  player_ppa      per-player EPA           UPSERT (this file)
  team_stats      team advanced            pipeline/fetch_team_stats.py
  team_advanced   team advanced (2nd set)  pipeline/fetch_advanced.py --team-only
  stats:* / ppa:* player percentile pools  recomputed from the above
  teampct:{season} team percentiles        recomputed from the above

Deliberately NOT touched: Savant ratings (opponent-adjusted, and the rating a
team carries into a week should not move under readers mid-slate), returning
production (a preseason figure), NFL talent (all-time), rosters, headshots,
EA ratings, game summaries. Those stay weekly.

Why this is not pipeline/fetch_data.py: that DELETEs the season's games,
player_stats and player_ppa and re-inserts them — right once a week, far too
heavy and too dangerous on a game-day cadence, and it also rewrites `games`,
which pipeline/fetch_scores.py owns between chains.

Safe to run every few minutes, by construction:

  * UPSERT for the two player tables, never DELETE-then-INSERT. The
    player_stats key is (player_id, season, team, category, stat_type), unique
    across all 1.2M stored rows; `team` belongs in it because a player who
    changes teams mid-season legitimately carries one row per team — 112 such
    groups exist, and keying without it would collapse them silently.
  * A short-payload guard on every fetch, here and in the two team scripts. A
    season's numbers only grow; a response that shrinks them by more than a
    fifth is an upstream fault, not a correction, and is refused outright.
  * A cheap no-op when no game has finished since the last successful run: one
    SELECT and one small read, no API call at all. That is what makes the ~160
    idle runs a week free.
  * One cache clear, only if something actually changed. A tighter loop that
    always cleared would keep every page permanently cold.

Usage:  python3 pipeline/refresh_stats.py            # active season
        python3 pipeline/refresh_stats.py --force    # ignore the gate
        python3 pipeline/refresh_stats.py 2025       # a specific season
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
import subprocess
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

# How many times to re-check a finished game before accepting that CFBD has
# nothing more for it. At a 15-minute cadence this is two hours, comfortably
# longer than the gap between a final and its box score being published.
MAX_EMPTY_RETRIES = 8

# Rows per INSERT. Large enough that the round trips stop mattering, small
# enough that one statement's parameters stay well inside libpq's limits.
BATCH = 2000

MARKER_KEY = f'statsrefresh:{SEASON}'

# Rebuilt from the tables above. Mirrors pipeline/backfill_pools.py's list —
# the player page and compare tool read exactly these.
QB  = ('QB',)
RB  = ('RB', 'HB', 'FB')
WRT = ('WR', 'TE')
DL  = ('DE', 'DT', 'NT', 'DL', 'EDGE')
LB  = ('LB', 'ILB', 'OLB', 'MLB')
DB  = ('CB', 'S', 'SS', 'FS', 'SAF', 'DB')
FRONT7 = DL + LB
WIDE_SKILL = ('WR', 'TE', 'RB', 'HB', 'FB')
STAT_POOLS = [('passing', QB), ('rushing', RB), ('rushing', QB),
              ('receiving', WRT), ('receiving', WIDE_SKILL),
              ('defensive', DL), ('defensive', LB), ('defensive', DB),
              ('defensive', FRONT7)]
PPA_POOLS = [QB, RB, WRT, DL, LB, DB]


def _marker_read(cur):
    """The kickoff this season's stats were last refreshed against, and how
    many times we have tried against it without anything moving."""
    try:
        cur.execute('SELECT payload FROM pool_store WHERE key = %s', (MARKER_KEY,))
        row = cur.fetchone()
        if row and row[0]:
            d = json.loads(gzip.decompress(bytes(row[0])).decode())
            return d.get('last_kickoff'), int(d.get('attempts') or 0)
    except Exception:
        pass
    return None, 0


def _marker_write(cur, last_kickoff, attempts=0):
    blob = gzip.compress(json.dumps({'last_kickoff': last_kickoff,
                                     'attempts': attempts}).encode())
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

seen, attempts = _marker_read(cursor)
if seen == last_kickoff and attempts >= MAX_EMPTY_RETRIES and not FORCE:
    print(f'{SEASON}: no game has finished since the last refresh — skipped')
    conn.close()
    sys.exit(0)

# ── Stage 1: season totals ───────────────────────────────────────────────
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

print(f'{SEASON}: player_stats — {len(rows)} read, {changed} written '
      f'(stored was {stored})')

# ── Stage 2: per-player EPA ───────────────────────────────────────────────
# Feeds the EPA percentile pools. Its natural key is already unique, so this
# is a straight upsert. Offence-only by source: CFBD publishes no defensive
# per-player EPA, which is why the site's defensive percentile sets are built
# from box-score stats instead.
ppa_changed = 0
with cfbd.ApiClient(configuration) as api_client:
    ppa_data = cfbd.MetricsApi(api_client).get_predicted_points_added_by_player_season(year=SEASON)

cursor.execute('SELECT count(*) FROM player_ppa WHERE season = %s', (SEASON,))
ppa_stored = cursor.fetchone()[0]

if ppa_stored and len(ppa_data) < ppa_stored * MIN_PAYLOAD_RATIO:
    print(f'{SEASON}: player_ppa payload is {len(ppa_data)} against {ppa_stored} '
          f'stored — skipped, totals above still written')
else:
    ppa_rows = {}
    for p in ppa_data:
        if p.id is None:
            continue
        ppa_rows[(str(p.id), SEASON)] = (
            p.name, p.position, p.team, p.conference,
            p.average_ppa.all, p.average_ppa.var_pass, p.average_ppa.rush,
            p.total_ppa.all)
    for i in range(0, len(ppa_rows), BATCH):
        chunk = list(ppa_rows.items())[i:i + BATCH]
        got = execute_values(cursor, '''
            INSERT INTO player_ppa (player_id, season, player_name, position, team,
                                    conference, avg_ppa_all, avg_ppa_pass,
                                    avg_ppa_rush, total_ppa)
            VALUES %s
            ON CONFLICT (player_id, season) DO UPDATE
               SET player_name  = EXCLUDED.player_name,
                   position     = EXCLUDED.position,
                   team         = EXCLUDED.team,
                   conference   = EXCLUDED.conference,
                   avg_ppa_all  = EXCLUDED.avg_ppa_all,
                   avg_ppa_pass = EXCLUDED.avg_ppa_pass,
                   avg_ppa_rush = EXCLUDED.avg_ppa_rush,
                   total_ppa    = EXCLUDED.total_ppa
             WHERE player_ppa.avg_ppa_all  IS DISTINCT FROM EXCLUDED.avg_ppa_all
                OR player_ppa.avg_ppa_pass IS DISTINCT FROM EXCLUDED.avg_ppa_pass
                OR player_ppa.avg_ppa_rush IS DISTINCT FROM EXCLUDED.avg_ppa_rush
                OR player_ppa.total_ppa    IS DISTINCT FROM EXCLUDED.total_ppa
                OR player_ppa.team         IS DISTINCT FROM EXCLUDED.team
            RETURNING 1
        ''', [(pid, season, nm, pos, tm, cf, a, pa, ru, tot)
              for (pid, season), (nm, pos, tm, cf, a, pa, ru, tot) in chunk],
            page_size=BATCH, fetch=True)
        ppa_changed += len(got)
    print(f'{SEASON}: player_ppa — {len(ppa_rows)} read, {ppa_changed} written')

# Only advance the marker once something actually landed. A game ending and
# CFBD publishing its box score are minutes apart, and a marker that advanced
# on the empty run in between would close the gate over its own miss — the
# stats would then wait for the NEXT game. Retrying instead, with a budget so
# the quiet midweek days do not become an endless poll.
if changed or ppa_changed:
    _marker_write(cursor, last_kickoff, MAX_EMPTY_RETRIES)
else:
    _marker_write(cursor, last_kickoff,
                  (attempts + 1) if seen == last_kickoff else 1)
    print(f'{SEASON}: nothing landed yet — attempt '
          f'{(attempts + 1) if seen == last_kickoff else 1} of {MAX_EMPTY_RETRIES}')
conn.commit()
conn.close()

# ── Stage 3: team stats ───────────────────────────────────────────────────
# Delegated rather than re-implemented: these two carry ~70 columns of CFBD
# field mapping between them, and a second copy would drift. Both now refuse a
# short payload themselves, which is what makes them safe to call here.
def _team_fingerprint():
    """A cheap digest of both team tables for the season.

    Needed because these two scripts DELETE and re-INSERT unconditionally, so
    "the subprocess exited 0" says nothing about whether a number moved. Without
    this the job would rebuild 16 percentile stores and clear the site cache on
    every run, which is exactly the cost the gate exists to avoid.
    """
    c = psycopg2.connect(os.getenv('DATABASE_URL'))
    try:
        k = c.cursor()
        out = []
        for tbl, col in (('team_stats', 'off_ppa'), ('team_advanced', 'off_ppa')):
            k.execute(f'SELECT count(*), round(sum({col})::numeric, 6), '
                      f'round(sum(def_ppa)::numeric, 6) '
                      f'FROM {tbl} WHERE season = %s', (SEASON,))
            out.append(k.fetchone())
        return out
    except Exception:
        return None
    finally:
        c.close()


if not (changed or ppa_changed or FORCE):
    print(f'{SEASON}: nothing landed for the players either — leaving team '
          f'stats and the percentile stores alone until it does')
    sys.exit(0)

before = _team_fingerprint()
for label, cmd in (('team_stats',    [sys.executable, _os.path.join(ROOT, 'pipeline', 'fetch_team_stats.py')]),
                   ('team_advanced', [sys.executable, _os.path.join(ROOT, 'pipeline', 'fetch_advanced.py'), '--team-only'])):
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        # Non-fatal on purpose: the player half above is already committed, and
        # a stale team percentile is a smaller problem than dropping the run.
        tail = (r.stdout or r.stderr or '').strip().splitlines()[-1:] or ['no output']
        print(f'{SEASON}: {label} — SKIPPED ({tail[0]})')
after = _team_fingerprint()
team_changed = before is not None and after is not None and before != after
print(f'{SEASON}: team stats — {"changed" if team_changed else "unchanged"}')

# ── Stage 4: the derived percentile stores ────────────────────────────────
# These are pure recomputations from the tables above — no API, no destructive
# write. The stored blob has to be dropped first or the compute functions read
# the stale value straight back out and never rebuild.
#
# Imported late: main.py opens a connection pool at import time, and doing that
# before the gate above would cost every idle run a pool it never uses.
pools_built = 0
if changed or ppa_changed or team_changed or FORCE:
    _os.environ.setdefault('POOL_BACKFILL', '1')
    import main  # noqa: E402  — reuse the exact pool + percentile code paths

    stale = [f"stats:{cat}:{','.join(pos)}:{SEASON}" for cat, pos in STAT_POOLS]
    stale += [f"ppa:{','.join(pos)}:{SEASON}" for pos in PPA_POOLS]
    stale += [f'teampct:{SEASON}']
    main._pool_store_delete(stale)

    for category, positions in STAT_POOLS:
        fn = main._stats_pool_cached
        (fn.uncached if hasattr(fn, 'uncached') else fn)(category, positions, SEASON)
        pools_built += 1
    for positions in PPA_POOLS:
        fn = main._ppa_pool_cached
        (fn.uncached if hasattr(fn, 'uncached') else fn)(positions, SEASON)
        pools_built += 1
    fn = main._team_percentiles_all
    (fn.uncached if hasattr(fn, 'uncached') else fn)(SEASON)
    pools_built += 1
    print(f'{SEASON}: percentiles — {pools_built} stores rebuilt')
else:
    print(f'{SEASON}: no input moved — percentile stores left alone')

# ── The cache ─────────────────────────────────────────────────────────────
# Once, at the end, and only when something moved. An idle run must not cost
# the site a cold cache, which is the whole reason this is safe to schedule
# tightly. Note the derived stores above are rebuilt BEFORE this fires, so the
# first reader after a clear gets fresh values rather than recomputing them.
if changed or ppa_changed or team_changed:
    try:
        from cache_notify import notify_cache_clear
        notify_cache_clear()
        print('cache cleared')
    except Exception as e:
        print(f'cache clear failed ({e}) — data is in Postgres, pages will '
              f'refresh at their TTL')
else:
    print('nothing changed — cache left warm')
