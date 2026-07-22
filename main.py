# === PERFORMANCE NOTES ===
# Cached routes (Flask-Caching, SimpleCache, in-memory):
#   /leaderboards, /leaderboards/<category>        @cache.cached(3600, query_string=True) — pre-existing
#   /leaderboards/teams, /leaderboards/teams/<cat>  @cache.cached(3600, query_string=True) — pre-existing
#   /teams                                          @cache.cached(86400) — pre-existing
#   /team/<team_name>                               @cache.cached(3600) — pre-existing
#   /rankings                                        @cache.cached(3600) — pre-existing
#   /rivalries                                       @cache.cached(86400) — pre-existing
#   /player/<player_id>                              @cache.memoize(3600) — added
#   get_cached_season_leaders() (home page sidebar)  @cache.memoize(3600) — added, keyed
#                                                     independently of week so it's computed
#                                                     once instead of once per distinct week URL
#   /admin/clear-cache clears the whole cache store, which covers both
#   @cache.cached and @cache.memoize since they share the same backend — no
#   changes needed there.
# NOT cached (by design — no /live routes exist in this app; /game/<id> pulls
# from ESPN and isn't cached here either, matching the rest of Pass 1's scope):
#   /game/<game_id>, /
#
# Indexes (see ensure_indexes(), run once at startup, CREATE INDEX IF NOT
# EXISTS so it's a no-op on repeat boots):
#   idx_player_stats_player_id       — pre-existing
#   idx_player_ppa_player_id         — pre-existing
#   idx_players_team                 — pre-existing
#   idx_player_stats_category_stattype (category, stat_type) — pre-existing
#   idx_player_stats_team            — added
#   idx_games_season_week (season, week) — added
#   (player_usage.player_id and games.id are already primary keys, so no
#   separate index was needed for either)
#
# N+1 fixes:
#   get_rivalry_map() — home() and team() used to call get_rivalry() once per
#   game/schedule row (one query each). Now one query loads the whole
#   rivalries table (328 rows) into a dict per request instead.
#
# Already fine, no change needed:
#   /leaderboards pagination already uses LIMIT/OFFSET at the SQL level.
#   /player and /team routes already ran all their DB work through a single
#   get_db()/release_db() block rather than one connection per query.
#   /team's schedule query already selects only the columns it needs, no
#   SELECT *.
#   Player-page percentile ranks already batch-fetch one query per position
#   group/category (see _fetch_stats_pool / _fetch_ppa_pool) and rank/percentile
#   in Python — not one query per stat.
#   /game/<game_id>'s two ESPN calls were already wrapped in try/except with
#   pre-initialized empty defaults, so a slow/failed ESPN response already
#   degraded gracefully instead of crashing the page — timeouts tightened to
#   4s (previously 8s/10s) so a hung ESPN request fails fast instead of
#   stalling page render.
# === END PERFORMANCE NOTES ===

import cfbd
import psycopg2
from psycopg2 import pool as pg_pool
import gzip
import json
import os
import re
import datetime
import hmac
import unicodedata
from zoneinfo import ZoneInfo
import requests as req
from urllib.parse import urlencode
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, Response, redirect, send_from_directory
from flask_caching import Cache
from collections import OrderedDict
from itertools import groupby

load_dotenv()

app = Flask(__name__)

cache = Cache(app, config={
    'CACHE_TYPE': 'SimpleCache',       # in-memory, no Redis needed
    'CACHE_DEFAULT_TIMEOUT': 3600,     # 1 hour default TTL
    # Cap entries well below the default 500: entries include fully rendered
    # player pages (~100-400KB each), and an uncapped cache under heavy
    # player-page traffic can push a small instance into swap — which
    # presents as EVERYTHING (even static files) taking seconds.
    'CACHE_THRESHOLD': 250,
})

configuration = cfbd.Configuration(
    access_token=os.getenv("CFBD_API_KEY")
)

# Connection pool — min 2 connections always open, max 10
connection_pool = None

def init_db_pool():
    global connection_pool
    connection_pool = pg_pool.ThreadedConnectionPool(
        minconn=2,
        maxconn=10,
        dsn=os.getenv('DATABASE_URL')
    )
    print("Database connection pool initialized")
    print(f"Pool created: min={connection_pool.minconn}, max={connection_pool.maxconn}")

def get_db():
    return connection_pool.getconn()

def release_db(conn):
    connection_pool.putconn(conn)

init_db_pool()

def ensure_indexes():
    """Idempotent — safe to run on every boot. CREATE INDEX IF NOT EXISTS is a
    no-op for anything that already exists, so this just backfills whatever's
    missing (and self-documents the full set this app relies on)."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_player_stats_player_id ON player_stats(player_id);
            CREATE INDEX IF NOT EXISTS idx_player_stats_team ON player_stats(team);
            CREATE INDEX IF NOT EXISTS idx_player_stats_category_stattype ON player_stats(category, stat_type);
            CREATE INDEX IF NOT EXISTS idx_player_ppa_player_id ON player_ppa(player_id);
            CREATE INDEX IF NOT EXISTS idx_players_team ON players(team);
            CREATE INDEX IF NOT EXISTS idx_games_season_week ON games(season, week);
            -- multi-season perf set (added with the 2016-2025 expansion):
            -- the (id::text) expression index is what lets every
            -- `ps.player_id = p.id::text` join probe players instead of
            -- sorting all 52k rows per query.
            CREATE INDEX IF NOT EXISTS idx_players_id_text ON players ((id::text));
            CREATE INDEX IF NOT EXISTS idx_player_stats_season_cat_type ON player_stats (season, category, stat_type);
            CREATE INDEX IF NOT EXISTS idx_player_stats_team_season ON player_stats (team, season);
            CREATE INDEX IF NOT EXISTS idx_player_stats_pid_season ON player_stats (player_id, season);
            CREATE INDEX IF NOT EXISTS idx_player_ppa_pid_season ON player_ppa (player_id, season);
            CREATE INDEX IF NOT EXISTS idx_team_stats_team_season ON team_stats (team, season);
            CREATE INDEX IF NOT EXISTS idx_team_stats_season ON team_stats (season);
            CREATE INDEX IF NOT EXISTS idx_team_advanced_team_season ON team_advanced (team, season);
            CREATE INDEX IF NOT EXISTS idx_team_advanced_season ON team_advanced (season);
            CREATE INDEX IF NOT EXISTS idx_sp_ratings_team_season ON sp_ratings (team, season);
            CREATE INDEX IF NOT EXISTS idx_sp_ratings_season ON sp_ratings (season);
            CREATE INDEX IF NOT EXISTS idx_ap_rankings_season ON ap_rankings (season);
            CREATE INDEX IF NOT EXISTS idx_ap_rankings_team_season ON ap_rankings (team, season);
            CREATE INDEX IF NOT EXISTS idx_games_home_season ON games (home_team, season);
            CREATE INDEX IF NOT EXISTS idx_games_away_season ON games (away_team, season);
        ''')
        conn.commit()
    except Exception as e:
        print(f"Index setup skipped/failed (non-fatal): {e}")
        conn.rollback()
    finally:
        release_db(conn)

ensure_indexes()

# ── Seasons ──────────────────────────────────────────────────────────────────
# CURRENT_SEASON is the most recent season that actually has stats loaded — the
# default for leaderboards, team stats, and player pages. It's DERIVED from the
# data, not hardcoded, so it advances to a new season on its own the first time
# that season produces stats; until then the site keeps showing the last
# completed season. UPCOMING_SEASON is the season the site projects forward to
# (next roster / schedule). Historical seasons (2016+) stay reachable via
# ?season=YYYY. The data pipeline's *ingest* season is separate and date-driven
# (season_util.current_cfb_season) — it rolls over before the new season has any
# data, which is exactly why the display default can't just mirror it.
def _newest_stats_season(default=2025):
    try:
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute('SELECT MAX(season) FROM player_stats WHERE season IS NOT NULL')
            row = cur.fetchone()
            return row[0] if row and row[0] else default
        finally:
            release_db(conn)
    except Exception:
        return default

CURRENT_SEASON = _newest_stats_season()
UPCOMING_SEASON = CURRENT_SEASON + 1

@cache.memoize(timeout=21600)
def get_available_seasons():
    """Seasons that actually have stats loaded, newest first — drives the
    season selector so it only offers years the backfill has populated."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        # Loose index scan (recursive skip scan): ~one index probe per season
        # instead of walking all 1M+ index entries with DISTINCT.
        cursor.execute('''
            WITH RECURSIVE t AS (
                (SELECT season FROM player_stats WHERE season IS NOT NULL
                 ORDER BY season DESC LIMIT 1)
                UNION ALL
                SELECT (SELECT season FROM player_stats
                        WHERE season < t.season AND season IS NOT NULL
                        ORDER BY season DESC LIMIT 1)
                FROM t WHERE t.season IS NOT NULL
            )
            SELECT season FROM t WHERE season IS NOT NULL ORDER BY season DESC
        ''')
        seasons = [r[0] for r in cursor.fetchall()]
        return seasons or [CURRENT_SEASON]
    finally:
        release_db(conn)

def requested_season():
    """Season from ?season=YYYY, validated against loaded data; defaults to
    (and falls back to) the current season."""
    s = request.args.get('season', type=int)
    if s and s in get_available_seasons():
        return s
    return CURRENT_SEASON

def get_ap_rankings(cursor, season=CURRENT_SEASON):
    """The FINAL AP poll for a season as {team: rank} — the postseason poll if
    it exists, else the latest regular-season week. ap_rankings now holds every
    weekly poll, so this pins to a single one (used for the home ticker, team
    hero, etc., where a single poll is wanted)."""
    cursor.execute('''
        SELECT team, rank FROM ap_rankings WHERE season = %s
          AND (season_type, week) = (
              SELECT season_type, week FROM ap_rankings WHERE season = %s
              ORDER BY (season_type = 'postseason') DESC, week DESC LIMIT 1)
    ''', (season, season))
    return {t: r for t, r in cursor.fetchall()}


def get_ap_week_map(cursor, season):
    """Every weekly AP poll for a season, for as-of-game-week lookups.
    Returns (regular, final): regular = {ranking_week: {team: rank}},
    final = {team: rank} (the postseason poll). Empty when a season has no
    polls loaded yet (e.g. a season that hasn't started)."""
    regular, final = {}, {}
    try:
        cursor.execute('SELECT season_type, week, team, rank FROM ap_rankings WHERE season = %s',
                       (season,))
        for stype, week, team, rank in cursor.fetchall():
            if stype == 'postseason':
                final[team] = rank
            else:
                regular.setdefault(week, {})[team] = rank
    except psycopg2.Error:
        cursor.connection.rollback()
    return regular, final


def ap_asof(ap_weekly, week, is_post):
    """The AP poll {team: rank} in effect for a game — the latest regular poll
    whose ranking week ≤ the game's week (poll teams carry INTO that week), or
    the final poll for postseason games. Empty when no poll applies yet."""
    regular, final = ap_weekly
    if is_post:
        return final
    weeks = [w for w in regular if w <= (week or 0)]
    return regular[max(weeks)] if weeks else {}

def get_conference_logos(cursor):
    """{conference_name: logo_url} for the Teams page and team-page standings.
    Populated by fetch_conf_logos.py; empty dict if the table isn't present yet."""
    try:
        cursor.execute('SELECT conference, logo FROM conference_logos')
        return {row[0]: row[1] for row in cursor.fetchall()}
    except psycopg2.Error:
        cursor.connection.rollback()
        return {}

_VALID_PPA_COLS = {'avg_ppa_all', 'avg_ppa_pass', 'avg_ppa_rush', 'total_ppa'}

def _fetch_stats_pool(cursor, category, positions, season=CURRENT_SEASON):
    """Peer pool for percentiles: all stat_types for all players at these
    positions in one season. The pool is identical for every player in the
    group, so it's memoized — a player page runs ~a dozen of these, and
    without the cache each page paid all of them fresh. Treat the returned
    dict as read-only (it's shared across requests)."""
    return _stats_pool_cached(category, tuple(positions), season)

def _pool_store_get(key):
    """Second cache layer under the memoize: pools persisted in Postgres
    (gzipped JSON), so a cache-pruned/restarted worker pays a ~30ms read
    instead of recomputing a ~400ms 15k-row aggregate."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT payload FROM pool_store WHERE key = %s', (key,))
        row = cur.fetchone()
        if row and row[0]:
            return json.loads(gzip.decompress(bytes(row[0])))
    except Exception:
        conn.rollback()
    finally:
        release_db(conn)
    return None

def _pool_store_put(key, season, pool):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS pool_store (
                key        TEXT PRIMARY KEY,
                season     INTEGER,
                payload    BYTEA,
                updated_at TIMESTAMPTZ DEFAULT now()
            )
        ''')
        cur.execute('''
            INSERT INTO pool_store (key, season, payload) VALUES (%s, %s, %s)
            ON CONFLICT (key) DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()
        ''', (key, season, gzip.compress(json.dumps(pool).encode())))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        release_db(conn)

def _pool_store_delete(keys):
    """Drop stored pools by exact key so the next compute regenerates them.
    The weekly precompute uses this to force a refresh — otherwise the compute
    functions read the (now stale) value straight back out of pool_store and
    never recompute against the freshly-fetched tables."""
    keys = [k for k in keys if k]
    if not keys:
        return
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('DELETE FROM pool_store WHERE key = ANY(%s)', (list(keys),))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        release_db(conn)

@cache.memoize(timeout=21600)
def _stats_pool_cached(category, positions_key, season):
    key = f"stats:{category}:{','.join(positions_key)}:{season}"
    stored = _pool_store_get(key)
    if stored is not None:
        return stored
    conn = get_db()
    try:
        cursor = conn.cursor()
        ph = ','.join(['%s'] * len(positions_key))
        cursor.execute(f'''
            SELECT ps.player_id, ps.stat_type, CAST(ps.stat AS REAL)
            FROM player_stats ps
            JOIN players pl ON ps.player_id = pl.id::text
            WHERE ps.category=%s AND ps.season=%s AND pl.position IN ({ph}) AND ps.stat IS NOT NULL
        ''', [category, season] + list(positions_key))
        pool = {}
        for pid, st, val in cursor.fetchall():
            pool.setdefault(pid, {})[st] = val
    finally:
        release_db(conn)
    _pool_store_put(key, season, pool)
    return pool

def _fetch_ppa_pool(cursor, positions, season=CURRENT_SEASON):
    """PPA peer pool (one season) — memoized like _fetch_stats_pool; read-only."""
    return _ppa_pool_cached(tuple(positions), season)

@cache.memoize(timeout=21600)
def _ppa_pool_cached(positions_key, season):
    key = f"ppa:{','.join(positions_key)}:{season}"
    stored = _pool_store_get(key)
    if stored is not None:
        return stored
    conn = get_db()
    try:
        cursor = conn.cursor()
        ph = ','.join(['%s'] * len(positions_key))
        cursor.execute(f'''
            SELECT pp.player_id, pp.avg_ppa_all, pp.avg_ppa_pass, pp.avg_ppa_rush, pp.total_ppa
            FROM player_ppa pp
            JOIN players pl ON pp.player_id = pl.id::text
            WHERE pp.season=%s AND pl.position IN ({ph})
        ''', [season] + list(positions_key))
        pool = {}
        for pid, *vals in cursor.fetchall():
            pool[pid] = dict(zip(('avg_ppa_all','avg_ppa_pass','avg_ppa_rush','total_ppa'), vals))
    finally:
        release_db(conn)
    _pool_store_put(key, season, pool)
    return pool

def _fetch_usage_pool(cursor, positions, season=CURRENT_SEASON):
    """Usage-rate peer pool (player_usage.overall = share of team plays the
    player was involved in) — the site's own snap-count proxy / target-share
    signal. Memoized + persisted like the other pools; keyed on the canonical
    players.id so it lines up with the stats/PPA pools. Read-only."""
    return _usage_pool_cached(tuple(positions), season)

@cache.memoize(timeout=21600)
def _usage_pool_cached(positions_key, season):
    key = f"usage:{','.join(positions_key)}:{season}"
    stored = _pool_store_get(key)
    if stored is not None:
        return stored
    conn = get_db()
    try:
        cursor = conn.cursor()
        ph = ','.join(['%s'] * len(positions_key))
        cursor.execute(f'''
            SELECT pl.id::text, CAST(pu.overall AS REAL)
            FROM player_usage pu
            JOIN players pl ON pu.player_id::text = pl.id::text
            WHERE pu.season=%s AND pl.position IN ({ph}) AND pu.overall IS NOT NULL
        ''', [season] + list(positions_key))
        pool = {pid: {'overall': ov} for pid, ov in cursor.fetchall()}
    finally:
        release_db(conn)
    _pool_store_put(key, season, pool)
    return pool

def _rank_pct(player_id, pool, stat_key, higher_better=True):
    """Rank and percentile from pre-fetched pool dict — no DB call."""
    pid_str = str(player_id)
    vals = [(pid, d[stat_key]) for pid, d in pool.items()
            if d.get(stat_key) is not None]
    if not vals:
        return None, None, 0
    my_val = next((v for pid, v in vals if pid == pid_str), None)
    if my_val is None:
        return None, None, len(vals)
    all_vals = [v for _, v in vals]
    n = len(all_vals)
    if higher_better:
        rank  = sum(1 for v in all_vals if v > my_val) + 1
        below = sum(1 for v in all_vals if v < my_val)
    else:
        rank  = sum(1 for v in all_vals if v < my_val) + 1
        below = sum(1 for v in all_vals if v > my_val)
    # Mid-rank ties so a modal value (e.g. a LB with 0 QB hurries, where most
    # peers also have 0) lands mid-pack, not dumped at the 1st-percentile floor
    # the plain "fraction strictly below" would give every zero-holder.
    equal = sum(1 for v in all_vals if v == my_val)
    percentile = max(1, min(99, round((below + 0.5 * equal) / n * 100)))
    return rank, percentile, n

# Minimum qualification thresholds by position group and stat category.
# These filter the peer pool to only "qualified" players before computing
# ranks/percentiles, using counting stats as a proxy for meaningful playing
# time (snap counts aren't in the database).
QUALIFICATIONS = {
    'QB': {
        'passing': {'ATT': 100},   # min 100 pass attempts
        'rushing': {'CAR': 20},    # min 20 carries for QB rush stats — rushing category's counting stat is CAR, not ATT
        'ppa':     {'ATT': 100},   # min 100 attempts for EPA (checked against the passing pool, which uses ATT)
    },
    'RB': {
        'rushing':   {'CAR': 50},  # min 50 carries — rushing category's counting stat is CAR, not ATT
        'receiving': {'REC': 10},  # min 10 receptions for RB receiving
        'ppa':       {'CAR': 50},  # checked against the rushing pool, which uses CAR
    },
    'WR': {
        'receiving': {'REC': 20},  # min 20 receptions
        'ppa':       {'REC': 20},
    },
    'TE': {
        'receiving': {'REC': 10},  # min 10 receptions (TEs get fewer targets)
        'ppa':       {'REC': 10},
    },
    'DL': {
        'defensive': {'TOT': 15},  # min 15 total tackles
        'ppa':       {'TOT': 15},
    },
    'LB': {
        'defensive': {'TOT': 20},  # LBs should have more tackles to qualify
        'ppa':       {'TOT': 20},
    },
    'DB': {
        'defensive': {'TOT': 15},
        'ppa':       {'TOT': 15},
    },
}

# Map positions to their group for qualification lookup
POS_GROUP_MAP = {
    'QB': 'QB',
    'RB': 'RB', 'HB': 'RB', 'FB': 'RB',
    'WR': 'WR',
    'TE': 'TE',
    'DE': 'DL', 'DT': 'DL', 'NT': 'DL', 'DL': 'DL', 'EDGE': 'DL',
    'LB': 'LB', 'ILB': 'LB', 'OLB': 'LB', 'MLB': 'LB',
    'CB': 'DB', 'S': 'DB', 'SS': 'DB', 'FS': 'DB', 'SAF': 'DB', 'DB': 'DB',
}

# Which stats category holds the qualifying counting stat for 'ppa' lookups
# (EPA pools don't carry ATT/REC/TOT themselves).
QUAL_SOURCE_CATEGORY = {
    'QB': 'passing', 'RB': 'rushing', 'WR': 'receiving', 'TE': 'receiving',
    'DL': 'defensive', 'LB': 'defensive', 'DB': 'defensive',
}

def _qual_threshold(pos_group, category):
    """(stat, minimum) qualification threshold for a position group + category, or (None, 0)."""
    q = QUALIFICATIONS.get(pos_group, {}).get(category, {})
    return next(iter(q.items())) if q else (None, 0)

def _qualify_pool(pool, qual_source, qual_stat, qual_min):
    """Filter a pool dict down to player_ids meeting a counting-stat minimum,
    looked up from qual_source (may be the same pool, or a different category's
    pool when the qualifying stat isn't part of `pool` itself, e.g. EPA pools)."""
    if not qual_stat or qual_min <= 0:
        return pool
    return {
        pid: d for pid, d in pool.items()
        if (qual_source.get(pid, {}).get(qual_stat) or 0) >= qual_min
    }

def compute_rank_and_percentile(cursor, player_id, stat_type, category, positions, higher_better=True, season=CURRENT_SEASON):
    """Single-stat rank, filtered to the qualified peer pool (kept for any legacy call sites)."""
    pos_group = POS_GROUP_MAP.get(positions[0], positions[0])
    qual_stat, qual_min = _qual_threshold(pos_group, category)

    if category == 'ppa':
        pool = _fetch_ppa_pool(cursor, positions, season)
        qual_category = QUAL_SOURCE_CATEGORY.get(pos_group)
        qual_source = _fetch_stats_pool(cursor, qual_category, positions, season) if qual_category else pool
        pool = _qualify_pool(pool, qual_source, qual_stat, qual_min)
        return _rank_pct(player_id, pool, stat_type, higher_better)

    pool = _fetch_stats_pool(cursor, category, positions, season)
    pool = _qualify_pool(pool, pool, qual_stat, qual_min)
    single = {pid: {stat_type: d.get(stat_type)} for pid, d in pool.items()}
    return _rank_pct(player_id, single, stat_type, higher_better)


# ── Player percentile rankings (Baseball-Savant style) ────────────────────────
# Curated, advanced-first metric sets per position group. Each row is
# (label, source, stat_key, higher_better); `source` names a peer pool assembled
# in _build_percentiles. The lead metrics are efficiency / EPA-based (the point
# of the feature); a few raw counting stats trail for context where no better
# efficiency equivalent exists in the data. Every stat_key here was verified to
# be populated for the position — sparse metrics (player-level success rate,
# explosiveness, defensive EPA, exact target share, catch rate) are NOT stored
# per player anywhere in the DB, so they are deliberately absent rather than
# shown on an unreliable pool. See the report accompanying this change.
#
# Which advanced data actually exists per player:
#   • EPA (player_ppa: avg_ppa_all/pass/rush, total_ppa) — OFFENSE ONLY.
#   • Efficiency rates (player_stats: PCT, YPA, YPC, YPR).
#   • Usage rate (player_usage.overall) — involvement / target-share proxy.
#   • Defense has no EPA or rate stats (no snap counts) — only disruption and
#     coverage counting stats, led by the most differentiating ones.
PERCENTILE_METRICS = {
    'QB': [
        ('EPA / Play',        'ppa',       'avg_ppa_all',  True),
        ('Pass EPA / Play',   'ppa',       'avg_ppa_pass', True),
        ('Rush EPA / Play',   'ppa',       'avg_ppa_rush', True),
        ('Total EPA',         'ppa',       'total_ppa',    True),
        ('Completion %',      'passing',   'PCT',          True),
        ('Yards / Attempt',   'passing',   'YPA',          True),
        ('Interceptions',     'passing',   'INT',          False),
        ('Pass Yards',        'passing',   'YDS',          True),
        ('Pass TDs',          'passing',   'TD',           True),
    ],
    'RB': [
        ('EPA / Play',        'ppa',       'avg_ppa_all',  True),
        ('Rush EPA / Play',   'ppa',       'avg_ppa_rush', True),
        ('Total EPA',         'ppa',       'total_ppa',    True),
        ('Yards / Carry',     'rushing',   'YPC',          True),
        ('Usage Rate',        'usage',     'overall',      True),
        ('Rush Yards',        'rushing',   'YDS',          True),
        ('Rush TDs',          'rushing',   'TD',           True),
        ('Receiving Yards',   'receiving', 'YDS',          True),
    ],
    'WR': [
        ('EPA / Play',        'ppa',       'avg_ppa_all',  True),
        ('Total EPA',         'ppa',       'total_ppa',    True),
        ('Yards / Reception', 'receiving', 'YPR',          True),
        ('Usage Rate',        'usage',     'overall',      True),
        ('Receiving Yards',   'receiving', 'YDS',          True),
        ('Receptions',        'receiving', 'REC',          True),
        ('Receiving TDs',     'receiving', 'TD',           True),
    ],
    'DL': [
        ('Sacks',             'defensive', 'SACKS',        True),
        ('Tackles for Loss',  'defensive', 'TFL',          True),
        ('QB Hurries',        'defensive', 'QB HUR',       True),
        ('Passes Defended',   'defensive', 'PD',           True),
        ('Tackles',           'defensive', 'TOT',          True),
        ('Solo Tackles',      'defensive', 'SOLO',         True),
    ],
    'DB': [
        ('Interceptions',     'ints',      'INT',          True),
        ('Passes Defended',   'defensive', 'PD',           True),
        ('Tackles for Loss',  'defensive', 'TFL',          True),
        ('Tackles',           'defensive', 'TOT',          True),
        ('Solo Tackles',      'defensive', 'SOLO',         True),
    ],
}
PERCENTILE_METRICS['TE'] = PERCENTILE_METRICS['WR']   # TEs share the receiver set
PERCENTILE_METRICS['LB'] = PERCENTILE_METRICS['DL']   # LBs share the front-seven set

# National-rank keys still shown on the season-stat header cards (rank only, not
# percentile). Kept in lockstep with the labels player.html maps to each key.
_RANK_SPECS = {
    'QB': [('pass_yds_rank','passing','YDS',True), ('pass_td_rank','passing','TD',True),
           ('pct_rank','passing','PCT',True), ('ypa_rank','passing','YPA',True),
           ('int_rank','passing','INT',False), ('epa_rank','ppa','avg_ppa_all',True)],
    'RB': [('rush_yds_rank','rushing','YDS',True), ('rush_td_rank','rushing','TD',True),
           ('ypc_rank','rushing','YPC',True), ('rec_yds_rank','receiving','YDS',True),
           ('epa_rank','ppa','avg_ppa_all',True)],
    'WR': [('rec_yds_rank','receiving','YDS',True), ('rec_td_rank','receiving','TD',True),
           ('rec_rank','receiving','REC',True), ('ypr_rank','receiving','YPR',True),
           ('epa_rank','ppa','avg_ppa_all',True)],
    'DL': [('tackles_rank','defensive','TOT',True), ('sacks_rank','defensive','SACKS',True)],
    'DB': [('tackles_rank','defensive','TOT',True), ('sacks_rank','defensive','SACKS',True)],
}
_RANK_SPECS['TE'] = _RANK_SPECS['WR']
_RANK_SPECS['LB'] = _RANK_SPECS['DL']

# Peer positions pooled for each qualification group (the "vs FBS <group>s" set).
_GROUP_POSITIONS = {
    'QB': ['QB'],
    'RB': ['RB','HB','FB'],
    'WR': ['WR','TE'], 'TE': ['WR','TE'],
    'DL': ['DE','DT','NT','DL','EDGE'],
    'LB': ['LB','ILB','OLB','MLB'],
    'DB': ['CB','S','SS','FS','SAF','DB'],
}
# Qualification group -> which metric set it uses (TE→WR, LB→DL share sets).
METRIC_GROUP = {'QB':'QB','RB':'RB','WR':'WR','TE':'TE','DL':'DL','LB':'LB','DB':'DB'}
# The counting-stat category that gates qualification for each group.
_PRIMARY_CATEGORY = {'QB':'passing','RB':'rushing','WR':'receiving','TE':'receiving',
                     'DL':'defensive','LB':'defensive','DB':'defensive'}


def _build_percentiles(cursor, player_id, pos, season=CURRENT_SEASON):
    """Assemble the player-page percentile rankings for one player.

    Returns (national_ranks, percentile_rows, group_name, peer_count):
      • national_ranks — {rank_key: national rank} for the season-stat cards.
      • percentile_rows — ordered [{'label', 'pct'}] for the percentile bars,
        advanced-first, only metrics the player actually qualifies for.
      • group_name — e.g. 'QB', 'WR', 'DL' (for the "vs FBS <group>s" header).
      • peer_count — size of the qualified peer pool.

    Every group is handled (offense and defense). Counting stats are zero-filled
    across the qualified pool so a player with none of a stat (a DL with no
    sacks) ranks at the bottom of qualified peers rather than dropping out of the
    pool — which would overstate everyone who recorded one. EPA and usage are not
    zero-filled: absence there means the player is outside that data, not a zero.
    """
    qgroup = POS_GROUP_MAP.get(pos)
    mgroup = METRIC_GROUP.get(qgroup) if qgroup else None
    if not mgroup:
        return {}, [], None, 0

    gp = _GROUP_POSITIONS[qgroup]
    specs = PERCENTILE_METRICS[mgroup]
    rank_specs = _RANK_SPECS.get(mgroup, [])
    needed = {s for _, s, _, _ in specs} | {s for _, s, _, _ in rank_specs}

    # Qualify against the group's primary counting stat (playing-time proxy).
    primary_cat = _PRIMARY_CATEGORY[qgroup]
    qstat, qmin = _qual_threshold(qgroup, primary_cat)
    primary_raw = _fetch_stats_pool(cursor, primary_cat, gp, season)
    qualified = {pid for pid, d in primary_raw.items() if (d.get(qstat) or 0) >= qmin}

    # Build each referenced source, restricted to the qualified peer set.
    # (pool, fill): fill=True zero-fills a missing stat to 0 before ranking.
    sources = {}
    for src in needed:
        if src in ('passing', 'rushing', 'receiving', 'defensive'):
            raw = primary_raw if src == primary_cat else _fetch_stats_pool(cursor, src, gp, season)
            sources[src] = ({pid: raw.get(pid, {}) for pid in qualified}, True)
        elif src == 'ppa':
            raw = _fetch_ppa_pool(cursor, gp, season)
            sources['ppa'] = ({pid: raw[pid] for pid in qualified if pid in raw}, False)
        elif src == 'usage':
            raw = _fetch_usage_pool(cursor, gp, season)
            sources['usage'] = ({pid: raw[pid] for pid in qualified if pid in raw}, False)
        elif src == 'ints':
            raw = _fetch_stats_pool(cursor, 'interceptions', gp, season)
            sources['ints'] = ({pid: {'INT': (raw.get(pid, {}).get('INT') or 0)}
                                for pid in qualified}, False)

    def rank_one(src, stat_key, hb):
        entry = sources.get(src)
        if not entry:
            return None, None, 0
        pool, fill = entry
        if fill:
            pool = {pid: {stat_key: (d.get(stat_key) or 0)} for pid, d in pool.items()}
        return _rank_pct(player_id, pool, stat_key, hb)

    national = {}
    for key, src, stat_key, hb in rank_specs:
        r, _, _ = rank_one(src, stat_key, hb)
        if r is not None:
            national[key] = r

    rows, peer = [], 0
    for label, src, stat_key, hb in specs:
        _, p, n = rank_one(src, stat_key, hb)
        if p is not None:
            rows.append({'label': label, 'pct': p})
            peer = max(peer, n)

    return national, rows, qgroup, peer


def get_rivalry(cursor, team1, team2):
    cursor.execute(
        'SELECT rivalry_name FROM rivalries WHERE team1=%s AND team2=%s LIMIT 1',
        (team1, team2)
    )
    row = cursor.fetchone()
    return row[0] if row else None

def get_rivalry_map(cursor):
    """One query for the whole rivalries table, looked up in Python afterward.
    Avoids the N+1 pattern of calling get_rivalry() once per game in a loop
    (used by the home page's game list and a team's full schedule)."""
    cursor.execute('SELECT team1, team2, rivalry_name FROM rivalries')
    return {(t1, t2): name for t1, t2, name in cursor.fetchall()}

def get_frozen_forecasts(cursor, game_ids):
    """Frozen pre-kickoff Savant Forecast + graded outcome for completed games
    that carry a scored prediction — the SAME rows the upset badge and accuracy
    tracker read (scored=1, never rewritten by predict_games.py), so a completed
    game's displayed forecast never changes after the fact.

    Returns {game_id: {home_prob, favorite, fav_prob, margin, correct,
    is_upset}}. Games without a stored prediction (pre-Forecast seasons, FCS
    matchups) are simply absent — callers render nothing for them."""
    if not game_ids:
        return {}
    try:
        cursor.execute('''
            SELECT game_id, home_team, away_team, home_prob, predicted_margin, correct
            FROM game_predictions WHERE game_id = ANY(%s) AND scored = 1
        ''', (list(game_ids),))
    except Exception:
        cursor.connection.rollback()   # game_predictions absent on a fresh DB
        return {}
    out = {}
    for gid, home, away, prob, margin, correct in cursor.fetchall():
        if prob is None:
            continue
        home_fav = prob >= 0.5
        out[gid] = {
            'home_prob': prob,
            'favorite': home if home_fav else away,
            'fav_prob': prob if home_fav else 1 - prob,
            'margin': margin,
            'correct': bool(correct),
            'is_upset': not correct,
        }
    return out


def get_game_label(notes):
    if not notes: return 'Bowl Games'
    if 'National Championship' in notes: return 'National Championship'
    if 'Semifinal' in notes: return 'Semifinal'
    if 'Quarterfinal' in notes: return 'Quarterfinal'
    if 'First Round' in notes: return 'First Round'
    if 'Conference Championship' in notes: return 'Conference Championships'
    return 'Bowl Games'

# Lineup position groups. OL excludes NT (a defensive nose tackle).
_LINEUP_OL    = {'OL','OT','OG','G','C','LT','LG','RG','RT'}
_LINEUP_SKILL = {'QB','RB','HB','FB','WR','TE','ATH','APB'}

def compute_starter_scores(cursor, roster, use_ea=True):
    """Return {player_id(str): score} used to pick lineup starters.

    `use_ea` toggles the EA-rating supplement (default on); pass False for a
    production-only baseline or as a kill-switch.

    Starters are chosen by real 2025 signal, not roster order or jersey.
    Every lookup keys on the stable player_id rather than the current team,
    so a player who transferred in for 2026 is still scored on the
    production he recorded at his prior team (the same id-based attribution
    the player page and leaderboards already rely on):

      • skill offense (QB/RB/WR/TE) -> player_usage.overall, the share of
        team plays the player was involved in — the closest available proxy
        for snap count, which CFBD does not provide. Total scrimmage yards
        break ties / cover the rare skill player with no usage row.
      • defense (DL/DE/DT/LB/CB/S/DB) -> a weighted production score built
        from tackles (the volume signal for a full-time defender) plus
        splash plays: TOT + 2·SACKS + 1.5·TFL + 1.5·PD + 3·INT + 0.5·QB HUR.
      • offensive line -> no individual OL production exists in any
        integrated data source (no snap counts, and linemen accrue no
        box-score stats), so OL falls back to seniority (class year), with
        jersey as a deterministic tiebreak. This is a documented proxy, not
        a production measure — the set of five is reasonable but the
        specific LT/LG/C/RG/RT labels are not individually verifiable.

    EA Sports College Football 27 overall rating (fetch_ea_ratings.py) is the
    SOLE signal for rated players: a rated player's score is exactly his EA
    overall, so at every position the higher EA grade always starts and
    production never reorders rated players. The 2025 production above is used
    only to rank players who have NO EA rating (they sit below the rated pool
    and start only where a position has no rated player at all). EA ratings are
    internal-only and never surfaced in the UI (licensed/proprietary data); a
    missing/unpopulated ea_ratings table degrades to production-only scoring.
    """
    ids = [str(p[4]) for p in roster if p[4] is not None]
    int_ids = [int(p[4]) for p in roster if p[4] is not None]
    if not ids:
        return {}

    # Production signals come from the most recent completed season — the
    # lineup is a forward projection, so pin these reads to CURRENT_SEASON
    # (historical backfill must not shift starter scores).
    usage = {}
    cursor.execute('SELECT player_id, overall FROM player_usage WHERE player_id = ANY(%s) AND season=%s',
                   (int_ids, CURRENT_SEASON))
    for pid, overall in cursor.fetchall():
        usage[str(pid)] = float(overall or 0)

    dstat = {}
    cursor.execute('''
        SELECT player_id, stat_type, MAX(CAST(stat AS REAL))
        FROM player_stats
        WHERE player_id = ANY(%s) AND season = %s AND category IN ('defensive','interceptions')
        GROUP BY player_id, stat_type
    ''', (ids, CURRENT_SEASON))
    for pid, st, val in cursor.fetchall():
        dstat.setdefault(pid, {})[st] = val or 0

    yds = {}
    cursor.execute('''
        SELECT player_id, SUM(CAST(stat AS REAL))
        FROM player_stats
        WHERE player_id = ANY(%s) AND season = %s
          AND category IN ('passing','rushing','receiving') AND stat_type = 'YDS'
        GROUP BY player_id
    ''', (ids, CURRENT_SEASON))
    for pid, total in cursor.fetchall():
        yds[str(pid)] = float(total or 0)

    # EA Sports College Football 27 overall rating, matched to our players by
    # name+team at ingest (see fetch_ea_ratings.py) — the PRIMARY talent signal.
    # Internal-only, never displayed. Table may not exist on a fresh DB, so a
    # missing table degrades gracefully to production-only scoring.
    ea = {}
    if use_ea:
        try:
            cursor.execute('SELECT player_id, overall FROM ea_ratings '
                           'WHERE player_id = ANY(%s) AND overall IS NOT NULL', (int_ids,))
            for pid, ovr in cursor.fetchall():
                ea[str(pid)] = float(ovr)
        except Exception:
            cursor.connection.rollback()  # ea_ratings not populated yet

    # EA-only scoring. build_lineup() sorts each position pool independently, so
    # a score only has to order players *within* a position group. A rated
    # player's score is exactly his EA overall (0–99) — production never reorders
    # rated players, so the higher EA grade always starts. Players with no EA
    # rating fall back to a production-ranked band (UNRATED_BASE …
    # UNRATED_BASE+UNRATED_SPREAD) that sits below the rated pool, so they start
    # only where a position has no rated player at all.
    UNRATED_BASE = 48.0     # unrated players rank here (EA ratings run 47–99)…
    UNRATED_SPREAD = 22.0   # …ordered among themselves by production (48–70)

    year_rank = {'4': 4, '3': 3, '2': 2, '1': 1}

    def _quality(p, pid, pos):
        """0..1 production/quality signal for a player, by position group. Used
        as the within-group tiebreak for rated players and the ranking signal
        for unrated ones."""
        if pos in _LINEUP_OL:
            # No individual OL box score exists in any source — seniority
            # (class year) is the only available proxy.
            return min(1.0, year_rank.get(str(p[8]), 0) / 4.0)
        if pos in _LINEUP_SKILL:
            # Usage share of team plays is the snap-count proxy; scrimmage
            # yards nudge ties and cover players with no usage row.
            u = usage.get(pid, 0.0)
            y = yds.get(pid, 0.0)
            return min(1.0, u + min(y, 4000.0) / 4000.0 * 0.15)
        # defense: tackle-weighted production, squashed into 0..1
        d = dstat.get(pid, {})
        s = (d.get('TOT', 0) + 2 * d.get('SACKS', 0) + 1.5 * d.get('TFL', 0)
             + 1.5 * d.get('PD', 0) + 3 * d.get('INT', 0) + 0.5 * d.get('QB HUR', 0))
        return s / (s + 35.0)

    scores = {}
    for p in roster:
        if p[4] is None:
            continue
        pid = str(p[4])
        pos = (p[2] or '').upper()
        jersey = int(p[3]) if str(p[3]).isdigit() else 99
        q = max(0.0, min(1.0, _quality(p, pid, pos)))
        ovr = ea.get(pid)
        if ovr is not None:
            base = ovr                                    # rated players ordered purely by EA overall
        else:
            base = UNRATED_BASE + UNRATED_SPREAD * q      # no EA → ranked by production, below the rated pool
        scores[pid] = base + (99 - min(jersey, 99)) * 1e-4  # deterministic final tiebreak
    return scores


def build_lineup(roster, starter_scores=None, ea_pos=None):
    """Slot the highest-scoring available player into each formation spot.
    `starter_scores` comes from compute_starter_scores(); absent, everyone
    scores 0 and slots fall back to roster order.

    `ea_pos` maps player_id(str) -> the player's specific EA Sports position
    (LT/LG/C/RG/RT, LE/RE/DT, MLB/LOLB/ROLB, CB/FS/SS, HB, …). When present it
    is preferred over the coarser roster position so, e.g., a rated left tackle
    lands at LT rather than being poured into the first open interior slot."""
    if starter_scores is None:
        starter_scores = {}
    ea_pos = ea_pos or {}

    # Each slot has its exact EA position(s) as PRIMARY, then broader fallbacks
    # for rosters/players without a specific EA position. Two-phase fill below
    # means a player only cross-fills another spot once every dedicated player
    # has claimed his own — so a left tackle is never grabbed to play center.
    slot_primary = {
        'QB': ['QB'], 'RB': ['HB','RB','FB','APB','ATH'],
        'WR1': ['WR'], 'WR2': ['WR'], 'WR3': ['WR'], 'TE': ['TE'],
        'LT': ['LT'], 'LG': ['LG'], 'C': ['C'], 'RG': ['RG'], 'RT': ['RT'],
        'DE1': ['LE','DE','EDGE'], 'DE2': ['RE','DE','EDGE'],
        'DT1': ['DT','NT'], 'DT2': ['DT','NT'],
        'LB1': ['MLB','ILB'], 'LB2': ['LOLB','OLB','LB'], 'LB3': ['ROLB','OLB','LB'],
        'CB1': ['CB'], 'CB2': ['CB'],
        'S1': ['FS','S','SAF'], 'S2': ['SS','S','SAF'],
    }
    slot_fallback = {
        'QB': [], 'RB': [], 'WR1': ['ATH'], 'WR2': ['ATH'], 'WR3': ['ATH'], 'TE': [],
        'LT': ['OT','OL','OG','G','RT','LG','RG','C'],
        'LG': ['OG','G','OL','OT','RG','C','LT','RT'],
        'C':  ['OL','OG','G','LG','RG','LT','RT','OT'],
        'RG': ['OG','G','OL','OT','LG','C','RT','LT'],
        'RT': ['OT','OL','OG','G','LT','RG','LG','C'],
        'DE1': ['DL','RE','DT'], 'DE2': ['DL','LE','DT'],
        'DT1': ['DL','DE','EDGE'], 'DT2': ['DL','DE','EDGE'],
        'LB1': ['LB','OLB','LOLB','ROLB'], 'LB2': ['ILB','MLB','ROLB'], 'LB3': ['ILB','MLB','LOLB'],
        'CB1': ['DB','FS','SS','S'], 'CB2': ['DB','SS','FS','S'],
        'S1': ['DB','SS','CB'], 'S2': ['DB','FS','CB'],
    }

    # Pool players by EFFECTIVE position: the specific EA position when known,
    # otherwise the roster position.
    pos_pool = {}
    for player in roster:
        first, last, pos, jersey, pid, headshot = player[0], player[1], player[2], player[3], player[4], player[5]
        if not pos: continue
        eff = (ea_pos.get(str(pid)) or pos).upper()
        pos_pool.setdefault(eff, []).append({
            'idx': pid, 'name': last, 'first': first, 'jersey': jersey or '',
            'pos': pos, 'headshot': headshot, 'score': starter_scores.get(str(pid), 0)})
    for pool in pos_pool.values():
        pool.sort(key=lambda x: x['score'], reverse=True)

    fill_order = ['QB','RB','WR1','WR2','WR3','TE','LT','LG','C','RG','RT',
                  'DE1','DE2','DT1','DT2','LB1','LB2','LB3','CB1','CB2','S1','S2']
    lineup, used = {}, set()

    def _try_fill(slot, pos_types):
        for pos_type in pos_types:
            for player in pos_pool.get(pos_type, []):
                if player['idx'] not in used:
                    lineup[slot] = player
                    used.add(player['idx'])
                    return True
        return False

    # Phase 1: every slot claims a player at its exact position first.
    for slot in fill_order:
        _try_fill(slot, slot_primary[slot])
    # Phase 2: fill anything still open from the broader fallback pools.
    for slot in fill_order:
        if slot not in lineup:
            _try_fill(slot, slot_primary[slot] + slot_fallback[slot])
    return lineup

def pivot_stats(raw_stats):
    result = {}
    for player_name, category, stat_type, stat in raw_stats:
        if category not in result: result[category] = {}
        if player_name not in result[category]: result[category][player_name] = {}
        result[category][player_name][stat_type] = stat
    return result

def compute_percentiles(all_teams_stats, team_name):
    """For each metric, compute what percentile this team falls in across all FBS teams."""
    
    # Metrics where HIGHER = better (offense)
    higher_better = [
        'off_ppa', 'off_success_rate', 'off_explosiveness', 'off_power_success',
        'off_line_yards', 'off_second_level_yards', 'off_open_field_yards',
        'off_rush_ppa', 'off_pass_ppa', 'off_rush_sr', 'off_pass_sr',
        'off_rush_exp', 'off_pass_exp',
    ]
    # Offense where LOWER = better
    lower_better_off = ['off_stuff_rate']

    # Defense where LOWER = better (opponent getting less = good)
    lower_better_def = [
        'def_ppa', 'def_success_rate', 'def_explosiveness', 'def_power_success',
        'def_line_yards', 'def_second_level_yards', 'def_open_field_yards',
        'def_rush_ppa', 'def_pass_ppa', 'def_rush_sr', 'def_pass_sr',
        'def_rush_exp', 'def_pass_exp',
    ]
    # Defense where HIGHER = better
    higher_better_def = ['def_stuff_rate']

    # Column index map matching team_stats table
    col_map = {
        'off_ppa': 3, 'off_success_rate': 5, 'off_explosiveness': 6,
        'off_power_success': 7, 'off_stuff_rate': 8, 'off_line_yards': 9,
        'off_open_field_yards': 10, 'off_second_level_yards': 11,
        'off_rush_ppa': 12, 'off_pass_ppa': 13, 'off_rush_sr': 14,
        'off_pass_sr': 15, 'off_rush_exp': 16, 'off_pass_exp': 17,
        'def_ppa': 20, 'def_success_rate': 22, 'def_explosiveness': 23,
        'def_power_success': 24, 'def_stuff_rate': 25, 'def_line_yards': 26,
        'def_open_field_yards': 27, 'def_second_level_yards': 28,
        'def_rush_ppa': 29, 'def_pass_ppa': 30, 'def_rush_sr': 31,
        'def_pass_sr': 32, 'def_rush_exp': 33, 'def_pass_exp': 34,
    }

    # Find this team's row
    team_row = next((r for r in all_teams_stats if r[0] == team_name), None)
    if not team_row:
        return {}

    percentiles = {}
    all_metrics = higher_better + lower_better_off + lower_better_def + higher_better_def

    for metric in all_metrics:
        idx = col_map[metric]
        team_val = team_row[idx]
        if team_val is None:
            percentiles[metric] = None
            continue

        # Get all non-null values for this metric
        all_vals = [r[idx] for r in all_teams_stats if r[idx] is not None]
        if not all_vals:
            percentiles[metric] = None
            continue

        # Count how many teams this team beats
        if metric in higher_better or metric in higher_better_def:
            rank = sum(1 for v in all_vals if v < team_val)
        else:  # lower is better
            rank = sum(1 for v in all_vals if v > team_val)

        pct = round((rank / len(all_vals)) * 100)
        # Clamp to 1-99
        pct = max(1, min(99, pct))
        percentiles[metric] = pct

    return percentiles

def compute_havoc_field_pos_percentiles(all_teams_advanced, team_name):
    """Percentiles for havoc rate and starting field position, from team_advanced.
    Keyed by dict (column name -> value) rather than positional index, unlike
    compute_percentiles() above, since these rows come from a plain SELECT *.

    field_pos_avg_start is stored as yards-to-go (100 - yard line), not the yard
    line itself — values cluster ~64-75. So for the offense, LOWER is better
    (fewer yards to travel); for the defense (opponent's yards-to-go against
    this team), HIGHER is better (pinning opponents back further)."""
    higher_better = ['def_havoc_total', 'def_havoc_front7', 'def_havoc_db', 'def_field_pos_avg_start']
    lower_better = ['off_field_pos_avg_start']

    team_row = all_teams_advanced.get(team_name)
    if not team_row:
        return {}

    percentiles = {}
    for metric in higher_better + lower_better:
        team_val = team_row.get(metric)
        if team_val is None:
            percentiles[metric] = None
            continue
        all_vals = [row[metric] for row in all_teams_advanced.values() if row.get(metric) is not None]
        if not all_vals:
            percentiles[metric] = None
            continue
        if metric in higher_better:
            rank = sum(1 for v in all_vals if v < team_val)
        else:
            rank = sum(1 for v in all_vals if v > team_val)
        pct = round((rank / len(all_vals)) * 100)
        percentiles[metric] = max(1, min(99, pct))

    return percentiles


@cache.memoize(timeout=21600)
def _team_percentiles_all(season):
    """Every team's merged advanced-stat percentiles for a season: the
    team_stats-based metrics (compute_percentiles) plus the havoc/field-position
    metrics (compute_havoc_field_pos_percentiles), keyed by team name.

    Ranking one team's metrics requires the whole FBS field, so the two
    all-teams SELECTs are done once here and every team's percentiles derived
    from them — far cheaper than the team route re-fetching the field on each
    request. Precomputed weekly into pool_store (key `teampct:{season}`) by
    precompute.py; on a miss it computes live and writes back (self-healing)."""
    key = f"teampct:{season}"
    stored = _pool_store_get(key)
    if stored is not None:
        return stored
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT * FROM team_stats WHERE season=%s', (season,))
        all_teams_stats = cur.fetchall()
        cur.execute('SELECT * FROM team_advanced WHERE season=%s', (season,))
        adv_cols = [d[0] for d in cur.description]
        all_teams_advanced = {row[0]: dict(zip(adv_cols, row)) for row in cur.fetchall()}
    finally:
        release_db(conn)
    result = {}
    for t in {r[0] for r in all_teams_stats} | set(all_teams_advanced):
        pct = compute_percentiles(all_teams_stats, t)
        pct.update(compute_havoc_field_pos_percentiles(all_teams_advanced, t))
        result[t] = pct
    _pool_store_put(key, season, result)
    return result

def sort_players(cat_dict, sort_key, min_val=0):
    players = []
    for name, stats in cat_dict.items():
        val = float(stats.get(sort_key, 0) or 0)
        if val > min_val:
            players.append({'name': name, **stats})
    return sorted(players, key=lambda x: float(x.get(sort_key, 0) or 0), reverse=True)

# Non-FBS conferences excluded from FBS-only pages. Includes the CFBD labels
# used by the FCS opponents added for logo display in fetch_fcs_logos.py —
# notably 'Southern' (SoCon), 'Big South-OVC' and 'UAC', which must be listed
# here so those teams don't leak onto the Teams grid / Rankings / Leaderboards /
# search. 'SIAC' is Division II (Savannah St, a historical FBS opponent) — not
# FCS by name, but non-FBS and excluded on the same basis.
FCS_CONFS = ('CAA','Big Sky','MVFC','SWAC','MEAC','Southland','Big South','OVC',
             'Big South-OVC','Southern','UAC','Patriot','NEC','Pioneer','Ivy',
             'FCS Independents','SIAC')

LEADERBOARD_PER_PAGE = 25

# ── Leaderboard column definitions ──────────────────────────────────────────
# Each entry: (key, label, tooltip, sortable, format).
# format drives both cell rendering and value coercion in the template:
#   int, float1, float2, pct1  — plain numeric formatting
#   epa                        — color-coded pill, positive green / negative red
#   epa_inv                    — same pill, colors flipped (negative is good — defense)
#   na                         — column isn't backed by real data; header renders
#                                 greyed out with a "not available" tooltip and every
#                                 cell renders "—". Not sortable, never selected in SQL.
#
# A handful of stats the spec asks for genuinely don't exist in this dataset
# (targets, air yards, YAC/ADOT, snap counts, forced fumbles, defensive
# pass/rush splits, games-played) — those are marked 'na' rather than faked.
PLAYER_COLUMNS = {
    'passing': {
        'standard': [
            ('cmp', 'CMP',  'Completions', True, 'int'),
            ('att', 'ATT',  'Attempts', True, 'int'),
            ('pct', 'CMP%', 'Completion Percentage', True, 'pct1'),
            ('yds', 'YDS',  'Passing Yards', True, 'int'),
            ('td',  'TD',   'Passing Touchdowns', True, 'int'),
            ('int', 'INT',  'Interceptions', True, 'int'),
            ('ypa', 'YPA',  'Yards Per Attempt', True, 'float1'),
            ('rtg', 'RTG',  'Passer Rating — standard NCAA formula', True, 'float1'),
        ],
        'advanced': [
            ('epa_pass',  'EPA/P',  'EPA Per Play (passing)', True, 'epa'),
            ('total_epa', 'PPA',    'Total Predicted Points Added', True, 'float1'),
            ('adj_ypa',   'ADJ YPA','Adjusted Yards Per Attempt — (YDS + 20×TD − 45×INT) / ATT', True, 'float1'),
            ('sack_pct',  'SACK%',  'Sack Percentage — not available in current dataset', False, 'na'),
        ],
    },
    'rushing': {
        'standard': [
            ('att',  'CAR',  'Carries', True, 'int'),
            ('yds',  'YDS',  'Rushing Yards', True, 'int'),
            ('ypc',  'YPC',  'Yards Per Carry', True, 'float1'),
            ('td',   'TD',   'Rushing Touchdowns', True, 'int'),
            ('fum',  'FUM',  'Fumbles', True, 'int'),
            ('long', 'LONG', 'Longest Run', True, 'int'),
            ('ypg',  'Y/G',  'Rushing Yards Per Game — not available (requires games played)', False, 'na'),
        ],
        'advanced': [
            ('epa_rush',  'EPA/R', 'EPA Per Rush', True, 'epa'),
            ('total_epa', 'PPA',   'Total Predicted Points Added', True, 'float1'),
            ('usage',     'USG%',  'Rush Usage Rate — share of team rush plays', True, 'pct1'),
            ('exp_pct',   'EXP%',  'Explosive Run Rate (≥10 yds) — not available in current dataset', False, 'na'),
        ],
    },
    'receiving': {
        'standard': [
            ('rec',     'REC',  'Receptions', True, 'int'),
            ('tgt',     'TGT',  'Targets — not available in current dataset', False, 'na'),
            ('yds',     'YDS',  'Receiving Yards', True, 'int'),
            ('td',      'TD',   'Receiving Touchdowns', True, 'int'),
            ('ypr',     'YPR',  'Yards Per Reception', True, 'float1'),
            ('cth_pct', 'CTH%', 'Catch Rate (REC/TGT) — not available (requires targets)', False, 'na'),
            ('ypg',     'Y/G',  'Receiving Yards Per Game — not available (requires games played)', False, 'na'),
        ],
        'advanced': [
            ('epa_play',  'EPA/T', 'EPA Per Target/Play', True, 'epa'),
            ('total_epa', 'PPA',   'Total Predicted Points Added', True, 'float1'),
            ('tgt_pct',   'TGT%',  'Target Share — not available in current dataset', False, 'na'),
        ],
    },
    'defense': {
        'standard': [
            ('tot',   'TOT',  'Total Tackles', True, 'int'),
            ('solo',  'SOLO', 'Solo Tackles', True, 'int'),
            ('ast',   'AST',  'Assisted Tackles (TOT − SOLO)', True, 'int'),
            ('tfl',   'TFL',  'Tackles For Loss', True, 'float1'),
            ('sacks', 'SCK',  'Sacks', True, 'float1'),
            ('pd',    'PBU',  'Pass Breakups', True, 'int'),
            ('ff',    'FF',   'Forced Fumbles — not available in current dataset', False, 'na'),
            ('int',   'INT',  'Interceptions', True, 'int'),
            ('td',    'TD',   'Defensive Touchdowns', True, 'int'),
        ],
        'advanced': [
            ('tkl_pct',  'TKL%',  'Share of team total tackles', True, 'pct1'),
            ('epa_play', 'EPA/P', 'Defensive EPA Per Play — not available in current dataset (PPA data only tracks offensive skill positions)', False, 'na'),
            ('prsh',     'PRSH',  'Pass Rush Win Rate — not available in current dataset', False, 'na'),
        ],
    },
}

# Position-group buckets for the filter-bar dropdown (maps a group label to
# the underlying `players.position` values it covers).
POSITION_GROUPS = {
    'QB': ('QB',),
    'RB': ('RB', 'FB'),
    'WR': ('WR',),
    'TE': ('TE',),
    'DL': ('DE', 'DT', 'NT', 'DL', 'EDGE'),
    'LB': ('LB',),
    'DB': ('CB', 'S', 'DB'),
}

TEAM_COLUMNS = {
    'offense': {
        'standard': [
            ('off_ppa', 'PPA', 'Avg Predicted Points Added Per Play', True, 'epa'),
            ('off_success_rate', 'SCR%', 'Offensive Success Rate', True, 'pct1'),
            ('off_explosiveness', 'EXP', 'Explosiveness', True, 'float2'),
            ('off_power_success', 'PWR', 'Power Success Rate', True, 'pct1'),
            ('off_line_yards', 'LINE', 'Line Yards Per Rush', True, 'float2'),
            ('off_second_level_yards', '2ND', 'Second Level Yards', True, 'float2'),
            ('off_open_field_yards', 'OPN', 'Open Field Yards', True, 'float2'),
        ],
        'advanced': [
            ('off_passing_plays_ppa', 'PPAP', 'Pass PPA Per Play', True, 'epa'),
            ('off_passing_success_rate', 'PSCR', 'Pass Success Rate', True, 'pct1'),
            ('off_passing_explosiveness', 'PEXP', 'Pass Explosiveness', True, 'float2'),
            ('off_rushing_plays_ppa', 'RPPA', 'Rush PPA Per Play', True, 'epa'),
            ('off_rushing_success_rate', 'RSCR', 'Rush Success Rate', True, 'pct1'),
            ('off_rushing_explosiveness', 'REXP', 'Rush Explosiveness', True, 'float2'),
            ('off_scoring_opps', 'SCR.OPP', 'Scoring Opportunities', True, 'int'),
            ('off_pts_per_opp', 'PTS/OPP', 'Points Per Scoring Opportunity', True, 'float2'),
            ('off_field_pos_avg_start', 'AVG.ST', 'Avg Starting Field Position (yard line)', True, 'float1'),
        ],
    },
    'defense': {
        'standard': [
            ('def_ppa', 'PPA', 'Avg Predicted Points Added Per Play Allowed — lower is better', True, 'epa_inv'),
            ('def_success_rate', 'SCR%', 'Success Rate Allowed — lower is better', True, 'pct1'),
            ('def_explosiveness', 'EXP', 'Explosiveness Allowed — lower is better', True, 'float2'),
            ('def_stuff_rate', 'STF', 'Stuff Rate — higher is better', True, 'pct1'),
            ('def_line_yards', 'LINE', 'Line Yards Allowed Per Rush — lower is better', True, 'float2'),
            ('def_second_level_yards', '2ND', 'Second Level Yards Allowed — lower is better', True, 'float2'),
            ('def_open_field_yards', 'OPN', 'Open Field Yards Allowed — lower is better', True, 'float2'),
        ],
        'advanced': [
            ('def_passing_plays_ppa', 'PPAP', 'Pass PPA Allowed — not available in current dataset', False, 'na'),
            ('def_passing_success_rate', 'PSCR', 'Pass Success Rate Allowed — not available in current dataset', False, 'na'),
            ('def_passing_explosiveness', 'PEXP', 'Pass Explosiveness Allowed — not available in current dataset', False, 'na'),
            ('def_rushing_plays_ppa', 'RPPA', 'Rush PPA Allowed — not available in current dataset', False, 'na'),
            ('def_rushing_success_rate', 'RSCR', 'Rush Success Rate Allowed — not available in current dataset', False, 'na'),
            ('def_rushing_explosiveness', 'REXP', 'Rush Explosiveness Allowed — not available in current dataset', False, 'na'),
            ('def_havoc_total', 'HVC', 'Total Havoc Rate', True, 'pct1'),
            ('def_havoc_front7', 'HVF7', 'Front 7 Havoc Rate', True, 'pct1'),
            ('def_havoc_db', 'HVDB', 'Defensive Back Havoc Rate', True, 'pct1'),
        ],
    },
    'sp': {
        'standard': [
            ('rating', 'SP+', 'Overall SP+ Rating', True, 'float1'),
            ('offense_rating', 'O.SP+', 'Offensive SP+ Rating', True, 'float1'),
            ('defense_rating', 'D.SP+', 'Defensive SP+ Rating', True, 'float1'),
            ('special_teams_rating', 'ST.SP+', 'Special Teams SP+ Rating', True, 'float1'),
            ('ranking', 'SP.RNK', 'SP+ National Ranking', True, 'int'),
        ],
        'advanced': [],  # no additional split for SP+ — Standard is the full picture
    },
    'savant': {
        'standard': [
            ('net_rating', 'NET', 'Savant Net Rating — expected scoring margin per 10 drives vs an average FBS team on a neutral field', True, 'float1'),
            ('off_rating', 'OFF', 'Savant Offensive Rating — opponent-adjusted points scored per 10 drives', True, 'float1'),
            ('def_rating', 'DEF', 'Savant Defensive Rating — opponent-adjusted points allowed per 10 drives. Lower is better', True, 'float1'),
            ('sos', 'SOS', 'Strength of Schedule — drive-weighted average opponent Net Rating', True, 'float1'),
            ('svr_games', 'GP', 'FBS-vs-FBS games in the rating sample', True, 'int'),
        ],
        'advanced': [
            ('raw_off', 'RAW.O', 'Unadjusted points scored per 10 drives (before opponent adjustment)', True, 'float1'),
            ('raw_def', 'RAW.D', 'Unadjusted points allowed per 10 drives — lower is better', True, 'float1'),
            ('drives_off', 'DRV.O', 'Countable offensive drives (garbage time, kneel-outs, and OT excluded)', True, 'int'),
            ('drives_def', 'DRV.D', 'Countable defensive drives', True, 'int'),
            ('net_ranking', 'NET.RK', 'Savant Net Rating national rank', True, 'int'),
        ],
    },
}

# Preferred default sort per (category, view) — falls back to the first
# sortable column if not listed here. Column order is chosen for readability
# (e.g. CMP before YDS, matching a real box score), which doesn't always
# match the stat a leaderboard should default-sort by (e.g. YDS, not CMP).
PLAYER_PREFERRED_SORT = {
    ('passing', 'standard'): 'yds', ('passing', 'advanced'): 'total_epa',
    ('rushing', 'standard'): 'yds', ('rushing', 'advanced'): 'total_epa',
    ('receiving', 'standard'): 'yds', ('receiving', 'advanced'): 'total_epa',
    ('defense', 'standard'): 'tot', ('defense', 'advanced'): 'tkl_pct',
}

def _default_sort_col(columns, category, view):
    """Fallback sort when the requested `sort` param isn't valid for the
    current view (e.g. switching Standard -> Advanced changes what's sortable)."""
    preferred = PLAYER_PREFERRED_SORT.get((category, view))
    if preferred:
        return preferred
    for key, _, _, sortable, _ in columns[category][view]:
        if sortable:
            return key
    return None

def _sortable_keys(columns, category, view):
    return {key for key, _, _, sortable, _ in columns[category][view] if sortable}

def _sort_and_paginate(rows, sort_col, sort_dir, page_raw):
    """Sort a list of dicts by `sort_col`, always pushing None values to the
    end regardless of direction (mirrors SQL's NULLS LAST) — needed because
    several leaderboard columns (RTG, TKL%, ADJ YPA, ...) are computed in
    Python from multiple joined sources and can't be ORDER BY'd in SQL."""
    reverse = sort_dir != 'asc'
    with_val = [r for r in rows if r.get(sort_col) is not None]
    without_val = [r for r in rows if r.get(sort_col) is None]
    with_val.sort(key=lambda r: r[sort_col], reverse=reverse)
    ordered = with_val + without_val
    page, offset, pagination = _pagination_ctx(page_raw, len(ordered))
    page_rows = ordered[offset:offset + LEADERBOARD_PER_PAGE]
    for i, r in enumerate(page_rows):
        r['rank'] = offset + i + 1
    return page_rows, pagination

def get_teams_by_conference(cursor):
    cursor.execute("SELECT name, conference FROM teams WHERE conference IS NOT NULL ORDER BY conference, name")
    out = OrderedDict()
    for name, conf in cursor.fetchall():
        if conf in FCS_CONFS:
            continue
        out.setdefault(conf, []).append(name)
    return out

def _pagination_ctx(page_raw, total_count):
    """Clamp the requested page against the real total and compute offset +
    display context. Returns (page, offset, ctx) — use `page`/`offset` for the
    SQL query, pass `ctx` straight to the template."""
    total_pages = max(1, -(-total_count // LEADERBOARD_PER_PAGE))  # ceil div
    try:
        page = int(page_raw)
    except (TypeError, ValueError):
        page = 1
    page = max(1, min(page, total_pages))
    offset = (page - 1) * LEADERBOARD_PER_PAGE
    start = offset + 1 if total_count > 0 else 0
    end = min(offset + LEADERBOARD_PER_PAGE, total_count)
    ctx = {
        'page': page, 'total_pages': total_pages, 'total_count': total_count,
        'per_page': LEADERBOARD_PER_PAGE, 'start': start, 'end': end,
    }
    return page, offset, ctx

def leaders_query_all(cursor, season=CURRENT_SEASON):
    """All home-page leader boards in one round trip. Ranks on the nine exact
    (category, stat_type) index ranges first — a few thousand narrow rows —
    and joins players/teams only for the ~45 winners, so no wide sort of the
    whole season ever happens.
    Returns {(category, stat_type): [(name, team, val, headshot, logo, pid)]}"""
    cursor.execute(f'''
        WITH ranked AS (
            SELECT category, stat_type, player_name, team, val, pid FROM (
                SELECT ps.category, ps.stat_type, ps.player_name, ps.team,
                       MAX(CAST(ps.stat AS REAL)) AS val,
                       MAX(ps.player_id) AS pid,
                       ROW_NUMBER() OVER (
                           PARTITION BY ps.category, ps.stat_type
                           ORDER BY MAX(CAST(ps.stat AS REAL)) DESC NULLS LAST) AS rn
                FROM player_stats ps
                WHERE ps.season = %s
                  AND ((ps.category = 'passing'       AND ps.stat_type IN ('YDS','TD'))
                    OR (ps.category = 'rushing'       AND ps.stat_type IN ('YDS','TD'))
                    OR (ps.category = 'receiving'     AND ps.stat_type IN ('YDS','TD'))
                    OR (ps.category = 'defensive'     AND ps.stat_type IN ('TOT','SACKS'))
                    OR (ps.category = 'interceptions' AND ps.stat_type = 'INT'))
                  AND ps.conference NOT IN {FCS_CONFS}
                GROUP BY ps.category, ps.stat_type, ps.player_name, ps.team
            ) x WHERE rn <= 5
        )
        SELECT r.category, r.stat_type, r.player_name, r.team,
               CAST(r.val AS INTEGER), p.headshot, t.logo_dark, p.id
        FROM ranked r
        INNER JOIN teams t ON r.team = t.name
        LEFT JOIN players p ON p.id::text = r.pid
        ORDER BY r.category, r.stat_type, r.val DESC
    ''', (season,))
    out = {}
    for cat, st, name, team, val, headshot, logo, pid in cursor.fetchall():
        out.setdefault((cat, st), []).append((name, team, val, headshot, logo, pid))
    return out

@cache.memoize(timeout=21600)
def get_cached_season_leaders():
    """Season-wide leaders are identical no matter which week the home page
    is showing, so this is memoized independently of the /week/<n>/<type>
    route — otherwise the same queries would re-run for every distinct
    week URL instead of being computed once per hour.

    Returns (label, leaderboard href, rows) per category, in the display order
    used by the home page sidebar. Stat values are colored via the --gold
    accent token in the template, so no per-category color is passed."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        boards = leaders_query_all(cursor)
        return [
            ('Passing Yards',   '/leaderboards/passing',   boards.get(('passing', 'YDS'), [])),
            ('Passing TDs',     '/leaderboards/passing',   boards.get(('passing', 'TD'), [])),
            ('Rushing Yards',   '/leaderboards/rushing',   boards.get(('rushing', 'YDS'), [])),
            ('Rushing TDs',     '/leaderboards/rushing',   boards.get(('rushing', 'TD'), [])),
            ('Receiving Yards', '/leaderboards/receiving', boards.get(('receiving', 'YDS'), [])),
            ('Receiving TDs',   '/leaderboards/receiving', boards.get(('receiving', 'TD'), [])),
            ('Tackles',         '/leaderboards/defense',   boards.get(('defensive', 'TOT'), [])),
            ('Sacks',           '/leaderboards/defense',   boards.get(('defensive', 'SACKS'), [])),
            ('Interceptions',   '/leaderboards/defense',   boards.get(('interceptions', 'INT'), [])),
        ]
    finally:
        release_db(conn)

def _ticker_game_label(notes):
    """Short status line for a ticker item — CFP rounds get named, everything
    else (bowls with sponsor-heavy names, regular season) just reads Final."""
    if notes:
        if 'National Championship' in notes: return 'CFP Championship'
        if 'Semifinal' in notes: return 'CFP Semifinal'
        if 'Quarterfinal' in notes: return 'CFP Quarterfinal'
        if 'First Round' in notes: return 'CFP First Round'
        if 'Conference Championship' in notes: return 'Conf Championship'
    return 'Final'

@cache.memoize(timeout=21600)
def get_ticker_data():
    """Sitewide scores ticker under the navbar: the most recent completed
    week, with postseason outranking regular season so the offseason shows
    playoff/bowl results instead of the last regular-season week."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT week, season_type FROM games
            WHERE completed = 1 AND season = %s
            ORDER BY CASE WHEN season_type = 'SeasonType.POSTSEASON' THEN 0 ELSE 1 END,
                     week DESC
            LIMIT 1
        ''', (CURRENT_SEASON,))
        row = cursor.fetchone()
        if not row:
            return None
        week, stype = row
        ranks = get_ap_rankings(cursor)
        cursor.execute('''
            SELECT g.away_team, g.away_points, g.home_team, g.home_points,
                   ta.abbreviation, th.abbreviation, ta.logo_dark, th.logo_dark,
                   g.id, g.notes
            FROM games g
            LEFT JOIN teams th ON g.home_team = th.name
            LEFT JOIN teams ta ON g.away_team = ta.name
            WHERE g.completed = 1 AND g.season = %s AND g.week = %s AND g.season_type = %s
            ORDER BY CASE WHEN g.notes LIKE '%%National Championship%%' THEN 1
                          WHEN g.notes LIKE '%%Semifinal%%' THEN 2
                          WHEN g.notes LIKE '%%Quarterfinal%%' THEN 3
                          WHEN g.notes LIKE '%%First Round%%' THEN 4
                          WHEN g.notes LIKE '%%Conference Championship%%' THEN 5
                          ELSE 6 END, g.notes, g.id
        ''', (CURRENT_SEASON, week, stype))
        games = []
        for away, apts, home, hpts, a_abbr, h_abbr, a_logo, h_logo, gid, notes in cursor.fetchall():
            games.append({
                'id': gid,
                'label': _ticker_game_label(notes),
                'away': {'abbr': a_abbr or away, 'pts': apts, 'logo': a_logo,
                         'rank': ranks.get(away), 'won': (apts or 0) > (hpts or 0)},
                'home': {'abbr': h_abbr or home, 'pts': hpts, 'logo': h_logo,
                         'rank': ranks.get(home), 'won': (hpts or 0) > (apts or 0)},
            })
        # Cap the ticker so it doesn't render all ~46 postseason games at once.
        # Postseason is already ordered by round (championship first); for a
        # regular week, surface ranked matchups first. Overflow is reachable via
        # the ticker's existing horizontal scroll.
        TICKER_MAX = 10
        if 'POSTSEASON' not in stype:
            def _relevance(g):
                present = [r for r in (g['away']['rank'], g['home']['rank']) if r]
                return min(present) if present else 999
            games.sort(key=_relevance)
        games = games[:TICKER_MAX]
        label = 'Postseason' if 'POSTSEASON' in stype else f'Week {week}'
        return {'label': label, 'games': games}
    finally:
        release_db(conn)

# Process start time, surfaced as an X-Boot response header. Two jobs:
# (1) externally PROVES that a deploy restarted the process — and since the
# page cache is in-process SimpleCache, a new boot time means a fresh, empty
# cache with zero manual steps; (2) makes deploy state visible when debugging.
BOOT_TIME = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

@app.after_request
def add_cache_headers(resp):
    resp.headers['X-Boot'] = BOOT_TIME
    # Fully public site — let browsers (and hover-prefetch) reuse HTML briefly
    # so back/forward and prefetched clicks render instantly. Data updates are
    # hourly at most, so 3 minutes of staleness is invisible.
    if request.method == 'GET' and resp.status_code == 200 \
            and resp.mimetype == 'text/html' and not request.path.startswith('/admin'):
        resp.cache_control.public = True
        resp.cache_control.max_age = 180
    return resp

@app.context_processor
def inject_seasons():
    return dict(current_season=CURRENT_SEASON, upcoming_season=UPCOMING_SEASON)

@app.context_processor
def inject_ticker():
    # A ticker failure should never take down page rendering
    try:
        # The scores ticker adds no value on the head-to-head compare tool, and
        # its game list distracts from that focused view — hide it there.
        if request.path.startswith('/compare'):
            return dict(ticker=None)
        return dict(ticker=get_ticker_data())
    except Exception:
        return dict(ticker=None)

# ── Shared game-card plumbing ───────────────────────────────────────────────
# Column list used by every route that renders the game_card component (home
# page + /games hub). Keep build_game_card() in sync with this order. t1 is the
# home team, t2 the away team.
GAME_CARD_SELECT = '''
    g.id, g.home_team, g.home_points, g.away_team, g.away_points,
    g.week, g.season_type, g.notes, t1.logo_dark, t2.logo_dark,
    g.completed, g.start_date, COALESCE(g.start_time_tbd, 0),
    t1.color, t2.color
'''

def format_kickoff(start_date_raw, time_tbd):
    """Format a stored (UTC) start_date as an Eastern (date, time) pair for
    scheduled games. time is 'TBD' when CFBD hasn't set a kickoff yet, which is
    true for roughly half the schedule this far out."""
    if not start_date_raw:
        return ('Date TBD', 'TBD')
    try:
        dt = datetime.datetime.fromisoformat(str(start_date_raw).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        et = dt.astimezone(ZoneInfo('America/New_York'))
        date_str = et.strftime('%a, %b %-d')
        time_str = 'TBD' if time_tbd else et.strftime('%-I:%M %p ET')
        return (date_str, time_str)
    except Exception:
        return (str(start_date_raw)[:10], 'TBD')

def build_game_card(row, ap_weekly, rivalry_map):
    """Turn a GAME_CARD_SELECT row into the dict the game_card macro consumes.
    Handles both completed games (scores, winner) and scheduled ones (kickoff
    date/time, no score). `ap_weekly` is the season's (regular, final) poll map
    from get_ap_week_map — each team's rank is the poll in effect for THAT
    game's week (scheduled or final), not a single season-wide poll."""
    (gid, home, home_pts, away, away_pts, week, season_type, notes,
     home_logo, away_logo, completed, start_date, time_tbd,
     home_color, away_color) = row
    date_str, time_str = format_kickoff(start_date, time_tbd)
    is_post = 'POST' in str(season_type or '').upper()
    ap = ap_asof(ap_weekly, week, is_post) if ap_weekly else {}
    return {
        'id': gid,
        'home': home, 'away': away,
        'home_pts': home_pts, 'away_pts': away_pts,
        'home_logo': home_logo, 'away_logo': away_logo,
        'home_color': home_color, 'away_color': away_color,
        'week': week, 'notes': notes,
        'completed': bool(completed),
        # Rank each team carried into this game — the weekly poll as-of its
        # week (upcoming games show ranks too once that week's poll is out).
        'home_rank': ap.get(home),
        'away_rank': ap.get(away),
        'rivalry': rivalry_map.get((home, away), '') if rivalry_map else '',
        'kickoff_date': date_str, 'kickoff_time': time_str,
    }

@app.route('/')
@app.route('/week/<int:week>/<season_type>')
@cache.cached(timeout=21600)  # keyed on path, so / and each /week/<n>/<type> cache separately
def home(week=None, season_type='regular'):
    conn = get_db()
    try:
        cursor = conn.cursor()
        ap_rankings = get_ap_rankings(cursor)                  # final poll (leaders/ticker)
        ap_weekly = get_ap_week_map(cursor, CURRENT_SEASON)    # per-week, for game cards

        cursor.execute('''
            SELECT week, season_type FROM (
                SELECT DISTINCT week, season_type,
                    CASE WHEN season_type = 'SeasonType.POSTSEASON' THEN 0 ELSE 1 END as sort_order
                FROM games WHERE completed = 1 AND season = %s
            ) sub
            ORDER BY sort_order, week DESC
        ''', (CURRENT_SEASON,))
        all_weeks = cursor.fetchall()

        if week is None:
            # Default to the current week: all_weeks sorts postseason first,
            # so once bowls/playoffs complete the home page shows those
            # instead of the last regular-season week.
            if all_weeks:
                week = all_weeks[0][0]
                season_type = 'postseason' if 'POSTSEASON' in all_weeks[0][1] else 'regular'
            else:
                week, season_type = 1, 'regular'

        db_season_type = 'SeasonType.POSTSEASON' if season_type == 'postseason' else 'SeasonType.REGULAR'

        cursor.execute(f'''
            SELECT {GAME_CARD_SELECT}
            FROM games g
            LEFT JOIN teams t1 ON g.home_team = t1.name
            LEFT JOIN teams t2 ON g.away_team = t2.name
            WHERE g.completed = 1 AND g.season = %s AND g.week = %s AND g.season_type = %s
            ORDER BY CASE WHEN g.notes LIKE '%%National Championship%%' THEN 1
                          WHEN g.notes LIKE '%%Semifinal%%' THEN 2
                          WHEN g.notes LIKE '%%Quarterfinal%%' THEN 3
                          WHEN g.notes LIKE '%%First Round%%' THEN 4
                          WHEN g.notes LIKE '%%Conference Championship%%' THEN 5
                          ELSE 6 END, g.notes, g.id
        ''', (CURRENT_SEASON, week, db_season_type))
        raw_games = cursor.fetchall()

        # One query for the whole rivalries table instead of one per game (N+1 fix)
        rivalry_map = get_rivalry_map(cursor)
        games = [build_game_card(row, ap_weekly, rivalry_map) for row in raw_games]

        label_order = ['National Championship','Semifinal','Quarterfinal','First Round','Conference Championships','Bowl Games']
        grouped_games = OrderedDict((label, []) for label in label_order)
        for game in games:
            grouped_games[get_game_label(game['notes'])].append(game)
        grouped_games = {k: v for k, v in grouped_games.items() if v}

        leaders = get_cached_season_leaders()

        # Live count of FBS teams for the hero pill, so it stays accurate
        # through realignment instead of a hardcoded "130+".
        cursor.execute('SELECT COUNT(*) FROM teams WHERE conference NOT IN %s', (FCS_CONFS,))
        fbs_team_count = cursor.fetchone()[0]
        # Drive tracking lives on the game page (Drives tab); point the hero
        # pill at the most prominent recent game rather than an unrelated page.
        featured_game_id = games[0]['id'] if games else None

    finally:
        release_db(conn)
    return render_template('home.html',
        games=games, grouped_games=grouped_games, all_weeks=all_weeks,
        selected_week=week, season_type=season_type,
        leaders=leaders, ap_rankings=ap_rankings,
        fbs_team_count=fbs_team_count, featured_game_id=featured_game_id)

@app.route('/games')
@cache.cached(timeout=21600, query_string=True)
def games_hub():
    """Full-season games hub: browse every game (completed 2025 + upcoming
    2026) by season and week, with a conference filter and a team filter that
    switches to a team's whole-season schedule. Reuses the home page's
    game_card component so completed and scheduled games look consistent."""
    conn = get_db()
    try:
        cursor = conn.cursor()

        cursor.execute('SELECT DISTINCT season FROM games ORDER BY season DESC')
        seasons = [r[0] for r in cursor.fetchall()]
        if not seasons:
            seasons = [2026]
        # Default to the newest season — this far out that's the upcoming 2026 slate.
        season = request.args.get('season', type=int)
        if season not in seasons:
            season = seasons[0]

        # Weeks available for the chosen season (regular first, postseason last).
        # DISTINCT requires ORDER BY cols to be selected; 'SeasonType.REGULAR'
        # sorts after 'SeasonType.POSTSEASON', so DESC puts regular weeks first
        # and the postseason last, which is the order we want.
        cursor.execute('''
            SELECT DISTINCT week, season_type FROM games WHERE season = %s
            ORDER BY season_type DESC, week
        ''', (season,))
        week_options = []
        for w, st in cursor.fetchall():
            is_post = 'POSTSEASON' in (st or '')
            week_options.append({
                'week': w,
                'stype': 'postseason' if is_post else 'regular',
                'label': 'Postseason' if is_post else f'Week {w}',
            })

        sel_week = request.args.get('week', type=int)
        sel_stype = request.args.get('stype', 'regular')
        match = next((o for o in week_options
                      if o['week'] == sel_week and o['stype'] == sel_stype), None)
        if not match:
            match = week_options[0] if week_options else {'week': 1, 'stype': 'regular'}
        sel_week, sel_stype = match['week'], match['stype']
        db_stype = 'SeasonType.POSTSEASON' if sel_stype == 'postseason' else 'SeasonType.REGULAR'

        sel_conf = request.args.get('conf', '')
        sel_team = request.args.get('team', '')

        # Filter dropdown data — FBS conferences and every FBS team.
        cursor.execute('SELECT DISTINCT conference FROM teams WHERE conference NOT IN %s ORDER BY conference', (FCS_CONFS,))
        conferences = [r[0] for r in cursor.fetchall() if r[0]]
        cursor.execute('SELECT name FROM teams WHERE conference NOT IN %s ORDER BY name', (FCS_CONFS,))
        team_names = [r[0] for r in cursor.fetchall()]

        # A team selection shows that team's full-season schedule (weeks stop
        # applying); otherwise browse the selected week, optionally by conference.
        where = ['g.season = %s']
        params = [season]
        if sel_team:
            where.append('(g.home_team = %s OR g.away_team = %s)')
            params += [sel_team, sel_team]
        else:
            where.append('g.week = %s AND g.season_type = %s')
            params += [sel_week, db_stype]
            if sel_conf:
                where.append('(t1.conference = %s OR t2.conference = %s)')
                params += [sel_conf, sel_conf]

        cursor.execute(f'''
            SELECT {GAME_CARD_SELECT}
            FROM games g
            LEFT JOIN teams t1 ON g.home_team = t1.name
            LEFT JOIN teams t2 ON g.away_team = t2.name
            WHERE {' AND '.join(where)}
            ORDER BY g.start_date NULLS LAST, g.id
        ''', params)
        raw_games = cursor.fetchall()

        rivalry_map = get_rivalry_map(cursor)
        ap_weekly = get_ap_week_map(cursor, season)   # poll as-of each game's week
        games = [build_game_card(row, ap_weekly, rivalry_map) for row in raw_games]

        # Savant Forecast chips for upcoming games — one batch read of the
        # precomputed predictions (predict_games.py), keyed by game id.
        forecasts = {}
        upcoming_ids = [g['id'] for g in games if not g['completed']]
        if upcoming_ids:
            try:
                cursor.execute('SELECT game_id, home_prob FROM game_predictions '
                               'WHERE game_id = ANY(%s) AND scored = 0', (upcoming_ids,))
                forecasts = {gid: p for gid, p in cursor.fetchall() if p is not None}
            except Exception:
                conn.rollback()   # table absent on a fresh DB — no chips

        # Frozen forecast for every completed card on this view (favorite %,
        # margin, and whether it hit) — the upset chip is derived from it.
        completed_ids = [g['id'] for g in games if g['completed']]
        completed_forecasts = get_frozen_forecasts(cursor, completed_ids)

        # Upsets among EXACTLY the games in the current view — derived from the
        # same cards + forecasts the grid renders, so the section always matches
        # what's listed (respects the week/conference/team filters). Previously
        # this queried MAX(week) independently and could show week 15's upsets
        # over a week-1 grid.
        week_upsets = []
        for g in games:
            f = completed_forecasts.get(g['id'])
            if not (g['completed'] and f and f['is_upset']):
                continue
            home_won = (g['home_pts'] or 0) > (g['away_pts'] or 0)
            week_upsets.append({
                'id': g['id'], 'favorite': f['favorite'], 'fav_prob': f['fav_prob'],
                'winner': g['home'] if home_won else g['away'],
                'winner_logo': g['home_logo'] if home_won else g['away_logo'],
            })
        week_upsets.sort(key=lambda u: u['fav_prob'], reverse=True)   # biggest surprise first

        # Label the section to match the view, so header and contents agree.
        if sel_team:
            upsets_label = sel_team
        elif sel_stype == 'postseason':
            upsets_label = 'Postseason'
        else:
            upsets_label = f'Week {sel_week}'
    finally:
        release_db(conn)

    return render_template('games.html',
        games=games, seasons=seasons, season=season, forecasts=forecasts,
        completed_forecasts=completed_forecasts,
        week_upsets=week_upsets, upsets_label=upsets_label,
        week_options=week_options, sel_week=sel_week, sel_stype=sel_stype,
        conferences=conferences, team_names=team_names,
        sel_conf=sel_conf, sel_team=sel_team)

@app.route('/search')
def search():
    q = request.args.get('q', '').strip()
    player_results = []
    team_results = []
    if q:
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT p.id, p.first_name, p.last_name, p.team, p.position,
                       p.jersey, p.headshot, t.conference, t.logo_dark,
                       p.active_2026, p.year
                FROM players p
                INNER JOIN teams t ON p.team = t.name
                WHERE (p.first_name || ' ' || p.last_name) ILIKE %s
                   OR p.last_name ILIKE %s
                   OR p.first_name ILIKE %s
                ORDER BY
                    -- exact/prefix name matches first, then current players
                    ((p.first_name || ' ' || p.last_name) ILIKE %s) DESC,
                    (p.last_name ILIKE %s) DESC,
                    COALESCE(p.active_2026, 0) DESC,
                    p.last_name, p.first_name
                LIMIT 50
            ''', (f'%{q}%', f'%{q}%', f'%{q}%', f'{q}%', f'{q}%'))
            player_results = cursor.fetchall()
            cursor.execute('''
                SELECT name, conference, logo_dark, color
                FROM teams
                WHERE (name ILIKE %s OR abbreviation ILIKE %s)
                  AND conference IS NOT NULL AND conference <> ALL(%s)
                ORDER BY name LIMIT 10
            ''', (f'%{q}%', f'%{q}%', list(FCS_CONFS)))
            team_results = cursor.fetchall()
        finally:
            release_db(conn)
    return render_template('search.html', player_results=player_results, team_results=team_results, query=q)

@app.route('/leaderboards')
@app.route('/leaderboards/<category>')
@cache.cached(timeout=21600, query_string=True)  # 1 hour — view/team/qualified are part of the query string, so each combo caches separately
def leaderboards(category='passing'):
    if category not in PLAYER_COLUMNS:
        category = 'passing'

    conn = get_db()
    try:
        cursor = conn.cursor()

        season      = requested_season()
        conf_filter = request.args.get('conf', '')
        team_filter = request.args.get('team', '')
        pos_filter  = request.args.get('pos', '')
        min_filter  = request.args.get('min', '')
        sort_col    = request.args.get('sort', '')
        sort_dir    = request.args.get('dir', 'desc')
        sort_dir    = sort_dir if sort_dir in ('asc', 'desc') else 'desc'
        view        = request.args.get('view', 'standard')
        view        = view if view in ('standard', 'advanced') else 'standard'
        qualified   = request.args.get('qualified', '1') != '0'
        page_raw    = request.args.get('page', '1')

        cursor.execute('SELECT DISTINCT conference FROM teams WHERE conference IS NOT NULL ORDER BY conference')
        conferences = [r[0] for r in cursor.fetchall() if r[0] not in FCS_CONFS]
        all_teams = get_teams_by_conference(cursor)

        ap_rankings = get_ap_rankings(cursor, season)
        players = []

        fcs_in = "','".join(FCS_CONFS)
        params = []
        conf_sql = ''
        if conf_filter:
            conf_sql = 'AND t.conference = %s'
            params.append(conf_filter)
        team_sql = ''
        if team_filter:
            team_sql = 'AND ps.team = %s'
            params.append(team_filter)
        pos_sql = ''
        if pos_filter in POSITION_GROUPS:
            pos_in = "','".join(POSITION_GROUPS[pos_filter])
            pos_sql = f"AND p.position IN ('{pos_in}')"

        column_defs = PLAYER_COLUMNS[category][view]
        ALLOWED = _sortable_keys(PLAYER_COLUMNS, category, view)
        if sort_col not in ALLOWED:
            sort_col = _default_sort_col(PLAYER_COLUMNS, category, view)

        if category == 'passing':
            min_att = min_filter if min_filter.isdigit() else '100'
            if not qualified:
                min_att = '0'

            ppa_join   = f'LEFT JOIN player_ppa pp ON pp.player_id = p.id::text AND pp.season = {season}' if view == 'advanced' else ''
            ppa_select = ", pp.avg_ppa_pass as epa_pass, pp.total_ppa as total_epa" if view == 'advanced' else ''
            ppa_group  = ', pp.avg_ppa_pass, pp.total_ppa' if view == 'advanced' else ''

            cursor.execute(f'''
                SELECT
                    p.id, p.first_name, p.last_name, ps.team, p.position, p.jersey, p.headshot,
                    t.logo_dark, t.conference, t.color,
                    MAX(CASE WHEN ps.stat_type='YDS'         THEN CAST(ps.stat AS REAL) END) as yds,
                    MAX(CASE WHEN ps.stat_type='TD'          THEN CAST(ps.stat AS REAL) END) as td,
                    MAX(CASE WHEN ps.stat_type='INT'         THEN CAST(ps.stat AS REAL) END) as int_,
                    MAX(CASE WHEN ps.stat_type='ATT'         THEN CAST(ps.stat AS REAL) END) as att,
                    MAX(CASE WHEN ps.stat_type='COMPLETIONS' THEN CAST(ps.stat AS REAL) END) as cmp,
                    MAX(CASE WHEN ps.stat_type='PCT'         THEN CAST(ps.stat AS REAL) END) as pct,
                    MAX(CASE WHEN ps.stat_type='YPA'         THEN CAST(ps.stat AS REAL) END) as ypa
                    {ppa_select}
                FROM players p
                JOIN player_stats ps ON ps.player_id = p.id::text AND ps.category = 'passing' AND ps.season = {season}
                JOIN teams t ON ps.team = t.name
                {ppa_join}
                WHERE p.position = 'QB'
                  AND t.conference NOT IN ('{fcs_in}')
                  {conf_sql} {team_sql} {pos_sql}
                GROUP BY p.id, ps.team, t.logo_dark, t.conference, t.color{ppa_group}
                HAVING MAX(CASE WHEN ps.stat_type='ATT' THEN CAST(ps.stat AS REAL) END) >= {min_att}
            ''', params)
            for r in cursor.fetchall():
                yds, td, int_, att, cmp_ = r[10] or 0, r[11] or 0, r[12] or 0, r[13] or 0, r[14] or 0
                pct = float(r[15] or 0)
                if pct <= 1.0: pct *= 100
                rtg     = ((8.4 * yds) + (330 * td) + (100 * cmp_) - (200 * int_)) / att if att else None
                adj_ypa = (yds + 20 * td - 45 * int_) / att if att else None
                row = {
                    'id': r[0], 'name': f"{r[1]} {r[2]}", 'first': r[1], 'last': r[2],
                    'team': r[3], 'pos': r[4], 'jersey': r[5], 'headshot': r[6],
                    'logo': r[7], 'conf': r[8], 'color': r[9],
                    'yds': int(yds), 'td': int(td), 'int': int(int_),
                    'att': int(att), 'cmp': int(cmp_), 'pct': round(pct, 1),
                    'ypa': round(float(r[16] or 0), 1),
                    'rtg': round(rtg, 1) if rtg is not None else None,
                    'adj_ypa': round(adj_ypa, 1) if adj_ypa is not None else None,
                    'gp': None, 'sack_pct': None,
                }
                if view == 'advanced':
                    row['epa_pass']  = round(float(r[17]), 3) if r[17] is not None else None
                    row['total_epa'] = round(float(r[18]), 1) if r[18] is not None else None
                players.append(row)

        elif category == 'rushing':
            min_att = min_filter if min_filter.isdigit() else '50'
            if not qualified:
                min_att = '0'

            ppa_join     = f'LEFT JOIN player_ppa pp ON pp.player_id = p.id::text AND pp.season = {season}' if view == 'advanced' else ''
            ppa_select   = ', pp.avg_ppa_rush as epa_rush, pp.total_ppa as total_epa' if view == 'advanced' else ''
            ppa_group    = ', pp.avg_ppa_rush, pp.total_ppa' if view == 'advanced' else ''
            usage_join   = f'LEFT JOIN player_usage pu ON pu.player_id = p.id AND pu.season = {season}' if view == 'advanced' else ''
            usage_select = ', pu.rush as usage_rush' if view == 'advanced' else ''
            usage_group  = ', pu.rush' if view == 'advanced' else ''

            cursor.execute(f'''
                SELECT
                    p.id, p.first_name, p.last_name, ps.team, p.position, p.jersey, p.headshot,
                    t.logo_dark, t.conference, t.color,
                    MAX(CASE WHEN ps.stat_type='YDS'  THEN CAST(ps.stat AS REAL) END) as yds,
                    MAX(CASE WHEN ps.stat_type='TD'   THEN CAST(ps.stat AS REAL) END) as td,
                    MAX(CASE WHEN ps.stat_type='CAR'  THEN CAST(ps.stat AS REAL) END) as att,
                    MAX(CASE WHEN ps.stat_type='YPC'  THEN CAST(ps.stat AS REAL) END) as ypc,
                    MAX(CASE WHEN ps.stat_type='LONG' THEN CAST(ps.stat AS REAL) END) as long_,
                    MAX(CASE WHEN pf.stat_type='FUM'  THEN CAST(pf.stat AS REAL) END) as fum
                    {ppa_select}{usage_select}
                FROM players p
                JOIN player_stats ps ON ps.player_id = p.id::text AND ps.category = 'rushing' AND ps.season = {season}
                JOIN teams t ON ps.team = t.name
                LEFT JOIN player_stats pf ON pf.player_id = p.id::text AND pf.category = 'fumbles' AND pf.season = {season}
                {ppa_join}
                {usage_join}
                WHERE p.position IN ('RB','FB','QB','WR','ATH')
                  AND t.conference NOT IN ('{fcs_in}')
                  {conf_sql} {team_sql} {pos_sql}
                GROUP BY p.id, ps.team, t.logo_dark, t.conference, t.color{ppa_group}{usage_group}
                HAVING MAX(CASE WHEN ps.stat_type='CAR' THEN CAST(ps.stat AS REAL) END) >= {min_att}
            ''', params)
            for r in cursor.fetchall():
                row = {
                    'id': r[0], 'name': f"{r[1]} {r[2]}", 'first': r[1], 'last': r[2],
                    'team': r[3], 'pos': r[4], 'jersey': r[5], 'headshot': r[6],
                    'logo': r[7], 'conf': r[8], 'color': r[9],
                    'yds': int(r[10] or 0), 'td': int(r[11] or 0), 'att': int(r[12] or 0),
                    'ypc': round(float(r[13] or 0), 1), 'long': int(r[14] or 0),
                    'fum': int(r[15] or 0),
                    'gp': None, 'ypg': None,
                }
                idx = 16
                if view == 'advanced':
                    row['epa_rush']  = round(float(r[idx]), 3) if r[idx] is not None else None; idx += 1
                    row['total_epa'] = round(float(r[idx]), 1) if r[idx] is not None else None; idx += 1
                    usage_val = r[idx]
                    if usage_val is not None:
                        usage_val = float(usage_val)
                        if usage_val <= 1.0: usage_val *= 100
                        usage_val = round(usage_val, 1)
                    row['usage'] = usage_val
                    row['exp_pct'] = None
                players.append(row)

        elif category == 'receiving':
            min_rec = min_filter if min_filter.isdigit() else '20'
            if not qualified:
                min_rec = '0'

            ppa_join   = f'LEFT JOIN player_ppa pp ON pp.player_id = p.id::text AND pp.season = {season}' if view == 'advanced' else ''
            ppa_select = ', pp.avg_ppa_all as epa_play, pp.total_ppa as total_epa' if view == 'advanced' else ''
            ppa_group  = ', pp.avg_ppa_all, pp.total_ppa' if view == 'advanced' else ''

            cursor.execute(f'''
                SELECT
                    p.id, p.first_name, p.last_name, ps.team, p.position, p.jersey, p.headshot,
                    t.logo_dark, t.conference, t.color,
                    MAX(CASE WHEN ps.stat_type='YDS'  THEN CAST(ps.stat AS REAL) END) as yds,
                    MAX(CASE WHEN ps.stat_type='TD'   THEN CAST(ps.stat AS REAL) END) as td,
                    MAX(CASE WHEN ps.stat_type='REC'  THEN CAST(ps.stat AS REAL) END) as rec,
                    MAX(CASE WHEN ps.stat_type='YPR'  THEN CAST(ps.stat AS REAL) END) as ypr,
                    MAX(CASE WHEN ps.stat_type='LONG' THEN CAST(ps.stat AS REAL) END) as long_
                    {ppa_select}
                FROM players p
                JOIN player_stats ps ON ps.player_id = p.id::text AND ps.category = 'receiving' AND ps.season = {season}
                JOIN teams t ON ps.team = t.name
                {ppa_join}
                WHERE p.position IN ('WR','TE','RB','ATH')
                  AND t.conference NOT IN ('{fcs_in}')
                  {conf_sql} {team_sql} {pos_sql}
                GROUP BY p.id, ps.team, t.logo_dark, t.conference, t.color{ppa_group}
                HAVING MAX(CASE WHEN ps.stat_type='REC' THEN CAST(ps.stat AS REAL) END) >= {min_rec}
            ''', params)
            for r in cursor.fetchall():
                row = {
                    'id': r[0], 'name': f"{r[1]} {r[2]}", 'first': r[1], 'last': r[2],
                    'team': r[3], 'pos': r[4], 'jersey': r[5], 'headshot': r[6],
                    'logo': r[7], 'conf': r[8], 'color': r[9],
                    'yds': int(r[10] or 0), 'td': int(r[11] or 0), 'rec': int(r[12] or 0),
                    'ypr': round(float(r[13] or 0), 1), 'long': int(r[14] or 0),
                    'gp': None, 'tgt': None, 'cth_pct': None, 'ypg': None,
                }
                if view == 'advanced':
                    row['epa_play']  = round(float(r[15]), 3) if r[15] is not None else None
                    row['total_epa'] = round(float(r[16]), 1) if r[16] is not None else None
                    row['tgt_pct']   = None
                players.append(row)

        elif category == 'defense':
            min_tot = min_filter if min_filter.isdigit() else '15'
            if not qualified:
                min_tot = '0'

            cursor.execute(f'''
                SELECT
                    p.id, p.first_name, p.last_name, ps.team, p.position, p.jersey, p.headshot,
                    t.logo_dark, t.conference, t.color,
                    MAX(CASE WHEN ps.stat_type='TOT'   THEN CAST(ps.stat AS REAL) END) as tot,
                    MAX(CASE WHEN ps.stat_type='SOLO'  THEN CAST(ps.stat AS REAL) END) as solo,
                    MAX(CASE WHEN ps.stat_type='SACKS' THEN CAST(ps.stat AS REAL) END) as sacks,
                    MAX(CASE WHEN ps.stat_type='TFL'   THEN CAST(ps.stat AS REAL) END) as tfl,
                    MAX(CASE WHEN ps.stat_type='PD'    THEN CAST(ps.stat AS REAL) END) as pd,
                    MAX(CASE WHEN ps.stat_type='TD'    THEN CAST(ps.stat AS REAL) END) as td,
                    MAX(CASE WHEN pi.stat_type='INT'   THEN CAST(pi.stat AS REAL) END) as int_
                FROM players p
                JOIN player_stats ps ON ps.player_id = p.id::text AND ps.category = 'defensive' AND ps.season = {season}
                JOIN teams t ON ps.team = t.name
                LEFT JOIN player_stats pi ON pi.player_id = p.id::text AND pi.category = 'interceptions' AND pi.season = {season}
                WHERE p.position IN ('DE','DT','NT','DL','EDGE','LB','CB','S','DB')
                  AND t.conference NOT IN ('{fcs_in}')
                  {conf_sql} {team_sql} {pos_sql}
                GROUP BY p.id, ps.team, t.logo_dark, t.conference, t.color
                HAVING MAX(CASE WHEN ps.stat_type='TOT' THEN CAST(ps.stat AS REAL) END) >= {min_tot}
            ''', params)
            defense_rows = cursor.fetchall()

            team_tot_map = {}
            if view == 'advanced':
                cursor.execute('''
                    SELECT team, SUM(CAST(stat AS REAL))
                    FROM player_stats WHERE category='defensive' AND stat_type='TOT' AND season=%s
                    GROUP BY team
                ''', (season,))
                team_tot_map = {r[0]: r[1] for r in cursor.fetchall()}

            for r in defense_rows:
                tot, solo = int(r[10] or 0), int(r[11] or 0)
                row = {
                    'id': r[0], 'name': f"{r[1]} {r[2]}", 'first': r[1], 'last': r[2],
                    'team': r[3], 'pos': r[4], 'jersey': r[5], 'headshot': r[6],
                    'logo': r[7], 'conf': r[8], 'color': r[9],
                    'tot': tot, 'solo': solo, 'ast': tot - solo,
                    'sacks': round(float(r[12] or 0), 1),
                    'tfl':   round(float(r[13] or 0), 1),
                    'pd':    int(r[14] or 0),
                    'td':    int(r[15] or 0),
                    'int':   int(r[16] or 0),
                    'gp': None, 'ff': None,
                }
                if view == 'advanced':
                    team_tot = team_tot_map.get(r[3])
                    row['tkl_pct']  = round(tot / team_tot * 100, 1) if team_tot else None
                    row['epa_play'] = None
                    row['prsh']     = None
                players.append(row)

        players, pagination = _sort_and_paginate(players, sort_col, sort_dir, page_raw)

    finally:
        release_db(conn)

    current_filters = {
        'mode': 'player', 'category': category, 'view': view, 'season': season,
        'conf': conf_filter, 'team': team_filter, 'pos': pos_filter,
        'qualified': '1' if qualified else '0', 'sort': sort_col, 'dir': sort_dir,
    }
    has_advanced = len(PLAYER_COLUMNS[category]['advanced']) > 0
    return render_template('leaderboards.html',
        mode='player', players=players, category=category, view=view,
        season=season, available_seasons=get_available_seasons(),
        conferences=conferences, all_teams=all_teams,
        conf_filter=conf_filter, team_filter=team_filter, pos_filter=pos_filter,
        min_filter=min_filter, sort_col=sort_col, sort_dir=sort_dir,
        qualified=qualified, column_defs=column_defs, current_filters=current_filters,
        has_advanced=has_advanced, position_groups=list(POSITION_GROUPS.keys()),
        ap_rankings=ap_rankings, pagination=pagination,
    )

# ── Team leaderboards ───────────────────────────────────────────────────────
# Categories reconciled to Offense/Defense/SP+ — Havoc and Scoring (formerly
# standalone categories) are now folded into the Defense/Offense Advanced views.
TEAM_CATEGORY_DEFAULTS = {
    'offense': ('off_ppa', 'desc'),
    'defense': ('def_ppa', 'asc'),   # lower is better, so ascending = best first
    'sp':      ('rating', 'desc'),
    'savant':  ('net_rating', 'desc'),
}

# Columns fetched from team_stats — offense is always-higher-better.
# Defense: def_power_success and def_stuff_rate are HIGHER-is-better (more
# stops/stuffs = good defense) despite the def_ prefix; the rest are lower-better.
#  bare column name -> table-qualified SQL reference. off_ppa/def_ppa/etc. exist
#  in BOTH team_stats and team_advanced, so an unqualified ORDER BY is ambiguous
#  once both tables are joined — every sortable column must be qualified.
TEAM_SORTABLE_COLS = {
    'off_ppa': 'ts.off_ppa', 'off_success_rate': 'ts.off_success_rate',
    'off_explosiveness': 'ts.off_explosiveness', 'off_power_success': 'ts.off_power_success',
    'off_line_yards': 'ts.off_line_yards', 'off_second_level_yards': 'ts.off_second_level_yards',
    'off_open_field_yards': 'ts.off_open_field_yards',
    'off_rushing_plays_ppa': 'ts.off_rushing_plays_ppa', 'off_passing_plays_ppa': 'ts.off_passing_plays_ppa',
    'off_rushing_success_rate': 'ts.off_rushing_success_rate', 'off_passing_success_rate': 'ts.off_passing_success_rate',
    'off_rushing_explosiveness': 'ts.off_rushing_explosiveness', 'off_passing_explosiveness': 'ts.off_passing_explosiveness',
    'def_ppa': 'ts.def_ppa', 'def_success_rate': 'ts.def_success_rate',
    'def_explosiveness': 'ts.def_explosiveness', 'def_power_success': 'ts.def_power_success',
    'def_stuff_rate': 'ts.def_stuff_rate', 'def_line_yards': 'ts.def_line_yards',
    'def_second_level_yards': 'ts.def_second_level_yards', 'def_open_field_yards': 'ts.def_open_field_yards',
    'def_havoc_total': 'adv.def_havoc_total', 'def_havoc_front7': 'adv.def_havoc_front7',
    'def_havoc_db': 'adv.def_havoc_db',
    'off_scoring_opps': 'adv.off_scoring_opps', 'off_pts_per_opp': 'adv.off_pts_per_opp',
    'off_field_pos_avg_start': 'adv.off_field_pos_avg_start',
    'rating': 'sp.rating', 'offense_rating': 'sp.offense_rating',
    'defense_rating': 'sp.defense_rating', 'special_teams_rating': 'sp.special_teams_rating',
    'ranking': 'sp.ranking',
    'net_rating': 'svr.net_rating', 'off_rating': 'svr.off_rating',
    'def_rating': 'svr.def_rating', 'sos': 'svr.sos',
    'svr_games': 'svr.games', 'raw_off': 'svr.raw_off', 'raw_def': 'svr.raw_def',
    'drives_off': 'svr.drives_off', 'drives_def': 'svr.drives_def',
    'net_ranking': 'svr.net_ranking',
}
TEAM_LOWER_BETTER = {
    'def_ppa','def_success_rate','def_explosiveness',
    'def_line_yards','def_open_field_yards','def_second_level_yards',
    'ranking',  # SP+ national rank — #1 is best
    'def_rating','raw_def',  # Savant points allowed per 10 drives — lower is better
    'net_ranking',           # Savant national rank — #1 is best
}

def _hex_to_rgba(hex_color, alpha):
    if not hex_color:
        return None
    h = hex_color.lstrip('#')
    if len(h) != 6:
        return None
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return None
    return f'rgba({r},{g},{b},{alpha})'


def slugify_team(name):
    """Deterministic URL slug for a team name: 'North Texas' -> 'north-texas',
    'Miami (OH)' -> 'miami-oh'. Matches the stored teams.slug column and is
    registered as the `team_slug` Jinja filter for building links."""
    s = unicodedata.normalize('NFKD', name or '')
    s = ''.join(c for c in s if not unicodedata.combining(c)).lower().replace("'", '')
    return re.sub(r'[^a-z0-9]+', '-', s).strip('-')


app.jinja_env.filters['team_slug'] = slugify_team


def team_url(name, season=None):
    """Build a team-page URL, season-aware. In a historical context (a page
    scoped to a past season), clicking a team should land on that same
    season's team page — so a season other than the current one is carried
    through as ?season=YYYY. The current season is left as a bare URL to keep
    links clean (the team page already defaults to it). Registered as the
    `team_url` Jinja global for use anywhere a team link is rendered."""
    slug = slugify_team(name)
    try:
        s = int(season) if season is not None else None
    except (TypeError, ValueError):
        s = None
    if s and s != CURRENT_SEASON:
        return f'/team/{slug}?season={s}'
    return f'/team/{slug}'


app.jinja_env.globals['team_url'] = team_url


def player_url(player_id, season=None):
    """Build a player-page URL, season-aware. Clicking a player from a page
    scoped to a past season should land on that same season's stats — so a
    season other than the current one is carried through as ?season=YYYY. The
    current season is left as a bare URL (the player page then defaults to the
    player's most recent recorded season, which for a current-season context
    is the current season). A season the player has no data for is handled by
    the route, which falls back to the default season. Registered as the
    `player_url` Jinja global."""
    try:
        s = int(season) if season is not None else None
    except (TypeError, ValueError):
        s = None
    if s and s != CURRENT_SEASON:
        return f'/player/{player_id}?season={s}'
    return f'/player/{player_id}'


app.jinja_env.globals['player_url'] = player_url


def clean_play_text(text):
    """Normalize raw play-by-play descriptions for consistent display.

    Two source feeds mix in this data: ESPN (mixed case, zero-padded yard
    lines like "Miami00"/"IND05") and an NCAA feed (ALL-CAPS with 1-digit
    yard lines like "MIAMI16"). Both render awkwardly, so:
      • split a team token glued to its yard number and drop the pad —
        "Miami00" -> "Miami 0", "IND05" -> "IND 5", "MIAMI16" -> "MIAMI 16";
      • title-case ALL-CAPS words (TOUCHDOWN, JOYCE, MIAMI) so casing matches
        the ESPN feed, leaving short abbreviations (TD, IND, LS, QB) alone.
    """
    if not text:
        return text
    text = re.sub(r'\b([A-Za-z]{2,})(\d{1,2})\b',
                  lambda m: f"{m.group(1)} {int(m.group(2))}", text)
    text = re.sub(r'\b[A-Z]{2,}\b',
                  lambda m: m.group(0)[:1] + m.group(0)[1:].lower()
                  if len(m.group(0)) >= 4 else m.group(0), text)
    return text


@app.route('/leaderboards/teams')
@app.route('/leaderboards/teams/<category>')
@cache.cached(timeout=21600, query_string=True)  # view/team are part of the query string, so each combo caches separately
def leaderboards_teams(category='savant'):
    if category not in TEAM_CATEGORY_DEFAULTS:
        category = 'offense'

    season      = requested_season()
    conf_filter = request.args.get('conf', '')
    team_filter = request.args.get('team', '')
    sort_col    = request.args.get('sort', '')
    sort_dir    = request.args.get('dir', '')
    page_raw    = request.args.get('page', '1')
    view        = request.args.get('view', 'standard')
    view        = view if view in ('standard', 'advanced') else 'standard'

    column_defs = TEAM_COLUMNS[category][view] or TEAM_COLUMNS[category]['standard']

    default_sort, default_dir = TEAM_CATEGORY_DEFAULTS[category]
    if sort_col not in TEAM_SORTABLE_COLS:
        sort_col = default_sort
        sort_dir = default_dir
    elif not sort_dir:
        sort_dir = 'desc'
    dir_sql   = 'ASC' if sort_dir == 'asc' else 'DESC'
    sort_sql  = TEAM_SORTABLE_COLS[sort_col]  # table-qualified — avoids ambiguous-column errors

    higher_better = sort_col not in TEAM_LOWER_BETTER
    goodness_dir  = 'DESC' if higher_better else 'ASC'

    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT DISTINCT conference FROM teams WHERE conference IS NOT NULL ORDER BY conference')
        conferences = [r[0] for r in cursor.fetchall() if r[0] not in FCS_CONFS]
        all_teams = get_teams_by_conference(cursor)

        fcs_in   = "','".join(FCS_CONFS)
        conf_sql = "AND t.conference = %s" if conf_filter else ""
        team_sql = "AND t.name = %s" if team_filter else ""
        params   = []
        if conf_filter: params.append(conf_filter)
        if team_filter: params.append(team_filter)

        cursor.execute(f'''
            SELECT COUNT(*) FROM teams t
            WHERE t.conference NOT IN ('{fcs_in}')
            {conf_sql} {team_sql}
        ''', params)
        page, offset, pagination = _pagination_ctx(page_raw, cursor.fetchone()[0])

        cursor.execute(f'''
            SELECT
                t.name, t.conference, t.logo_dark, t.color, ar.rank as ap_rank,
                ts.off_ppa, ts.off_success_rate, ts.off_explosiveness, ts.off_power_success,
                ts.off_line_yards, ts.off_second_level_yards, ts.off_open_field_yards,
                ts.off_rushing_plays_ppa, ts.off_passing_plays_ppa,
                ts.off_rushing_success_rate, ts.off_passing_success_rate,
                ts.off_rushing_explosiveness, ts.off_passing_explosiveness,
                ts.def_ppa, ts.def_success_rate, ts.def_explosiveness, ts.def_power_success,
                ts.def_stuff_rate, ts.def_line_yards, ts.def_second_level_yards, ts.def_open_field_yards,
                adv.def_havoc_total, adv.def_havoc_front7, adv.def_havoc_db,
                adv.off_scoring_opps, adv.off_pts_per_opp, adv.off_field_pos_avg_start,
                sp.rating, sp.offense_rating, sp.defense_rating, sp.special_teams_rating, sp.ranking,
                svr.net_rating, svr.off_rating, svr.def_rating, svr.sos,
                svr.games AS svr_games, svr.raw_off, svr.raw_def,
                svr.drives_off, svr.drives_def, svr.net_ranking,
                RANK() OVER (ORDER BY {sort_sql} {goodness_dir} NULLS LAST) as goodness_rank
            FROM teams t
            LEFT JOIN team_stats ts ON ts.team = t.name AND ts.season = {season}
            LEFT JOIN team_advanced adv ON adv.team = t.name AND adv.season = {season}
            LEFT JOIN sp_ratings sp ON sp.team = t.name AND sp.season = {season}
            LEFT JOIN savant_ratings svr ON svr.team = t.name AND svr.season = {season}
            LEFT JOIN ap_rankings ar ON ar.team = t.name AND ar.season = {season}
            WHERE t.conference NOT IN ('{fcs_in}')
            {conf_sql} {team_sql}
            ORDER BY {sort_sql} {dir_sql} NULLS LAST
            LIMIT {LEADERBOARD_PER_PAGE} OFFSET {offset}
        ''', params)
        cols = [d[0] for d in cursor.description]

        def _r(v, nd=3): return round(v, nd) if v is not None else None
        def _pct(v): return round(v * 100, 1) if v is not None else None

        teams_out = []
        for i, row in enumerate(cursor.fetchall()):
            d = dict(zip(cols, row))
            is_good = d['goodness_rank'] is not None and pagination['total_count'] and \
                      d['goodness_rank'] <= max(1, pagination['total_count'] // 2)
            bg = _hex_to_rgba(d['color'], 0.14) if d['color'] else None
            if bg is None:
                bg = 'rgba(52,211,153,0.1)' if is_good else 'rgba(248,113,113,0.1)'
            teams_out.append({
                'rank': offset + i + 1, 'name': d['name'], 'conf': d['conference'],
                'logo': d['logo_dark'], 'color': d['color'], 'ap_rank': d['ap_rank'],
                'sort_bg': bg,
                'off_ppa': _r(d['off_ppa']), 'off_success_rate': _pct(d['off_success_rate']),
                'off_explosiveness': _r(d['off_explosiveness']), 'off_power_success': _pct(d['off_power_success']),
                'off_line_yards': _r(d['off_line_yards'], 2), 'off_second_level_yards': _r(d['off_second_level_yards'], 2),
                'off_open_field_yards': _r(d['off_open_field_yards'], 2),
                'off_rushing_plays_ppa': _r(d['off_rushing_plays_ppa']), 'off_passing_plays_ppa': _r(d['off_passing_plays_ppa']),
                'off_rushing_success_rate': _pct(d['off_rushing_success_rate']), 'off_passing_success_rate': _pct(d['off_passing_success_rate']),
                'off_rushing_explosiveness': _r(d['off_rushing_explosiveness']), 'off_passing_explosiveness': _r(d['off_passing_explosiveness']),
                'def_ppa': _r(d['def_ppa']), 'def_success_rate': _pct(d['def_success_rate']),
                'def_explosiveness': _r(d['def_explosiveness']), 'def_power_success': _pct(d['def_power_success']),
                'def_stuff_rate': _pct(d['def_stuff_rate']), 'def_line_yards': _r(d['def_line_yards'], 2),
                'def_second_level_yards': _r(d['def_second_level_yards'], 2), 'def_open_field_yards': _r(d['def_open_field_yards'], 2),
                'def_havoc_total': _pct(d['def_havoc_total']), 'def_havoc_front7': _pct(d['def_havoc_front7']),
                'def_havoc_db': _pct(d['def_havoc_db']),
                'off_scoring_opps': d['off_scoring_opps'], 'off_pts_per_opp': _r(d['off_pts_per_opp'], 2),
                'off_field_pos_avg_start': _r(d['off_field_pos_avg_start'], 1),
                'rating': _r(d['rating'], 1), 'offense_rating': _r(d['offense_rating'], 1),
                'defense_rating': _r(d['defense_rating'], 1), 'special_teams_rating': _r(d['special_teams_rating'], 1),
                'ranking': d['ranking'],
                'net_rating': _r(d['net_rating'], 1), 'off_rating': _r(d['off_rating'], 1),
                'def_rating': _r(d['def_rating'], 1), 'sos': _r(d['sos'], 1),
                'svr_games': d['svr_games'], 'raw_off': _r(d['raw_off'], 1), 'raw_def': _r(d['raw_def'], 1),
                'drives_off': d['drives_off'], 'drives_def': d['drives_def'],
                'net_ranking': d['net_ranking'],
                # unavailable-in-dataset columns, kept as None so column_defs 'na' entries render consistently
                'def_passing_plays_ppa': None, 'def_passing_success_rate': None, 'def_passing_explosiveness': None,
                'def_rushing_plays_ppa': None, 'def_rushing_success_rate': None, 'def_rushing_explosiveness': None,
            })
    finally:
        release_db(conn)

    current_filters = {
        'mode': 'team', 'category': category, 'view': view, 'season': season,
        'conf': conf_filter, 'team': team_filter,
        'sort': sort_col, 'dir': sort_dir,
    }
    has_advanced = len(TEAM_COLUMNS[category]['advanced']) > 0
    return render_template('leaderboards.html',
        mode='team', teams=teams_out, category=category, view=view,
        season=season, available_seasons=get_available_seasons(),
        conferences=conferences, all_teams=all_teams,
        conf_filter=conf_filter, team_filter=team_filter,
        sort_col=sort_col, sort_dir=sort_dir,
        column_defs=column_defs, current_filters=current_filters,
        has_advanced=has_advanced,
        pagination=pagination,
    )

@app.route('/teams')
@cache.cached(timeout=86400)  # 24 hours — basically static
def teams():
    conn = get_db()
    try:
        cursor = conn.cursor()
        ap_rankings = get_ap_rankings(cursor)
        conf_logos = get_conference_logos(cursor)
        # Exclude FCS programs (present only for opponent-logo lookups on
        # schedule/game pages) so they never surface on this FBS-only grid.
        cursor.execute('SELECT name, conference, logo_dark, color, alt_color FROM teams '
                       'WHERE conference NOT IN %s ORDER BY conference, name', (FCS_CONFS,))
        rows = cursor.fetchall()
    finally:
        release_db(conn)
    conf_order = ['SEC','Big Ten','Big 12','ACC','American Athletic','Mountain West','Sun Belt','MAC','Conference USA','FBS Independents']
    conferences = {}
    for team in rows:
        conf = team[1] or 'Other'
        if conf not in conferences: conferences[conf] = []
        conferences[conf].append(team)
    sorted_confs = OrderedDict()
    for conf in conf_order:
        if conf in conferences: sorted_confs[conf] = conferences[conf]
    for conf in conferences:
        if conf not in sorted_confs: sorted_confs[conf] = conferences[conf]
    return render_template('teams.html', conferences=sorted_confs, ap_rankings=ap_rankings,
                           conf_logos=conf_logos)

@app.route('/savant-rating')
@cache.cached(timeout=86400)  # 24 hours — recomputed offline by compute_savant_ratings.py
def savant_rating_methodology():
    """Plain-language methodology page for the Savant Rating (SVR) system."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT sr.team, t.logo_dark, t.conference, sr.net_rating, sr.off_rating,
                   sr.def_rating, sr.sos, sr.net_ranking, ar.rank
            FROM savant_ratings sr
            JOIN teams t ON t.name = sr.team
            LEFT JOIN ap_rankings ar ON ar.team = sr.team AND ar.season = sr.season
            WHERE sr.season = %s
            ORDER BY sr.net_ranking
            LIMIT 10
        ''', (CURRENT_SEASON,))
        top10 = [{'team': r[0], 'logo': r[1], 'conf': r[2], 'net': r[3], 'off': r[4],
                  'def': r[5], 'sos': r[6], 'rank': r[7], 'ap': r[8]}
                 for r in cursor.fetchall()]
        cursor.execute('SELECT COUNT(*), SUM(drives_off), SUM(games)/2 FROM savant_ratings WHERE season = %s',
                       (CURRENT_SEASON,))
        n_teams, n_drives, n_games = cursor.fetchone()
    finally:
        release_db(conn)
    return render_template('savant_rating.html', top10=top10, season=CURRENT_SEASON,
                           n_teams=n_teams, n_drives=n_drives, n_games=n_games)

# ── Returning Production ────────────────────────────────────────────────────
# Bill Connelly–style metric: what share of a team's PRIOR-season statistical
# production is retained on THIS season's roster (same school only — a player
# who transferred in produced nothing here last year, so he can't be
# "returning"). Split into offense and defense and ranked nationally.
#   Offense weight  = passing + rushing + receiving yards (the passer and the
#                     catcher are both credited, per the standard definition).
#   Defense weight  = tackles + 2·TFL + 3·sacks + 3·INT + passes-defended,
#                     a volume base with impact-play bonuses (no defensive
#                     "yards" exist to weight by).
def _rp_def_value(tot, tfl, sacks, ints, pd):
    return (tot or 0) + 2 * (tfl or 0) + 3 * (sacks or 0) + 3 * (ints or 0) + (pd or 0)


@cache.memoize(timeout=21600)
def _returning_production_ranks(season):
    """Every FBS team's offense/defense/overall returning-production percentage
    for `season` (vs season-1), plus national ranks.

    Precomputed weekly into pool_store (key `returning:{season}`) by
    precompute.py; on a miss (e.g. a brand-new season, or right after a store
    reset) it computes live and writes back, the same self-healing pattern the
    percentile pools use. The @cache.memoize adds an in-process layer on top."""
    key = f"returning:{season}"
    stored = _pool_store_get(key)
    if stored is not None:
        return stored
    prior = season - 1
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('''
            SELECT ps.team, ps.player_id,
              SUM(CASE WHEN ps.category IN ('passing','rushing','receiving') AND ps.stat_type='YDS' THEN CAST(ps.stat AS REAL) ELSE 0 END) AS off_yds,
              SUM(CASE WHEN ps.category='defensive'     AND ps.stat_type='TOT'   THEN CAST(ps.stat AS REAL) ELSE 0 END) AS tot,
              SUM(CASE WHEN ps.category='defensive'     AND ps.stat_type='TFL'   THEN CAST(ps.stat AS REAL) ELSE 0 END) AS tfl,
              SUM(CASE WHEN ps.category='defensive'     AND ps.stat_type='SACKS' THEN CAST(ps.stat AS REAL) ELSE 0 END) AS sacks,
              SUM(CASE WHEN ps.category='defensive'     AND ps.stat_type='PD'    THEN CAST(ps.stat AS REAL) ELSE 0 END) AS pd,
              SUM(CASE WHEN ps.category='interceptions' AND ps.stat_type='INT'   THEN CAST(ps.stat AS REAL) ELSE 0 END) AS ints
            FROM player_stats ps
            WHERE ps.season = %s
            GROUP BY ps.team, ps.player_id
        ''', (prior,))
        prod = cur.fetchall()
        cur.execute('SELECT team, player_id FROM rosters WHERE season = %s', (season,))
        ret_set = {(t, str(pid)) for t, pid in cur.fetchall()}
        cur.execute('SELECT name FROM teams WHERE conference IS NOT NULL AND conference <> ALL(%s)',
                    (list(FCS_CONFS),))
        fbs = {r[0] for r in cur.fetchall()}
    finally:
        release_db(conn)

    teams = {}
    for team_name, pid, off_yds, tot, tfl, sacks, pd, ints in prod:
        d = teams.setdefault(team_name, {'off_tot': 0.0, 'off_ret': 0.0, 'def_tot': 0.0, 'def_ret': 0.0})
        ov = off_yds or 0.0
        dv = _rp_def_value(tot, tfl, sacks, ints, pd)
        d['off_tot'] += ov
        d['def_tot'] += dv
        if (team_name, str(pid)) in ret_set:
            d['off_ret'] += ov
            d['def_ret'] += dv

    for d in teams.values():
        d['off_pct'] = round(d['off_ret'] / d['off_tot'] * 100, 1) if d['off_tot'] > 0 else None
        d['def_pct'] = round(d['def_ret'] / d['def_tot'] * 100, 1) if d['def_tot'] > 0 else None
        tot_pool = d['off_tot'] + d['def_tot']
        d['overall_pct'] = round((d['off_ret'] + d['def_ret']) / tot_pool * 100, 1) if tot_pool > 0 else None

    fbs_teams = [t for t in teams if t in fbs]
    counts = {}
    for pct_key, rank_key in (('off_pct', 'off_rank'), ('def_pct', 'def_rank'), ('overall_pct', 'overall_rank')):
        ranked = sorted((t for t in fbs_teams if teams[t][pct_key] is not None),
                        key=lambda t: teams[t][pct_key], reverse=True)
        for i, t in enumerate(ranked, 1):
            teams[t][rank_key] = i
        counts[rank_key] = len(ranked)
    result = {'teams': teams, 'counts': counts}
    _pool_store_put(key, season, result)
    return result


def _returning_breakdown(cursor, team, prior, season, limit=4):
    """Top returning and departed prior-season contributors for one team,
    ranked by their share of the team's total production (offense + defense)."""
    cursor.execute('''
        SELECT ps.player_id, p.first_name, p.last_name, p.position,
          SUM(CASE WHEN ps.category IN ('passing','rushing','receiving') AND ps.stat_type='YDS' THEN CAST(ps.stat AS REAL) ELSE 0 END) AS off_yds,
          SUM(CASE WHEN ps.category='defensive'     AND ps.stat_type='TOT'   THEN CAST(ps.stat AS REAL) ELSE 0 END) AS tot,
          SUM(CASE WHEN ps.category='defensive'     AND ps.stat_type='TFL'   THEN CAST(ps.stat AS REAL) ELSE 0 END) AS tfl,
          SUM(CASE WHEN ps.category='defensive'     AND ps.stat_type='SACKS' THEN CAST(ps.stat AS REAL) ELSE 0 END) AS sacks,
          SUM(CASE WHEN ps.category='defensive'     AND ps.stat_type='PD'    THEN CAST(ps.stat AS REAL) ELSE 0 END) AS pd,
          SUM(CASE WHEN ps.category='interceptions' AND ps.stat_type='INT'   THEN CAST(ps.stat AS REAL) ELSE 0 END) AS ints
        FROM player_stats ps
        LEFT JOIN players p ON p.id = ps.player_id::int
        WHERE ps.team = %s AND ps.season = %s
        GROUP BY ps.player_id, p.first_name, p.last_name, p.position
    ''', (team, prior))
    rows = cursor.fetchall()
    if not rows:
        return [], []
    off_tot = sum((r[4] or 0) for r in rows) or 1
    def_tot = sum(_rp_def_value(r[5], r[6], r[7], r[9], r[8]) for r in rows) or 1

    cursor.execute('SELECT player_id FROM rosters WHERE season = %s AND team = %s', (season, team))
    returning_ids = {str(r[0]) for r in cursor.fetchall()}

    players = []
    for pid, fn, ln, pos, off_yds, tot, tfl, sacks, pd, ints in rows:
        dv = _rp_def_value(tot, tfl, sacks, ints, pd)
        share = (off_yds or 0) / off_tot + dv / def_tot
        if share <= 0:
            continue
        # Show whichever side the player mostly contributed on.
        if (off_yds or 0) / off_tot >= dv / def_tot:
            stat = f"{int(off_yds):,} yds"
        else:
            stat = f"{int(tot or 0)} tkl" + (f", {sacks:g} sk" if sacks else '')
        players.append({
            'id': int(pid) if str(pid).isdigit() else None,
            'name': f"{fn or ''} {ln or ''}".strip() or 'Unknown',
            'pos': pos or '', 'stat': stat, 'share': share,
            'returning': str(pid) in returning_ids,
        })
    players.sort(key=lambda p: p['share'], reverse=True)
    returning = [p for p in players if p['returning']][:limit]
    departed = [p for p in players if not p['returning']][:limit]
    return returning, departed


# ── NFL Talent ──────────────────────────────────────────────────────────────
@cache.memoize(timeout=86400)
def _team_nfl_talent(team):
    """Every player who finished their college career at this program and went
    on to the NFL (drafted or UDFA). All-time — not season-scoped. A player is
    attributed to his LAST college team (the program he was drafted from), so a
    transfer counts only for the school he left for the NFL, not every school he
    passed through. Drafted players are grouped into draft classes (newest
    first, earliest picks first within a class); UDFAs (which carry no draft
    year) are listed separately.

    Precomputed weekly into pool_store (key `nfltalent:{team}`) by precompute.py;
    on a miss it computes live and writes back (self-healing, like the pools)."""
    key = f"nfltalent:{team}"
    stored = _pool_store_get(key)
    if stored is not None:
        return stored
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('''
            SELECT p.id, p.first_name, p.last_name, p.position,
                   p.nfl_status, p.nfl_team, p.nfl_team_logo,
                   p.draft_year, p.draft_round, p.draft_pick, p.headshot
            FROM players p
            JOIN (
                SELECT DISTINCT ON (player_id) player_id, team
                FROM rosters
                ORDER BY player_id, season DESC
            ) last ON last.player_id = p.id
            WHERE last.team = %s AND p.nfl_status IN ('drafted', 'udfa')
        ''', (team,))
        rows = cur.fetchall()
    finally:
        release_db(conn)

    drafted, udfa = [], []
    for pid, fn, ln, pos, status, nfl_team, logo, yr, rnd, pick, headshot in rows:
        rec = {'id': pid, 'name': f"{fn or ''} {ln or ''}".strip(), 'pos': pos or '',
               'nfl_team': nfl_team, 'logo': logo, 'year': yr, 'round': rnd,
               'pick': pick, 'headshot': headshot}
        (drafted if status == 'drafted' else udfa).append(rec)

    drafted.sort(key=lambda d: (-(d['year'] or 0), d['round'] or 99, d['pick'] or 999))
    classes = []
    for yr, grp in groupby(drafted, key=lambda d: d['year']):
        classes.append({'year': yr, 'players': list(grp)})
    udfa.sort(key=lambda d: d['name'])

    result = {
        'classes': classes, 'udfa': udfa,
        'summary': {
            'draft_picks': len(drafted),
            'first_round': sum(1 for d in drafted if d['round'] == 1),
            'udfa': len(udfa),
            'total': len(drafted) + len(udfa),
        },
    }
    _pool_store_put(key, None, result)
    return result


@app.route('/team/<path:team_ref>')
@cache.cached(timeout=21600, query_string=True)  # 1 hour; season is in the query string
def team(team_ref):
    season = requested_season()
    is_current = season == CURRENT_SEASON
    conn = get_db()
    try:
        cursor = conn.cursor()
        # Canonical URLs use the slug (e.g. /team/north-texas). Old links that
        # passed the raw name (/team/North Texas) still resolve — they
        # 301-redirect to the slug so shared/bookmarked links don't break.
        cursor.execute('SELECT name FROM teams WHERE slug = %s', (team_ref,))
        _row = cursor.fetchone()
        if _row:
            team_name = _row[0]
        else:
            cursor.execute('SELECT slug FROM teams WHERE name = %s', (team_ref,))
            _old = cursor.fetchone()
            if _old:
                return redirect('/team/' + _old[0], code=301)
            return render_template('404.html', message=f'Team "{team_ref}" not found.'), 404

        ap_rankings = get_ap_rankings(cursor, season)
        team_rank = ap_rankings.get(team_name)

        # Championships won in the viewed season (national title first)
        cursor.execute('''
            SELECT award FROM awards
            WHERE team = %s AND season = %s AND kind = 'team'
            ORDER BY (award = 'National Champions') DESC, award
        ''', (team_name, season))
        team_awards = [r[0] for r in cursor.fetchall()]

        cursor.execute('SELECT name, conference, abbreviation, logo, color, alt_color, logo_dark FROM teams WHERE name = %s', (team_name,))
        team_info = cursor.fetchone()
        if not team_info:
            return render_template('404.html', message=f'Team "{team_name}" not found.'), 404

        # FCS teams live in the teams table only so their logo/name can render
        # when an FBS team plays them — they have no team page of their own.
        if team_info[1] in FCS_CONFS:
            return render_template('404.html',
                message=f'{team_name} is an FCS team — no team page is available.'), 404

        cursor.execute('''
            SELECT
                SUM(CASE WHEN (home_team=%s AND home_points>away_points) OR (away_team=%s AND away_points>home_points) THEN 1 ELSE 0 END),
                SUM(CASE WHEN (home_team=%s AND home_points<away_points) OR (away_team=%s AND away_points<home_points) THEN 1 ELSE 0 END)
            FROM games WHERE (home_team=%s OR away_team=%s) AND completed=1 AND season=%s AND season_type='SeasonType.REGULAR'
        ''', (team_name,)*6 + (season,))
        record = cursor.fetchone()

        cursor.execute('''
            SELECT COUNT(*),
                SUM(CASE WHEN home_team=%s THEN home_points ELSE away_points END),
                SUM(CASE WHEN home_team=%s THEN away_points ELSE home_points END)
            FROM games WHERE (home_team=%s OR away_team=%s) AND completed=1 AND season=%s AND season_type='SeasonType.REGULAR'
        ''', (team_name, team_name, team_name, team_name, season))
        g = cursor.fetchone()
        games_played = g[0] or 1
        pts_for = g[1] or 0
        pts_against = g[2] or 0

        cursor.execute("SELECT SUM(stat) FROM player_stats WHERE team=%s AND season=%s AND category='passing' AND stat_type='YDS'", (team_name, season))
        pass_yds = cursor.fetchone()[0] or 0
        cursor.execute("SELECT SUM(stat) FROM player_stats WHERE team=%s AND season=%s AND category='rushing' AND stat_type='YDS'", (team_name, season))
        rush_yds = cursor.fetchone()[0] or 0

        season_stats = {
            'games': games_played,
            'pass_yds_pg': round(pass_yds / games_played, 1),
            'rush_yds_pg': round(rush_yds / games_played, 1),
            'pts_for_pg':  round(pts_for / games_played, 1),
            'pts_against_pg': round(pts_against / games_played, 1),
        }

        # National ranks (FBS only) for the hero per-game stat cards — mirrors
        # the per-player hero's rank ordinals. Uses the same per-game
        # definitions as season_stats above; #1 = best (fewest for pts allowed).
        def _rank_of(values, higher_better=True):
            if team_name not in values:
                return None
            ordered = sorted(values.values(), reverse=higher_better)
            return ordered.index(values[team_name]) + 1

        cursor.execute('''
            SELECT s.team, COUNT(*) gp, SUM(s.pf) pf, SUM(s.pa) pa
            FROM (
                SELECT home_team AS team, home_points AS pf, away_points AS pa
                  FROM games WHERE completed=1 AND season=%s AND season_type='SeasonType.REGULAR'
                UNION ALL
                SELECT away_team AS team, away_points AS pf, home_points AS pa
                  FROM games WHERE completed=1 AND season=%s AND season_type='SeasonType.REGULAR'
            ) s
            JOIN teams t ON t.name = s.team AND t.conference NOT IN %s
            GROUP BY s.team
        ''', (season, season, FCS_CONFS))
        pf_pg, pa_pg, gp_map = {}, {}, {}
        for tm, gp, pf, pa in cursor.fetchall():
            if gp:
                pf_pg[tm], pa_pg[tm], gp_map[tm] = (pf or 0) / gp, (pa or 0) / gp, gp

        cursor.execute('''
            SELECT ps.team, ps.category, SUM(ps.stat)
            FROM player_stats ps
            JOIN teams t ON t.name = ps.team AND t.conference NOT IN %s
            WHERE ps.category IN ('passing','rushing') AND ps.stat_type='YDS' AND ps.season=%s
            GROUP BY ps.team, ps.category
        ''', (FCS_CONFS, season))
        yd = {}
        for tm, cat, yds in cursor.fetchall():
            yd.setdefault(tm, {})[cat] = yds or 0
        pass_pg = {tm: yd.get(tm, {}).get('passing', 0) / gp for tm, gp in gp_map.items()}
        rush_pg = {tm: yd.get(tm, {}).get('rushing', 0) / gp for tm, gp in gp_map.items()}

        hero_ranks = {
            'pts_for_pg':     _rank_of(pf_pg, higher_better=True),
            'pts_against_pg': _rank_of(pa_pg, higher_better=False),
            'pass_yds_pg':    _rank_of(pass_pg, higher_better=True),
            'rush_yds_pg':    _rank_of(rush_pg, higher_better=True),
        }

        standings = []
        if team_info[1]:
            cursor.execute('''
                SELECT t.name, t.logo,
                    SUM(CASE WHEN (g.home_team=t.name AND g.home_points>g.away_points) OR (g.away_team=t.name AND g.away_points>g.home_points) THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN (g.home_team=t.name AND g.home_points<g.away_points) OR (g.away_team=t.name AND g.away_points<g.home_points) THEN 1 ELSE 0 END) as losses,
                    SUM(CASE WHEN g.home_team=t.name THEN g.home_points ELSE CASE WHEN g.away_team=t.name THEN g.away_points ELSE 0 END END) as pf,
                    SUM(CASE WHEN g.home_team=t.name THEN g.away_points ELSE CASE WHEN g.away_team=t.name THEN g.home_points ELSE 0 END END) as pa,
                    t.logo_dark
                FROM teams t
                LEFT JOIN games g ON (g.home_team=t.name OR g.away_team=t.name)
                    AND g.completed=1 AND g.season=%s AND g.season_type='SeasonType.REGULAR'
                WHERE t.conference=%s
                GROUP BY t.name, t.logo, t.logo_dark ORDER BY wins DESC
            ''', (season, team_info[1]))
            standings = cursor.fetchall()

        # One query for the whole rivalries table instead of one per
        # schedule row (N+1 fix)
        rivalry_map = get_rivalry_map(cursor)

        def _team_schedule(season):
            # Opponent-centric schedule rows for one season. Trailing fields
            # (completed, kickoff_date, kickoff_time) let the template render
            # 2025 results and upcoming 2026 kickoffs from the same markup.
            cursor.execute('''
                SELECT g.id,
                    CASE WHEN g.home_team=%s THEN 'home' ELSE 'away' END,
                    CASE WHEN g.home_team=%s THEN g.away_team ELSE g.home_team END,
                    CASE WHEN g.home_team=%s THEN t2.logo ELSE t1.logo END,
                    CASE WHEN g.home_team=%s THEN g.home_points ELSE g.away_points END,
                    CASE WHEN g.home_team=%s THEN g.away_points ELSE g.home_points END,
                    g.week, g.season_type, g.notes,
                    CASE WHEN g.home_team=%s THEN t2.logo_dark ELSE t1.logo_dark END,
                    g.completed, g.start_date, COALESCE(g.start_time_tbd, 0)
                FROM games g
                LEFT JOIN teams t1 ON g.home_team=t1.name
                LEFT JOIN teams t2 ON g.away_team=t2.name
                WHERE (g.home_team=%s OR g.away_team=%s) AND g.season=%s
                ORDER BY CASE WHEN g.season_type='SeasonType.REGULAR' THEN 0 ELSE 1 END, g.week
            ''', (team_name,)*8 + (season,))
            out = []
            for r in cursor.fetchall():
                kd, kt = format_kickoff(r[11], r[12])
                rivalry = rivalry_map.get((team_name, r[2]), '')
                out.append(r[:10] + (r[10], kd, kt, rivalry))
            return out

        schedule = _team_schedule(season)
        schedule_next = _team_schedule(UPCOMING_SEASON) if is_current else []

        # The current view shows the upcoming roster (who's on the team next
        # season); a historical view shows that season's actual roster.
        roster_season = UPCOMING_SEASON if is_current else season
        cursor.execute('''
            SELECT p.first_name, p.last_name, r.position, r.jersey, p.id, p.headshot,
                   r.height, r.weight, r.class_year, COALESCE(p.redshirt, 0)
            FROM rosters r
            JOIN players p ON p.id = r.player_id
            WHERE r.team=%s AND r.season=%s
            ORDER BY
                CASE r.position
                    WHEN 'QB' THEN 1 WHEN 'RB' THEN 2 WHEN 'HB' THEN 2 WHEN 'FB' THEN 2
                    WHEN 'WR' THEN 3 WHEN 'TE' THEN 4
                    WHEN 'OL' THEN 5 WHEN 'OT' THEN 5 WHEN 'OG' THEN 5
                    WHEN 'LT' THEN 5 WHEN 'LG' THEN 5 WHEN 'C' THEN 5
                    WHEN 'RG' THEN 5 WHEN 'RT' THEN 5
                    WHEN 'DE' THEN 6 WHEN 'EDGE' THEN 6
                    WHEN 'DT' THEN 7 WHEN 'NT' THEN 7 WHEN 'DL' THEN 7
                    WHEN 'LB' THEN 8 WHEN 'ILB' THEN 8 WHEN 'OLB' THEN 8 WHEN 'MLB' THEN 8
                    WHEN 'CB' THEN 9 WHEN 'DB' THEN 10
                    WHEN 'S' THEN 10 WHEN 'SS' THEN 10 WHEN 'FS' THEN 10 WHEN 'SAF' THEN 10
                    WHEN 'K' THEN 11 WHEN 'P' THEN 12 WHEN 'LS' THEN 13
                    ELSE 14 END, p.last_name
        ''', (team_name, roster_season))
        roster = cursor.fetchall()

        # Starters are picked from real 2025 usage/production keyed on
        # player_id (transfer-aware — see compute_starter_scores), not from
        # roster order. Pass the full roster tuples so OL seniority (the
        # year column) is available to the scorer and the slotter.
        # The projected-starters lineup is a forward projection (EA ratings +
        # last season's production), so it only renders on the current view —
        # historical rosters get the roster table without a projection.
        lineup = {}
        if is_current:
            starter_scores = compute_starter_scores(cursor, roster)
            # Specific EA positions (LT/LG/C/RG/RT, LE/RE, MLB/LOLB/ROLB, CB/FS/SS…)
            # so players slot into their actual spot, not just a generic OL/DL pool.
            ea_pos = {}
            roster_int_ids = [int(p[4]) for p in roster if p[4] is not None]
            if roster_int_ids:
                try:
                    cursor.execute('SELECT player_id, position FROM ea_ratings '
                                   'WHERE player_id = ANY(%s) AND position IS NOT NULL', (roster_int_ids,))
                    ea_pos = {str(pid): pos.upper() for pid, pos in cursor.fetchall()}
                except Exception:
                    cursor.connection.rollback()  # ea_ratings not populated yet
            lineup = build_lineup(roster, starter_scores, ea_pos)

        cursor.execute('SELECT player_name, category, stat_type, stat FROM player_stats WHERE team=%s AND season=%s', (team_name, season))
        all_stats = pivot_stats(cursor.fetchall())

        passing_stats     = sort_players(all_stats.get('passing', {}),    'YDS')
        rushing_stats     = sort_players(all_stats.get('rushing', {}),    'YDS')
        receiving_stats   = sort_players(all_stats.get('receiving', {}),  'YDS')
        defensive_stats   = sort_players(all_stats.get('defensive', {}),  'TOT')
        kicking_stats     = sort_players(all_stats.get('kicking', {}),    'FGM')
        punting_stats     = sort_players(all_stats.get('punting', {}),    'YDS')
        kick_return_stats = sort_players(all_stats.get('kickReturns', {}), 'YDS')
        punt_return_stats = sort_players(all_stats.get('puntReturns', {}), 'YDS')

        # The defensive feed doesn't carry assisted tackles or interceptions
        # directly: AST is derived (TOT = SOLO + AST), and INT lives in the
        # separate 'interceptions' category. A defender who played but had no
        # pick has a real INT of 0 (not missing), so default to 0 here.
        # AST is derived (TOT = SOLO + AST); INT comes from the separate
        # 'interceptions' category. NOTE: player_ppa (EPA/play) is offense-only
        # in CFBD — defenders have no PPA row — so there is no defensive EPA/
        # rating column to add here; we don't fabricate one.
        _ints = all_stats.get('interceptions', {})
        for _p in defensive_stats:
            _tot, _solo = _p.get('TOT'), _p.get('SOLO')
            if _tot is not None and _solo is not None:
                _p['AST'] = _tot - _solo
            _p['INT'] = (_ints.get(_p['name']) or {}).get('INT', 0)

        # Add headshots and player IDs to stat tables.
        # Key off player_stats' own stable player_id (which every stat row
        # carries) rather than the current players.team, so players who
        # transferred OUT — whose stats still belong to this team but whose
        # players row now lists their new team — still resolve to a headshot
        # and a clickable /player/<id> link instead of a broken entry.
        cursor.execute('''
            SELECT DISTINCT ps.player_name, ps.player_id, p.headshot
            FROM player_stats ps
            LEFT JOIN players p ON p.id::text = ps.player_id
            WHERE ps.team = %s AND ps.season = %s
        ''', (team_name, season))
        _player_rows = cursor.fetchall()
        headshot_map   = {row[0]: row[2] for row in _player_rows}
        player_id_map  = {row[0]: row[1] for row in _player_rows}

        def add_headshots(players):
            for p in players:
                p['headshot']   = headshot_map.get(p['name'])
                p['player_id']  = player_id_map.get(p['name'])
            return players

        passing_stats     = add_headshots(passing_stats)
        rushing_stats     = add_headshots(rushing_stats)
        receiving_stats   = add_headshots(receiving_stats)
        defensive_stats   = add_headshots(defensive_stats)
        kicking_stats     = add_headshots(kicking_stats)
        punting_stats     = add_headshots(punting_stats)
        kick_return_stats = add_headshots(kick_return_stats)
        punt_return_stats = add_headshots(punt_return_stats)

        # Normalize PCT from decimal to percentage (DB stores 0.648, display needs 64.8)
        for p in passing_stats:
            if p.get('PCT') is not None and float(p.get('PCT', 0)) <= 1.0:
                p['PCT'] = round(float(p['PCT']) * 100, 1)
            if not p.get('YPA'):
                yds = float(p.get('YDS', 0) or 0)
                att = float(p.get('ATT', 0) or 0)
                if att > 0:
                    p['YPA'] = round(yds / att, 1)
        for p in kicking_stats:
            if p.get('PCT') is not None and float(p.get('PCT', 0)) <= 1.0:
                p['PCT'] = round(float(p['PCT']) * 100, 1)

        # Precomputed once per season (all teams) and read here — a copy so the
        # havoc merge below never mutates the shared cached object.
        percentiles = dict(_team_percentiles_all(season).get(team_name, {}))

        cursor.execute('SELECT * FROM team_stats WHERE team=%s AND season=%s', (team_name, season))
        ts = cursor.fetchone()
        team_adv = None
        if ts:
            team_adv = {
                'off_plays':                ts[1],
                'def_plays':                ts[18],
                'off_ppa':                  round(ts[3], 3)  if ts[3]  else None,
                'off_success_rate':         round(ts[5]*100, 1) if ts[5] else None,
                'off_explosiveness':        round(ts[6], 3)  if ts[6]  else None,
                'off_power_success':        round(ts[7]*100, 1) if ts[7] else None,
                'off_stuff_rate':           round(ts[8]*100, 1) if ts[8] else None,
                'off_line_yards':           round(ts[9], 2)  if ts[9]  else None,
                'off_second_level_yards':   round(ts[11], 2) if ts[11] else None,
                'off_open_field_yards':     round(ts[10], 2) if ts[10] else None,
                'off_rush_ppa':             round(ts[12], 3) if ts[12] else None,
                'off_pass_ppa':             round(ts[13], 3) if ts[13] else None,
                'off_rush_sr':              round(ts[14]*100, 1) if ts[14] else None,
                'off_pass_sr':              round(ts[15]*100, 1) if ts[15] else None,
                'off_rush_exp':             round(ts[16], 3) if ts[16] else None,
                'off_pass_exp':             round(ts[17], 3) if ts[17] else None,
                'def_ppa':                  round(ts[20], 3) if ts[20] else None,
                'def_success_rate':         round(ts[22]*100, 1) if ts[22] else None,
                'def_explosiveness':        round(ts[23], 3) if ts[23] else None,
                'def_power_success':        round(ts[24]*100, 1) if ts[24] else None,
                'def_stuff_rate':           round(ts[25]*100, 1) if ts[25] else None,
                'def_line_yards':           round(ts[26], 2) if ts[26] else None,
                'def_second_level_yards':   round(ts[28], 2) if ts[28] else None,
                'def_open_field_yards':     round(ts[27], 2) if ts[27] else None,
                'def_rush_ppa':             round(ts[29], 3) if ts[29] else None,
                'def_pass_ppa':             round(ts[30], 3) if ts[30] else None,
                'def_rush_sr':              round(ts[31]*100, 1) if ts[31] else None,
                'def_pass_sr':              round(ts[32]*100, 1) if ts[32] else None,
                'def_rush_exp':             round(ts[33], 3) if ts[33] else None,
                'def_pass_exp':             round(ts[34], 3) if ts[34] else None,
            }

        # NOTE: the block below used to be nested inside `if ts:`, which meant
        # brand-new FBS programs with no team_stats row yet (e.g. teams just
        # joining from FCS) fell through with no return statement at all —
        # a 500 error. sp/recruiting/havoc are independent lookups that each
        # already null-check their own row, so they run unconditionally now.
        cursor.execute('SELECT rating, ranking, offense_rating, offense_ranking, defense_rating, defense_ranking, special_teams_rating FROM sp_ratings WHERE team=%s AND season=%s', (team_name, season))
        sp_row = cursor.fetchone()
        sp = None
        if sp_row:
            sp = {
                'rating':           round(sp_row[0], 1) if sp_row[0] else None,
                'ranking':          sp_row[1],
                'off_rating':       round(sp_row[2], 1) if sp_row[2] else None,
                'off_ranking':      sp_row[3],
                'def_rating':       round(sp_row[4], 1) if sp_row[4] else None,
                'def_ranking':      sp_row[5],
                'st_rating':        round(sp_row[6], 1) if sp_row[6] else None,
            }

        # Savant Rating (SVR) — the site's proprietary opponent-adjusted
        # points-per-10-drives model (computed by compute_savant_ratings.py)
        cursor.execute('''
            SELECT off_rating, off_ranking, def_rating, def_ranking,
                   net_rating, net_ranking, sos, games
            FROM savant_ratings WHERE team=%s AND season=%s
        ''', (team_name, season))
        svr_row = cursor.fetchone()
        svr = None
        if svr_row:
            svr = {
                'off_rating':  round(svr_row[0], 1) if svr_row[0] is not None else None,
                'off_ranking': svr_row[1],
                'def_rating':  round(svr_row[2], 1) if svr_row[2] is not None else None,
                'def_ranking': svr_row[3],
                'net_rating':  round(svr_row[4], 1) if svr_row[4] is not None else None,
                'net_ranking': svr_row[5],
                'sos':         round(svr_row[6], 1) if svr_row[6] is not None else None,
                'games':       svr_row[7],
            }

        # Weekly Savant snapshots (compute_savant_ratings writes one per cron
        # run in-season): the latest week labels the displayed rating as
        # "Through Week N", and the full series drives the within-season
        # progression chart on the Trends tab. Seasons without snapshots
        # (2016–2024) show neither — their end-of-season display is unchanged.
        # Week 20 is the postseason sentinel: the season is complete, so no
        # "through week" label applies.
        svr_week = None
        svr_weekly = None
        try:
            cursor.execute('''
                SELECT week, off_rating, def_rating, net_rating, net_ranking
                FROM savant_weekly WHERE team=%s AND season=%s ORDER BY week
            ''', (team_name, season))
            wrows = cursor.fetchall()
            if wrows:
                last_wk = wrows[-1][0]
                svr_week = last_wk if last_wk < 20 else None
                if len(wrows) >= 2:
                    svr_weekly = {
                        'weeks': [('Post' if w == 20 else w) for w, *_ in wrows],
                        'off':  [r[1] for r in wrows],
                        'def':  [r[2] for r in wrows],
                        'net':  [r[3] for r in wrows],
                        'rank': [r[4] for r in wrows],
                    }
        except Exception:
            conn.rollback()   # savant_weekly absent on a fresh DB

        # Recruiting rankings trend
        cursor.execute('''
            SELECT year, rank, points FROM team_recruiting
            WHERE team=%s AND year >= 2022 ORDER BY year DESC
        ''', (team_name,))
        recruiting = [{'year': r[0], 'rank': r[1], 'points': round(r[2], 1) if r[2] else None}
                      for r in cursor.fetchall()]

        # Havoc + field position (from team_advanced) — just this team's row.
        # The havoc/field-position percentiles are already merged into
        # `percentiles` by _team_percentiles_all(season) above, so there's no
        # need to pull the whole field here anymore.
        cursor.execute('SELECT * FROM team_advanced WHERE season=%s AND team=%s', (season, team_name))
        adv_cols = [d[0] for d in cursor.description]
        _adv = cursor.fetchone()
        adv_row = dict(zip(adv_cols, _adv)) if _adv else None
        havoc = None
        if adv_row and adv_row.get('def_havoc_total') is not None:
            havoc = {
                'total':   round(adv_row['def_havoc_total'] * 100, 1),
                'front7':  round(adv_row['def_havoc_front7'] * 100, 1) if adv_row['def_havoc_front7'] else None,
                'db':      round(adv_row['def_havoc_db'] * 100, 1) if adv_row['def_havoc_db'] else None,
                'off_fp':  round(adv_row['off_field_pos_avg_start'], 1) if adv_row['off_field_pos_avg_start'] else None,
                'def_fp':  round(adv_row['def_field_pos_avg_start'], 1) if adv_row['def_field_pos_avg_start'] else None,
                'scoring_opps': adv_row['off_scoring_opps'],
                'pts_per_opp': round(adv_row['off_pts_per_opp'], 2) if adv_row['off_pts_per_opp'] else None,
            }

        conf_logo = get_conference_logos(cursor).get(team_info[1])

        # ── Trends tab: this program's full multi-season history ──────────
        # One row per loaded season per metric; missing seasons stay None so
        # the charts render honest gaps instead of misleading zeros.
        all_seasons = sorted(get_available_seasons())

        def _series(query, params, cols):
            cursor.execute(query, params)
            by = {r[0]: r[1:] for r in cursor.fetchall()}
            return [[(by[s][i] if s in by and by[s][i] is not None else None)
                     for s in all_seasons] for i in range(cols)]

        sv_off, sv_def, sv_net, sv_rank = _series(
            'SELECT season, off_rating, def_rating, net_rating, net_ranking '
            'FROM savant_ratings WHERE team=%s', (team_name,), 4)
        sp_rt, sp_off, sp_def, sp_rank = _series(
            'SELECT season, rating, offense_rating, defense_rating, ranking '
            'FROM sp_ratings WHERE team=%s', (team_name,), 4)
        rec_rank, rec_pts = _series(
            'SELECT year, rank, points FROM team_recruiting WHERE team=%s', (team_name,), 2)
        ap_rank, = _series(
            'SELECT season, rank FROM ap_rankings WHERE team=%s', (team_name,), 1)
        epa_off, epa_def = _series(
            'SELECT season, off_ppa, def_ppa FROM team_stats WHERE team=%s', (team_name,), 2)
        t_wins, t_losses = _series('''
            SELECT season,
                SUM(CASE WHEN (home_team=%s AND home_points>away_points)
                          OR (away_team=%s AND away_points>home_points) THEN 1 ELSE 0 END),
                SUM(CASE WHEN (home_team=%s AND home_points<away_points)
                          OR (away_team=%s AND away_points<home_points) THEN 1 ELSE 0 END)
            FROM games WHERE (home_team=%s OR away_team=%s) AND completed=1
            GROUP BY season''', (team_name,)*6, 2)
        # Conference per season (from that season's stat rows) — realignment
        # context for hover tooltips and the header note.
        confs, = _series(
            'SELECT season, MIN(conference) FROM player_stats '
            'WHERE team=%s AND conference IS NOT NULL GROUP BY season', (team_name,), 1)

        trends = {
            'seasons': all_seasons,
            'weekly': svr_weekly,   # within-season progression (None pre-2025)
            'savant': {'off': sv_off, 'def': sv_def, 'net': sv_net, 'rank': sv_rank},
            'sp': {'rating': sp_rt, 'off': sp_off, 'def': sp_def, 'rank': sp_rank},
            'recruiting': {'rank': rec_rank, 'points': rec_pts},
            'ap': ap_rank,
            'epa': {'off': epa_off, 'def': epa_def},
            'record': {'wins': t_wins, 'losses': t_losses},
            'conference': confs,
        }

        # Returning production — only when the prior season's data exists
        # (e.g. not computable for 2016, the earliest loaded year).
        returning = None
        if (season - 1) in get_available_seasons():
            rp = _returning_production_ranks(season)
            td = rp['teams'].get(team_name)
            if td and (td.get('off_pct') is not None or td.get('def_pct') is not None):
                ret_list, dep_list = _returning_breakdown(cursor, team_name, season - 1, season)
                returning = {
                    'prior': season - 1, 'n': rp['counts'].get('off_rank'),
                    'off_pct': td.get('off_pct'), 'def_pct': td.get('def_pct'),
                    'overall_pct': td.get('overall_pct'),
                    'off_rank': td.get('off_rank'), 'def_rank': td.get('def_rank'),
                    'overall_rank': td.get('overall_rank'),
                    'returning_list': ret_list, 'departed_list': dep_list,
                }

        # NFL Talent — all-time draft/UDFA alumni (not season-scoped)
        nfl_talent = _team_nfl_talent(team_name)

        return render_template('team.html',
                team=team_info, record=record, season_stats=season_stats,
                returning=returning, nfl_talent=nfl_talent,
                hero_ranks=hero_ranks,
                season=season, is_current_season=is_current,
                roster_season=roster_season, next_season=UPCOMING_SEASON,
                available_seasons=get_available_seasons(),
                trends=trends,
                standings=standings, schedule=schedule, schedule_next=schedule_next,
                roster=roster, lineup=lineup,
                passing_stats=passing_stats, rushing_stats=rushing_stats,
                receiving_stats=receiving_stats, defensive_stats=defensive_stats,
                kicking_stats=kicking_stats, punting_stats=punting_stats,
                kick_return_stats=kick_return_stats, punt_return_stats=punt_return_stats,
                team_adv=team_adv, percentiles=percentiles, sp=sp, svr=svr,
                svr_week=svr_week,
                ap_rankings=ap_rankings, team_rank=team_rank,
                team_awards=team_awards,
                recruiting=recruiting, havoc=havoc, conf_logo=conf_logo)
    finally:
        release_db(conn)

@app.route('/api/players')
def api_players():
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify([])
    # Optional position filter (used by the compare page's position tabs).
    # Backward compatible: the navbar search sends no `pos`, so it is ignored.
    pos = request.args.get('pos', '').strip().upper()
    pos_cols = POSITION_GROUPS.get(pos)
    conn = get_db()
    try:
        cursor = conn.cursor()
        if pos_cols:
            pos_ph = ','.join(['%s'] * len(pos_cols))
            cursor.execute(f'''
                SELECT p.id, p.first_name, p.last_name, p.team, p.position,
                       p.jersey, p.headshot, t.logo_dark, 'player' as result_type
                FROM players p
                INNER JOIN teams t ON p.team = t.name
                WHERE ((p.first_name || ' ' || p.last_name) ILIKE %s OR p.last_name ILIKE %s)
                  AND p.position IN ({pos_ph})
                ORDER BY p.last_name, p.first_name
                LIMIT 6
            ''', (f'%{q}%', f'{q}%', *pos_cols))
            player_rows = cursor.fetchall()
            team_rows = []  # a position filter means the user is picking a player
        else:
            cursor.execute('''
                SELECT p.id, p.first_name, p.last_name, p.team, p.position,
                       p.jersey, p.headshot, t.logo_dark, 'player' as result_type
                FROM players p
                INNER JOIN teams t ON p.team = t.name
                WHERE (p.first_name || ' ' || p.last_name) ILIKE %s
                   OR p.last_name ILIKE %s
                ORDER BY p.last_name, p.first_name
                LIMIT 6
            ''', (f'%{q}%', f'{q}%'))
            player_rows = cursor.fetchall()
            cursor.execute('''
                SELECT name, conference, logo_dark, color, 'team' as result_type
                FROM teams
                WHERE (name ILIKE %s OR abbreviation ILIKE %s)
                  AND conference IS NOT NULL AND conference <> ALL(%s)
                ORDER BY name
                LIMIT 4
            ''', (f'%{q}%', f'%{q}%', list(FCS_CONFS)))
            team_rows = cursor.fetchall()
    finally:
        release_db(conn)
    results = []
    for r in team_rows:
        results.append({'type': 'team', 'name': r[0], 'conference': r[1],
                        'logo': r[2], 'color': r[3], 'url': f'/team/{slugify_team(r[0])}'})
    for r in player_rows:
        results.append({'type': 'player', 'id': r[0], 'first': r[1], 'last': r[2],
                        'team': r[3], 'pos': r[4], 'jersey': r[5],
                        'headshot': r[6], 'logo': r[7], 'url': f'/player/{r[0]}'})
    return jsonify(results)

@app.route('/rankings')
@cache.cached(timeout=21600, query_string=True)  # 1 hour; season is in the query string
def rankings():
    season = requested_season()

    def poll_label(wk, st):
        if st == 'postseason':
            return 'Final'
        return 'Preseason' if wk == 1 else f'Week {wk}'

    conn = get_db()
    try:
        cursor = conn.cursor()
        # Every poll this season has, chronological (regular weeks, then final).
        cursor.execute('SELECT DISTINCT week, season_type FROM ap_rankings WHERE season = %s', (season,))
        polls = sorted(cursor.fetchall(), key=lambda r: (0 if r[1] == 'regular' else 1, r[0]))

        sel_week = sel_type = None
        rows = []
        if polls:
            # Selected poll from ?w=<season_type>-<week>; default to the final.
            want = request.args.get('w', '')
            chosen = next(((wk, st) for wk, st in polls if f'{st}-{wk}' == want), polls[-1])
            sel_week, sel_type = chosen
            is_post = sel_type == 'postseason'

            # Win/loss record AS OF this poll: the poll for ranking week W
            # reflects results through week W-1, so count regular games with
            # week < W; the final poll counts the whole season (bowls included).
            cursor.execute('''
                SELECT a.rank, a.team, a.points, a.first_place_votes, a.week,
                       t.logo, t.conference, t.color,
                       sp.rating, sp.ranking as sp_rank,
                       SUM(CASE WHEN (g.home_team=a.team AND g.home_points>g.away_points) OR (g.away_team=a.team AND g.away_points>g.home_points) THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN (g.home_team=a.team AND g.home_points<g.away_points) OR (g.away_team=a.team AND g.away_points<g.home_points) THEN 1 ELSE 0 END) as losses,
                       a.prev_rank, t.logo_dark, t.alt_color
                FROM ap_rankings a
                LEFT JOIN teams t ON a.team = t.name
                LEFT JOIN sp_ratings sp ON a.team = sp.team AND sp.season = a.season
                LEFT JOIN games g ON (g.home_team=a.team OR g.away_team=a.team)
                    AND g.completed=1 AND g.season = a.season
                    AND (%(post)s OR (g.season_type='SeasonType.REGULAR' AND g.week < %(wk)s))
                WHERE a.season = %(season)s AND a.week = %(wk)s AND a.season_type = %(stype)s
                GROUP BY a.rank, a.team, a.points, a.first_place_votes, a.week, a.prev_rank,
                         t.logo, t.conference, t.color, t.logo_dark, t.alt_color,
                         sp.rating, sp.ranking
                ORDER BY a.rank
            ''', {'season': season, 'wk': sel_week, 'stype': sel_type, 'post': is_post})
            rows = cursor.fetchall()
    finally:
        release_db(conn)

    poll_options = [{'value': f'{st}-{wk}', 'label': poll_label(wk, st),
                     'selected': (wk == sel_week and st == sel_type)} for wk, st in polls]
    return render_template('rankings.html', rankings=rows,
                           poll_options=poll_options,
                           sel_label=poll_label(sel_week, sel_type) if sel_week else None,
                           sel_is_final=(sel_type == 'postseason'),
                           season=season, available_seasons=get_available_seasons())


# ── Drives tab classification helpers ──────────────────────────────────────
# ESPN's play type.text values surveyed across several games (regular season
# and bowls): Kickoff, Kickoff Return (Offense), Timeout, End Period, End of
# Half, End of Game, Rush, Rushing Touchdown, Pass Reception, Passing
# Touchdown, Pass Incompletion, Pass Interception Return, Interception, Sack,
# Fumble, Fumble Recovery (Own/Opponent), Fumble Return Touchdown, Penalty,
# Punt, Punt Return, Blocked Punt Touchdown, Field Goal Good, Field Goal
# Missed, Blocked Field Goal, Safety.
_NON_SCRIMMAGE_TYPES = ('kickoff', 'timeout', 'end period', 'end of half', 'end of game', 'end of quarter')

def _classify_play(play_type_text):
    """(label, color, is_turnover) for one play, or None to skip the play
    entirely (kickoffs/timeouts/period markers aren't scrimmage snaps)."""
    t = (play_type_text or '').lower()
    if any(s in t for s in _NON_SCRIMMAGE_TYPES):
        return None
    if 'interception' in t:
        return ('INT', '#dc2626', True)
    if 'fumble' in t:
        if 'recovery (own)' in t:
            return ('Fumble', '#6b7280', False)
        return ('FUM', '#dc2626', True)
    if 'sack' in t:
        return ('Sack', '#ef4444', False)
    if 'safety' in t:
        return ('Safety', '#dc2626', False)
    if 'penalty' in t:
        return ('Penalty', '#eab308', False)
    if 'field goal' in t:
        if 'missed' in t or 'blocked' in t:
            return ('FG Miss', '#6b7280', False)
        return ('FG', '#f97316', False)
    if 'punt' in t:
        return ('Punt', '#a855f7', False)
    if 'incompletion' in t:
        return ('Inc', '#6b7280', False)
    if 'reception' in t or 'pass' in t:
        return ('Pass', '#3b82f6', False)
    if 'rush' in t or 'run' in t or 'kneel' in t:
        return ('Rush', '#22c55e', False)
    return ('Play', '#6b7280', False)

def _classify_drive_result(display_result):
    """(badge_label, bg_color, text_color) for a drive's header badge."""
    r = (display_result or '').lower()
    if 'fumble' in r or 'interception' in r or 'pick' in r:
        return ('TURNOVER', '#dc2626', '#fff')
    if 'safety' in r:
        return ('SAFETY', '#dc2626', '#fff')
    if 'touchdown' in r:
        return ('TOUCHDOWN', '#16a34a', '#fff')
    if 'missed' in r and 'field goal' in r or r == 'missed fg':
        return ('MISSED FG', 'rgba(255,255,255,0.08)', 'rgba(255,255,255,0.5)')
    if 'field goal' in r:
        return ('FIELD GOAL', '#f97316', '#fff')
    if 'punt' in r:
        return ('PUNT', 'rgba(255,255,255,0.1)', 'rgba(255,255,255,0.6)')
    if 'downs' in r:
        return ('DOWNS', 'rgba(255,255,255,0.08)', 'rgba(255,255,255,0.5)')
    if display_result:
        return (display_result.upper(), 'rgba(255,255,255,0.08)', 'rgba(255,255,255,0.5)')
    return ('—', 'rgba(255,255,255,0.08)', 'rgba(255,255,255,0.5)')


@app.route('/game/<int:game_id>')
@cache.cached(timeout=3600)
def game_detail(game_id):
    conn = get_db()
    try:
        cursor = conn.cursor()

        cursor.execute('''
            SELECT g.id, g.home_team, g.away_team, g.home_points, g.away_points,
                   g.week, g.season_type, g.notes, g.start_date,
                   t1.logo_dark, t2.logo_dark, t1.color, t2.color,
                   t1.alt_color, t2.alt_color,
                   g.completed, COALESCE(g.start_time_tbd, 0), g.season,
                   t1.conference, t2.conference
            FROM games g
            LEFT JOIN teams t1 ON g.home_team = t1.name
            LEFT JOIN teams t2 ON g.away_team = t2.name
            WHERE g.id = %s
        ''', (game_id,))
        game_info = cursor.fetchone()
        if not game_info:
            return render_template('404.html', message='Game not found.'), 404

        home_team = game_info[1]
        away_team = game_info[2]
        game_season = game_info[17] or CURRENT_SEASON
        # FCS opponents have no team page — only link the ones we know are FBS.
        home_is_fbs = bool(game_info[18]) and game_info[18] not in FCS_CONFS
        away_is_fbs = bool(game_info[19]) and game_info[19] not in FCS_CONFS
        # AP ranks each team carried INTO this game — the weekly poll as-of the
        # game's week (final poll for postseason), empty until that poll exists.
        _is_post = 'POST' in str(game_info[6] or '').upper()
        ap_rankings = ap_asof(get_ap_week_map(cursor, game_season), game_info[5], _is_post)
        rivalry_name = get_rivalry(cursor, home_team, away_team)

        # Scheduled-but-unplayed game (e.g. the 2026 season): there's no box
        # score, drives, win probability, or ESPN summary yet. Render a compact
        # "upcoming" page with the kickoff instead of doing the (fruitless,
        # multi-second) ESPN fetches the completed-game path relies on.
        is_scheduled = not game_info[15]

        records = {}
        name_to_player_id = {}
        summary_row = None
        if not is_scheduled:
            def _record(team):
                # Record through this game (inclusive), ordered CHRONOLOGICALLY.
                # Game ids are NOT time-ordered (a week-1 game can have a higher
                # id than a week-10 one), so compare by (start_date, id) against
                # this game's — an `id <= game_id` filter counted later games and
                # produced records like Army 2-1 on a week-1 loss.
                cursor.execute('''
                    SELECT
                        SUM(CASE WHEN (home_team=%s AND home_points>away_points) OR (away_team=%s AND away_points>home_points) THEN 1 ELSE 0 END),
                        SUM(CASE WHEN (home_team=%s AND home_points<away_points) OR (away_team=%s AND away_points<home_points) THEN 1 ELSE 0 END)
                    FROM games
                    WHERE (home_team=%s OR away_team=%s) AND home_points IS NOT NULL AND away_points IS NOT NULL
                      AND season = %s
                      AND (start_date, id) <= (SELECT start_date, id FROM games WHERE id = %s)
                ''', (team, team, team, team, team, team, game_season, game_id))
                row = cursor.fetchone()
                return (row[0] or 0, row[1] or 0) if row else (0, 0)

            records = {'home': _record(home_team), 'away': _record(away_team)}

            cursor.execute('''
                SELECT (first_name || ' ' || last_name), id
                FROM players WHERE team IN (%s, %s)
            ''', (home_team, away_team))
            name_to_player_id = {row[0].lower(): row[1] for row in cursor.fetchall()}

            # Stored ESPN summary (fetch_game_summaries.py) — completed games are
            # immutable, so pages render from Postgres with no ESPN call
            try:
                cursor.execute('SELECT summary_gz FROM game_summaries WHERE game_id = %s', (game_id,))
                summary_row = cursor.fetchone()
            except Exception:
                conn.rollback()  # table not created yet — fall back to live fetch
    finally:
        release_db(conn)

    if is_scheduled:
        # Kickoff in Eastern; time is 'TBD' when CFBD hasn't set one yet.
        kick_date, kick_time = format_kickoff(game_info[8], game_info[16])
        season_type_raw = game_info[6] or ''
        # Savant Forecast — precomputed weekly by predict_games.py; a game
        # without a row (e.g. FCS opponent) simply shows no forecast block.
        forecast = None
        conn2 = get_db()
        try:
            c2 = conn2.cursor()
            c2.execute('SELECT home_prob, predicted_margin FROM game_predictions '
                       'WHERE game_id = %s AND scored = 0', (game_id,))
            fr = c2.fetchone()
            if fr and fr[0] is not None:
                forecast = {'home_prob': fr[0], 'margin': fr[1]}
        except Exception:
            conn2.rollback()   # table absent on a fresh DB — degrade to no block
        finally:
            release_db(conn2)
        return render_template('game.html',
            game=game_info, home_team=home_team, away_team=away_team,
            forecast=forecast,
            home_is_fbs=home_is_fbs, away_is_fbs=away_is_fbs,
            # As-of-week ranks (per-season now) — empty until that week's poll
            # exists, so upcoming games show ranks once the poll is out.
            ap_rankings=ap_rankings, rivalry_name=rivalry_name,
            is_scheduled=True,
            kickoff_date=kick_date, kickoff_time=kick_time,
            season_type_display='Postseason' if 'POST' in str(season_type_raw).upper() else 'Regular Season',
            season_year=game_info[17], game_season=game_season,
            week_num=game_info[5], notes=game_info[7] or '',
            game_date=kick_date, game_time=kick_time,
            records={}, espn_game_id=None,
            # Empty defaults so the shared template never references missing data
            quarters={'home': [], 'away': []}, venue={}, attendance=None,
            venue_name='', venue_location='', attendance_fmt='', tv_broadcast='',
            plays=[], team_stats=[], home_stats={}, away_stats={},
            player_stats=[], leaders={}, drives=[], box_score={'home': {}, 'away': {}},
            structured_leaders={}, win_prob=[], top_wpa=[])

    espn_game_id = None
    quarters = {'home': [], 'away': []}
    venue = {}
    attendance = None
    venue_name = ''
    venue_location = ''
    attendance_fmt = ''
    tv_broadcast = ''
    plays = []
    team_stats = []
    player_stats = []
    leaders = {}
    win_prob = []
    drives = []
    box_score = {'home': {}, 'away': {}}
    home_stats = {}
    away_stats = {}

    try:
        data = {}
        if summary_row:
            data = json.loads(gzip.decompress(bytes(summary_row[0])))
        else:
            # Not stored yet (e.g. game just completed) — fetch live. CFBD
            # game ids are ESPN event ids, so ask the summary endpoint
            # directly rather than scanning the scoreboard by date, which
            # silently missed every prime-time game (kickoffs after 00:00
            # UTC land on the next day's slate).
            s = req.get(
                'https://site.api.espn.com/apis/site/v2/sports/football/college-football/summary',
                params={'event': game_id},
                timeout=4
            )
            if s.ok:
                data = s.json()
                # Store it (completed games are immutable) so no future
                # request ever repeats this fetch — the live ESPN call was
                # the game page's thread-blocking cost under crawler load.
                if game_info[15] and data.get('header', {}).get('competitions'):
                    connS = get_db()
                    try:
                        curS = connS.cursor()
                        curS.execute('''
                            INSERT INTO game_summaries (game_id, summary_gz)
                            VALUES (%s, %s) ON CONFLICT (game_id) DO NOTHING
                        ''', (game_id, gzip.compress(json.dumps(data).encode())))
                        connS.commit()
                    except Exception:
                        connS.rollback()
                    finally:
                        release_db(connS)
        if data.get('header', {}).get('competitions'):
            espn_game_id = str(game_id)
        else:
            # Fallback for ids ESPN doesn't recognize: scan the scoreboard
            # across the game's UTC date and the day before (to cover the
            # UTC-midnight boundary), matching by team name.
            data = {}
            date_str = str(game_info[8])[:10].replace('-', '') if game_info[8] else None
            if date_str:
                day = datetime.datetime.strptime(date_str, '%Y%m%d')
                prev_day = (day - datetime.timedelta(days=1)).strftime('%Y%m%d')
                r = req.get(
                    'https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard',
                    params={'dates': f'{prev_day}-{date_str}', 'limit': 400},
                    timeout=4
                )
                for ev in r.json().get('events', []):
                    comp = (ev.get('competitions') or [{}])[0]
                    all_names = set()
                    for c in comp.get('competitors', []):
                        t = c.get('team', {})
                        all_names.update(n.lower() for n in [
                            t.get('displayName', ''), t.get('shortDisplayName', ''),
                            t.get('name', ''), t.get('abbreviation', '')
                        ])
                    if home_team.lower() in all_names or away_team.lower() in all_names:
                        espn_game_id = ev.get('id')
                        break
            if espn_game_id:
                s = req.get(
                    'https://site.api.espn.com/apis/site/v2/sports/football/college-football/summary',
                    params={'event': espn_game_id},
                    timeout=4
                )
                if s.ok:
                    data = s.json()

        if espn_game_id:

            # ── Quarters (use displayValue — value field is always 0 in ESPN) ──
            def _score(ls):
                dv = ls.get('displayValue')
                if dv is not None:
                    try:
                        return int(float(dv))
                    except (ValueError, TypeError):
                        pass
                try:
                    return int(float(ls.get('value') or 0))
                except (ValueError, TypeError):
                    return 0

            try:
                comp = data.get('header', {}).get('competitions', [{}])[0]
                for competitor in comp.get('competitors', []):
                    side = competitor.get('homeAway', 'home')
                    quarters[side] = [_score(ls) for ls in competitor.get('linescores', [])]
                venue = comp.get('venue') or {}
                attendance = comp.get('attendance')
                venue_name = venue.get('fullName', '')
                venue_address = venue.get('address', {})
                venue_city = venue_address.get('city', '')
                venue_state = venue_address.get('state', '')
                if venue_city and venue_state:
                    venue_location = f"{venue_city}, {venue_state}"
                elif venue_city:
                    venue_location = venue_city
                att = attendance or 0
                attendance_fmt = f"{att:,}" if att else ''
            except Exception as e:
                print(f"Quarters error: {e}")

            try:
                for broadcast in data.get('broadcasts', []):
                    for media in broadcast.get('media', []):
                        tv_broadcast = media.get('shortName', '')
                        break
                    if tv_broadcast:
                        break
            except Exception:
                pass

            # First pass: build play ID lookup for win_prob enrichment
            play_lookup = {}
            for _drv in ((data.get('drives') or {}).get('previous') or []):
                for _pl in (_drv.get('plays') or []):
                    pid = _pl.get('id')
                    if pid:
                        _start = _pl.get('start') or {}
                        play_lookup[str(pid)] = {
                            'text':       clean_play_text(_pl.get('text', '')),
                            'type':       (_pl.get('type') or {}).get('text', ''),
                            'clock':      (_pl.get('clock') or {}).get('displayValue', ''),
                            'period':     (_pl.get('period') or {}).get('number', 0),
                            'home_score': _pl.get('homeScore', 0),
                            'away_score': _pl.get('awayScore', 0),
                            'is_scoring': bool(_pl.get('scoringPlay', False)),
                            'down_dist':  (_start.get('shortDownDistanceText') or
                                           _start.get('downDistanceText', '')),
                        }

            # Win probability
            wp_raw = data.get('winprobability') or []
            total_wp = max(len(wp_raw) - 1, 1)
            for i, wp in enumerate(wp_raw):
                home_pct     = float(wp.get('homeWinPercentage', 0.5))
                secs_left    = wp.get('secondsLeft')
                secs_elapsed = wp.get('secondsElapsed') or wp.get('seconds')
                if secs_left is not None:
                    minutes_x = (3600 - float(secs_left)) / 60
                elif secs_elapsed is not None:
                    minutes_x = float(secs_elapsed) / 60
                else:
                    minutes_x = i / total_wp * 60
                play_id = str(wp.get('playId', '') or '')
                matched = play_lookup.get(play_id, {})
                win_prob.append({
                    'x':          round(minutes_x, 3),
                    'home':       round(home_pct, 4),
                    'away':       round(1 - home_pct, 4),
                    'play_id':    play_id,
                    'play_text':  matched.get('text', ''),
                    'play_type':  matched.get('type', ''),
                    'clock':      matched.get('clock', ''),
                    'period':     matched.get('period', 0),
                    'home_score': matched.get('home_score', 0),
                    'away_score': matched.get('away_score', 0),
                    'down_dist':  matched.get('down_dist', ''),
                    'is_scoring': matched.get('is_scoring', False),
                })

            # Build team-side map from competitors for play attribution
            team_side_map = {}
            try:
                comp2 = (data.get('header', {}).get('competitions') or [{}])[0]
                for c in comp2.get('competitors', []):
                    dn = c.get('team', {}).get('displayName', '')
                    if dn:
                        team_side_map[dn] = c.get('homeAway', 'home')
            except Exception:
                pass

            # Play by play + drives.
            # Running score, used to attribute each scoring play to the team
            # that ACTUALLY scored rather than the team that had possession for
            # the drive — otherwise defensive/special-teams scores (blocked-punt
            # TD, pick-six, fumble return, safety) get credited to the offense.
            prev_home_score = prev_away_score = 0
            for drive in ((data.get('drives') or {}).get('previous') or []):
                team_name = (drive.get('team') or {}).get('displayName', '')
                drive_result = drive.get('displayResult', '')
                drive_yards = drive.get('yards', 0) or 0
                drive_plays_n = drive.get('offensivePlays', 0)
                drive_summary = (f"{drive_result} · {drive_plays_n} plays, {drive_yards} yds"
                                 if drive_result else '')
                play_side = team_side_map.get(team_name, 'home')

                # Drive metadata
                start_data = drive.get('start') or {}
                start_period = start_data.get('period') or {}
                if isinstance(start_period, dict):
                    quarter = start_period.get('number', 1)
                else:
                    try: quarter = int(start_period)
                    except: quarter = 1
                start_clock_raw = start_data.get('clock') or {}
                start_clock = start_clock_raw.get('displayValue', '') if isinstance(start_clock_raw, dict) else ''
                start_yl = int(start_data.get('yardLine') or 25)
                time_el_raw = drive.get('timeElapsed') or {}
                time_el = time_el_raw.get('displayValue', '') if isinstance(time_el_raw, dict) else ''

                # displayResult is the long form ("Touchdown"/"Field Goal"),
                # so classify it the same way the header badge does; a drive is
                # "scoring" when it ends in the possessing team's own points.
                badge_label, badge_bg, badge_color = _classify_drive_result(drive_result)
                is_scoring_drive = badge_label in ('TOUCHDOWN', 'FIELD GOAL', 'SAFETY')
                yl = min(max(start_yl, 1), 99)

                for play in (drive.get('plays') or []):
                    play_type = (play.get('type') or {}).get('text', '')
                    is_scoring = bool(play.get('scoringPlay', False))
                    # Attribute the play to the team that actually scored. Every
                    # play carries the running homeScore/awayScore; the side
                    # whose score rose on a scoring play is the scorer (defense/
                    # ST included). Non-scoring plays stay with the offense.
                    try: ph = int(play.get('homeScore'))
                    except (TypeError, ValueError): ph = prev_home_score
                    try: pa = int(play.get('awayScore'))
                    except (TypeError, ValueError): pa = prev_away_score
                    scored_side = play_side
                    if is_scoring:
                        if ph - prev_home_score > pa - prev_away_score:
                            scored_side = 'home'
                        elif pa - prev_away_score > ph - prev_home_score:
                            scored_side = 'away'
                    prev_home_score, prev_away_score = ph, pa
                    plays.append({
                        'team': team_name,
                        'side': scored_side,
                        'text': clean_play_text(play.get('text', '')),
                        'type': play_type,
                        'period': (play.get('period') or {}).get('number', 0),
                        'clock': (play.get('clock') or {}).get('displayValue', ''),
                        'scoring': is_scoring,
                        'is_scoring': is_scoring,
                        'score_value': play.get('scoreValue', 0),
                        'home_score': play.get('homeScore', ''),
                        'away_score': play.get('awayScore', ''),
                        'drive_summary': drive_summary,
                    })

                # ── Drives tab: per-play stacked-bar data ───────────────────────
                # Position every play by its OWN scrimmage spot, taken straight
                # from ESPN's per-play start.yardLine (= yards from the HOME
                # team's goal), mapped onto the display scale where 0 = the away
                # team's own goal (left) and 100 = the home team's own goal
                # (right):   abs = 100 - yardLine.
                #   away (drives left→right): abs increases as yardLine falls
                #   home (drives right→left): abs decreases as yardLine rises
                # Each bar spans from this play's spot to the NEXT scrimmage
                # play's spot — its true net field movement. This replaces an
                # earlier statYardage accumulator that started from a
                # mis-mapped position and ran off the goal line after a few
                # plays, collapsing the rest of the drive into zero-width
                # slivers stacked at one yard line.
                direction = 1 if play_side == 'away' else -1
                start_yl_abs = max(0.0, min(100.0, 100.0 - yl))

                def _abs_from_yl(v):
                    try:
                        return max(0.0, min(100.0, 100.0 - int(v)))
                    except (TypeError, ValueError):
                        return None

                # Scrimmage snaps only (drop kickoff/timeout/period markers),
                # keeping each play's classification + its start yardLine.
                scrimmage = []
                for p in (drive.get('plays') or []):
                    classified = _classify_play((p.get('type') or {}).get('text', ''))
                    if classified is None:
                        continue
                    scrimmage.append((p, classified, _abs_from_yl((p.get('start') or {}).get('yardLine'))))

                down_map = {1: '1st', 2: '2nd', 3: '3rd', 4: '4th'}
                drive_play_list = []
                prev_abs = start_yl_abs
                for idx, (p, classified, a0) in enumerate(scrimmage):
                    label, color, is_turnover = classified
                    ptype = (p.get('type') or {}).get('text', '')
                    stat_yards = int(p.get('statYardage', 0) or 0)
                    if a0 is None:
                        a0 = prev_abs
                    prev_abs = a0

                    # End position = where the next scrimmage play began (the
                    # spot this play left the ball). For the last play, a
                    # rush/pass touchdown ends in the end zone; a field goal or
                    # anything else is a marker at the spot (a FG's statYardage
                    # is the kick distance, not field advancement).
                    a1 = None
                    if idx + 1 < len(scrimmage):
                        a1 = scrimmage[idx + 1][2]
                    if a1 is None:
                        if bool(p.get('scoringPlay')) and 'field goal' not in ptype.lower():
                            a1 = 100.0 if direction == 1 else 0.0
                        elif 'field goal' in ptype.lower():
                            a1 = a0
                        else:
                            a1 = max(0.0, min(100.0, a0 + direction * stat_yards))

                    start_pct = min(a0, a1)
                    end_pct = max(a0, a1)
                    net_yards = int(round((a1 - a0) * direction))

                    p_start = p.get('start') or {}
                    down, distance = p_start.get('down'), p_start.get('distance')
                    down_dist = f"{down_map.get(down, '')} & {distance}" if down and distance is not None else ''
                    play_clock = (p.get('clock') or {}).get('displayValue', '')
                    play_period = (p.get('period') or {}).get('number') or quarter
                    tooltip_bits = [b for b in [
                        down_dist,
                        f"{label} — {net_yards:+d} yds" if net_yards else f"{label} — 0 yds",
                        f"Q{play_period} · {play_clock}" if play_clock else f"Q{play_period}",
                    ] if b]

                    drive_play_list.append({
                        'label':       label,
                        'color':       color,
                        'start_pct':   round(start_pct, 2),
                        'width_pct':   round(end_pct - start_pct, 2),
                        'yards':       net_yards,
                        'is_turnover': is_turnover,
                        'is_scoring':  bool(p.get('scoringPlay', False)),
                        'tooltip':     ' | '.join(tooltip_bits),
                    })

                # Fallback for the rare drive where ESPN gives no usable
                # per-play data at all: one summary bar spanning the drive's
                # net distance, labeled with the drive result, instead of an
                # empty field.
                if not drive_play_list and drive_yards:
                    fallback_end = max(0.0, min(100.0, start_yl_abs + direction * drive_yards))
                    drive_play_list.append({
                        'label':       (drive_result or 'Drive')[:10],
                        'color':       '#6b7280',
                        'start_pct':   round(min(start_yl_abs, fallback_end), 2),
                        'width_pct':   round(abs(fallback_end - start_yl_abs), 2),
                        'yards':       drive_yards,
                        'is_turnover': False,
                        'is_scoring':  is_scoring_drive,
                        'tooltip':     f"{drive_result} · {drive_yards} yds",
                    })

                drives.append({
                    'team': team_name,
                    'is_home': play_side == 'home',
                    'result': drive_result,
                    'plays_count': drive_plays_n,
                    'yards': drive_yards,
                    'time_elapsed': time_el,
                    'quarter': quarter,
                    'start_clock': start_clock,
                    'is_scoring': is_scoring_drive,
                    'badge_label': badge_label,
                    'badge_bg': badge_bg,
                    'badge_color': badge_color,
                    'plays': drive_play_list,
                    'start_yardline': start_yl_abs,
                })

            # Drives tab shows scoring drives only (TD / FG / safety), matching
            # the Play-by-Play section that was likewise narrowed to scoring
            # plays — punts, turnovers, and end-of-half drives are dropped.
            drives = [d for d in drives if d['is_scoring']]

            # Team stats — build normalized dicts + normalize sacks
            for tb in ((data.get('boxscore') or {}).get('teams') or []):
                side = tb.get('homeAway', 'home')
                t_display = (tb.get('team') or {}).get('displayName', side)
                d = {}
                for st in (tb.get('statistics') or []):
                    d[st.get('name', '')] = st.get('displayValue', '')
                print(f"Team {t_display} stat keys: {list(d.keys())}")
                # Normalize sacks — ESPN uses several key names
                if 'sacks' not in d:
                    for k in ('Sacks', 'sacksYardsLost', 'sackYardsLost', 'defensiveSacks'):
                        if k in d:
                            raw_val = d[k]
                            d['sacks'] = raw_val.split('-')[0].strip() if '-' in raw_val else raw_val
                            break
                team_stats.append({'side': side, 'stats': [{'name': k, 'value': v} for k, v in d.items()]})
                if side == 'home':
                    home_stats = d
                else:
                    away_stats = d

            # Player stats + build athlete/team lookup for $ref resolution
            boxscore_players = (data.get('boxscore') or {}).get('players') or []
            athlete_lookup = {}   # athlete_id -> {name, headshot, team}
            team_id_lookup = {}   # team_id    -> display_name

            for pb in boxscore_players:
                t_obj  = pb.get('team', {}) or {}
                t_id   = str(t_obj.get('id', ''))
                t_name = t_obj.get('displayName', '')
                tn_l   = t_name.lower()
                at_l   = away_team.lower()
                if tn_l and (tn_l == at_l or at_l in tn_l or tn_l in at_l
                             or any(w in tn_l for w in at_l.split() if len(w) >= 2)):
                    side = 'away'
                else:
                    side = 'home'
                if t_id and t_name:
                    team_id_lookup[t_id] = t_name
                cats = []
                for cat in (pb.get('statistics') or []):
                    labels = cat.get('labels', [])
                    athletes = []
                    for ae in (cat.get('athletes') or []):
                        ad = ae.get('athlete', {})
                        hs = ad.get('headshot')
                        aid = str(ad.get('id', ''))
                        if aid and aid not in athlete_lookup:
                            athlete_lookup[aid] = {
                                'name':     ad.get('displayName', ''),
                                'headshot': hs.get('href', '') if isinstance(hs, dict) else (hs or ''),
                                'team':     t_name,
                            }
                        ath_name = ad.get('displayName', '')
                        athletes.append({
                            'name':      ath_name,
                            'headshot':  hs.get('href', '') if isinstance(hs, dict) else (hs or ''),
                            'stats':     dict(zip(labels, ae.get('stats', []))),
                            'player_id': name_to_player_id.get(ath_name.lower()),
                        })
                    cats.append({'name': cat.get('name', ''), 'labels': labels, 'athletes': athletes})
                player_stats.append({'side': side, 'categories': cats})

            # Build structured box_score
            for team_data in player_stats:
                p_side = team_data['side']
                for cat in team_data['categories']:
                    box_score[p_side][cat['name'].lower()] = {
                        'labels': cat['labels'],
                        'athletes': cat['athletes'],
                    }
            print("\n=== BOX SCORE DEBUG ===")
            for p_side in ['away', 'home']:
                team_lbl = away_team if p_side == 'away' else home_team
                print(f"\n{team_lbl} ({p_side}):")
                for cname, cdata in box_score[p_side].items():
                    print(f"  {cname}: labels={cdata['labels']}")
                    if cdata['athletes']:
                        print(f"    first: {cdata['athletes'][0]['name']} stats={cdata['athletes'][0].get('stats', {})}")

            def _id_from_ref(obj, pattern):
                m = re.search(pattern, obj.get('$ref', ''))
                return m.group(1) if m else str(obj.get('id', ''))

            # Leaders — keyed by ESPN's API name (e.g. 'passingYards')
            # Resolve $ref athlete/team objects via boxscore lookup
            for lg in (data.get('leaders') or []):
                api_key = lg.get('name', '')   # e.g. 'passingYards'
                if not api_key:
                    continue   # entire entry is a $ref — skip
                cat_leaders = []
                for leader in (lg.get('leaders') or []):
                    ath = leader.get('athlete') or {}
                    ath_name = ath.get('displayName', '') or ath.get('shortName', '')
                    if not ath_name:
                        aid  = _id_from_ref(ath, r'/athletes/(\d+)')
                        info = athlete_lookup.get(aid, {})
                        ath_name = info.get('name', '')
                        ath_hs   = info.get('headshot', '')
                    else:
                        hs     = ath.get('headshot')
                        ath_hs = hs.get('href', '') if isinstance(hs, dict) else (hs or '')
                    team_obj  = leader.get('team') or {}
                    team_name = team_obj.get('displayName', '')
                    if not team_name:
                        tid       = _id_from_ref(team_obj, r'/teams/(\d+)')
                        team_name = team_id_lookup.get(tid, '')
                    is_home = team_side_map.get(team_name, '') == 'home'
                    ath_obj = leader.get('athlete') or {}
                    cat_leaders.append({
                        'name':       ath_name,
                        'headshot':   ath_hs,
                        'team':       team_name,
                        'stat':       leader.get('displayValue', ''),
                        'is_home':    is_home,
                        'position':   (ath_obj.get('position') or {}).get('abbreviation', ''),
                        'stat_detail': '',
                    })
                leaders[api_key] = cat_leaders

            # Fallback: derive leaders from boxscore.players when ESPN leaders are empty/unusable
            if not any(p['name'] for plist in leaders.values() for p in plist):
                leaders.clear()
                cat_cfg = [
                    ('passingYards',   'passing',   ['C/ATT','YDS','TD']),
                    ('rushingYards',   'rushing',   ['CAR','YDS','TD']),
                    ('receivingYards', 'receiving', ['REC','YDS','TD']),
                ]
                for pb in boxscore_players:
                    side   = pb.get('homeAway', 'home')
                    t_name = (pb.get('team') or {}).get('displayName', '')
                    for cat in (pb.get('statistics') or []):
                        cn = cat.get('name', '').lower()
                        for api_key, keyword, want_cols in cat_cfg:
                            if keyword not in cn:
                                continue
                            labels  = cat.get('labels', [])
                            ul      = [l.upper() for l in labels]
                            try:
                                yds_idx = ul.index('YDS')
                            except ValueError:
                                continue
                            best_yds, best_entry = -1, None
                            for ae in (cat.get('athletes') or []):
                                stats = ae.get('stats', [])
                                try:
                                    yds = int(float(stats[yds_idx])) if yds_idx < len(stats) else 0
                                except (ValueError, TypeError):
                                    yds = 0
                                if yds > best_yds:
                                    best_yds = yds
                                    ad = ae.get('athlete', {})
                                    hs = ad.get('headshot')
                                    dv_parts = []
                                    for w in want_cols:
                                        try:
                                            dv_parts.append(str(stats[ul.index(w)]))
                                        except (ValueError, IndexError):
                                            pass
                                    best_entry = {
                                        'name':       ad.get('displayName', ''),
                                        'headshot':   hs.get('href', '') if isinstance(hs, dict) else (hs or ''),
                                        'team':       t_name,
                                        'stat':       ' · '.join(dv_parts) or f"{yds} YDS",
                                        'is_home':    side == 'home',
                                        'position':   (ad.get('position') or {}).get('abbreviation', ''),
                                        'stat_detail': '',
                                    }
                            if best_entry:
                                leaders.setdefault(api_key, []).append(best_entry)

    except Exception as e:
        print(f"ESPN fetch error: {e}")
        import traceback; traceback.print_exc()

    structured_leaders = {}
    leader_cats = [
        ('passingYards',   'Passing'),
        ('rushingYards',   'Rushing'),
        ('receivingYards', 'Receiving'),
    ]
    def _matches_team(espn_full, db_short):
        a, b = espn_full.lower(), db_short.lower()
        # exact, substring, or any significant word overlap
        return (a == b or b in a or a in b
                or any(w in a for w in b.split() if len(w) >= 2))

    for api_key, display_name in leader_cats:
        if api_key in leaders:
            players = leaders[api_key]
            home_leader = None
            away_leader = None
            for p in players:
                espn_name = p.get('team', '')
                if _matches_team(espn_name, home_team):
                    home_leader = p
                elif _matches_team(espn_name, away_team):
                    away_leader = p
            structured_leaders[api_key] = {
                'label': display_name,
                'home':  home_leader,
                'away':  away_leader,
            }

    # Enrich game leaders with player IDs for linking
    for cat_data in structured_leaders.values():
        for side in ('home', 'away'):
            ldr = cat_data.get(side)
            if ldr:
                ldr['player_id'] = name_to_player_id.get((ldr.get('name') or '').lower())

    # Top plays by win probability added — derived from the same WP series
    # that feeds the chart. Each sample carries the post-play win %, so the
    # swing a play produced is the change from the previous sample. No extra
    # data source: if win_prob is empty/unmatched, top_wpa is simply empty.
    top_wpa = []
    for i in range(1, len(win_prob)):
        cur = win_prob[i]
        if not cur.get('play_text'):
            continue
        delta = cur['home'] - win_prob[i - 1]['home']   # change in home win %
        top_wpa.append({
            'swing_pct':  round(abs(delta) * 100),
            'toward':     'home' if delta > 0 else 'away',
            'play_text':  cur.get('play_text', ''),
            'clock':      cur.get('clock', ''),
            'period':     cur.get('period', 0),
            'home_score': cur.get('home_score', 0),
            'away_score': cur.get('away_score', 0),
        })
    top_wpa.sort(key=lambda p: p['swing_pct'], reverse=True)
    top_wpa = [p for p in top_wpa if p['swing_pct'] >= 1][:5]

    # Date + season type formatting
    # start_date is stored in UTC; convert to Eastern before formatting the
    # date and kickoff time. Previously the tz was stripped and the raw UTC
    # time was labeled "ET", so e.g. a 00:30 UTC kickoff showed "12:30 AM ET"
    # (and on the wrong calendar day) instead of the correct 7:30 PM ET.
    start_date_raw = str(game_info[8]) if game_info[8] else ''
    try:
        dt = datetime.datetime.fromisoformat(start_date_raw.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        et = dt.astimezone(ZoneInfo('America/New_York'))
        game_date = et.strftime('%A, %B %-d, %Y')
        game_time = et.strftime('%-I:%M %p ET')
    except Exception:
        game_date = start_date_raw[:10] if start_date_raw else 'TBD'
        game_time = ''

    season_type_raw = game_info[6] or ''
    season_type_display = 'Postseason' if 'POST' in str(season_type_raw).upper() else 'Regular Season'
    week_num = game_info[5]
    notes = game_info[7] or ''

    # Frozen pre-kickoff Savant Forecast for this completed game (drives both
    # the recap line and, when it was a miss, the upset badge). None when the
    # game predates Forecast or was never predicted.
    conn_u = get_db()
    try:
        forecast = get_frozen_forecasts(conn_u.cursor(), [game_id]).get(game_id)
    finally:
        release_db(conn_u)

    return render_template('game.html',
        game=game_info,
        forecast=forecast,
        is_scheduled=False,
        home_team=home_team,
        away_team=away_team,
        home_is_fbs=home_is_fbs,
        away_is_fbs=away_is_fbs,
        ap_rankings=ap_rankings,
        espn_game_id=espn_game_id,
        quarters=quarters,
        venue=venue,
        attendance=attendance,
        venue_name=venue_name,
        venue_location=venue_location,
        attendance_fmt=attendance_fmt,
        tv_broadcast=tv_broadcast,
        plays=plays,
        team_stats=team_stats,
        home_stats=home_stats,
        away_stats=away_stats,
        player_stats=player_stats,
        leaders=leaders,
        drives=drives,
        box_score=box_score,
        structured_leaders=structured_leaders,
        win_prob=win_prob,
        top_wpa=top_wpa,
        records=records,
        game_date=game_date,
        game_time=game_time,
        season_type_display=season_type_display,
        week_num=week_num,
        notes=notes,
        rivalry_name=rivalry_name,
        game_season=game_season,
    )


# Conference championship games are tagged 'SeasonType.REGULAR' in this
# dataset (they land on the last "week" of the regular season, not
# 'SeasonType.POSTSEASON'), so they're matched on notes text regardless of
# season_type rather than being folded into the postseason-only branch.
_CCG_RE = re.compile(
    r'^(SEC|Big Ten|Big 12|ACC|Pac-12|Mountain West|American|Sun Belt|MAC|Conference USA)'
    r'\s+Championship(?:\s+Game)?$'
)
_CCG_ABBR = {'Conference USA': 'CUSA', 'Mountain West': 'MW', 'American': 'AAC'}


def shorten_game_label(season_type, week, notes):
    """Compact game-log label. Regular-season games keep the bare week
    number (matching the pre-existing display) so this only changes rows
    that were actually showing a wrong/misleading week number before."""
    notes = (notes or '').strip()

    m = _CCG_RE.match(notes)
    if m:
        conf = m.group(1)
        return f"{_CCG_ABBR.get(conf, conf)} CCG"

    if season_type != 'SeasonType.POSTSEASON':
        return str(week)

    if not notes:
        return 'Postseason'

    label = notes
    # This dataset phrases CFP rounds as "... at the <Bowl Name>" rather
    # than with a " - " separator, so match on the round name itself and
    # drop the specific bowl/sponsor name — the round is what matters here.
    if 'College Football Playoff Semifinal' in label:
        label = 'CFP Semifinal'
    elif 'College Football Playoff Quarterfinal' in label:
        label = 'CFP Quarterfinal'
    elif 'College Football Playoff First Round' in label:
        label = 'CFP R1'
    elif 'College Football Playoff National Championship' in label:
        label = 'CFP Championship'
    elif 'College Football Playoff' in label:
        label = 'CFP'

    if len(label) > 28:
        label = label[:26] + '…'

    return label


class _SkipGameLog(Exception):
    """Control-flow: game log already loaded from the Postgres store."""

@app.route('/player/<int:player_id>')
def player_detail(player_id):
    # No explicit ?season= defaults to the player's most recent recorded
    # season — a departed player (e.g. 2016-19 career) lands on his final
    # year instead of an empty current-season page.
    s = request.args.get('season', type=int)
    if s and s in get_available_seasons():
        return _player_detail_cached(player_id, s)
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT MAX(season) FROM player_stats WHERE player_id = %s', (str(player_id),))
        row = cur.fetchone()
    finally:
        release_db(conn)
    return _player_detail_cached(player_id, row[0] if row and row[0] else CURRENT_SEASON)

@cache.memoize(timeout=21600)  # memoize keys on (player_id, season)
def _player_detail_cached(player_id, season):
    conn = get_db()
    try:
        cursor = conn.cursor()

        cursor.execute('''
            SELECT p.id, p.first_name, p.last_name, p.team, p.position, p.jersey,
                   p.headshot, p.height, p.weight, p.year,
                   t.logo_dark, t.color, t.alt_color, t.conference,
                   p.active_2026, p.draft_status,
                   p.nfl_status, p.nfl_team, p.nfl_team_logo,
                   p.draft_year, p.draft_round, p.draft_pick,
                   COALESCE(p.redshirt, 0)
            FROM players p
            LEFT JOIN teams t ON p.team = t.name
            WHERE p.id = %s
        ''', (player_id,))
        row = cursor.fetchone()
        if not row:
            return render_template('404.html', message='Player not found.'), 404

        # The players row is only the CURRENT snapshot — a departed player
        # keeps his first-seen team there (e.g. Burrow's identity row says
        # Ohio State). For a historical view, the hero must show the team,
        # jersey, and measurables AS OF the viewed season, resolved from the
        # rosters table (player_stats' per-season team as fallback). The
        # current-season view keeps players.team so present-day transfer
        # moves still display the player's new team.
        if season != CURRENT_SEASON:
            cursor.execute('''
                SELECT r.team, r.position, r.jersey, r.height, r.weight, r.class_year,
                       t.logo_dark, t.color, t.alt_color, t.conference
                FROM rosters r LEFT JOIN teams t ON t.name = r.team
                WHERE r.player_id = %s AND r.season = %s
            ''', (player_id, season))
            season_row = cursor.fetchone()
            if not season_row:
                # No roster row that year (pre-2019 gaps) — fall back to the
                # team the season's stats were recorded under, keeping the
                # identity row's position/jersey/measurables.
                cursor.execute('''
                    SELECT ps.team, NULL, NULL, NULL, NULL, NULL,
                           t.logo_dark, t.color, t.alt_color, t.conference
                    FROM player_stats ps LEFT JOIN teams t ON t.name = ps.team
                    WHERE ps.player_id = %s AND ps.season = %s AND ps.team IS NOT NULL
                    LIMIT 1
                ''', (str(player_id), season))
                season_row = cursor.fetchone()
            if season_row and season_row[0]:
                row = list(row)
                row[3] = season_row[0]                              # team
                if season_row[1]: row[4] = season_row[1]            # position
                if season_row[2] is not None: row[5] = season_row[2]  # jersey
                if season_row[3] is not None: row[7] = season_row[3]  # height
                if season_row[4] is not None: row[8] = season_row[4]  # weight
                if season_row[5]: row[9] = season_row[5]            # class year
                row[10] = season_row[6]                             # logo_dark
                row[11] = season_row[7]                             # color
                row[12] = season_row[8]                             # alt_color
                row[13] = season_row[9]                             # conference

        is_active_2026 = row[14] if row[14] is not None else 1
        draft_status   = row[15]

        c1 = row[11] or '#1a2a4a'
        c2 = row[12] or '#0a1220'
        h = int(row[7]) if row[7] else None
        year_raw = str(row[9]).strip() if row[9] is not None else ''
        year_map = {
            '1': 'Freshman',  '2': 'Sophomore', '3': 'Junior', '4': 'Senior', '5': 'Graduate',
            'Fr': 'Freshman', 'So': 'Sophomore', 'Jr': 'Junior', 'Sr': 'Senior', 'Gr': 'Graduate',
        }
        # Redshirt is a career trait — prefix the class label when set.
        is_redshirt = bool(row[22])
        _year_base = year_map.get(year_raw, '')
        year_display = f'Redshirt {_year_base}' if (is_redshirt and _year_base) else _year_base
        player = {
            'id':         row[0],
            'first_name': row[1],
            'last_name':  row[2],
            'name':       f"{row[1]} {row[2]}",
            'team':       row[3],
            'position':   row[4],
            'jersey':     row[5],
            'headshot':   row[6],
            'height':     row[7],
            'weight':     row[8],
            'year':       row[9],
            'logo_dark':  row[10],
            'conference': row[13],
            'height_fmt': f"{h // 12}'{h % 12}\"" if h else '',
            'year_fmt':   year_display,
            'redshirt':   is_redshirt,
            'nfl_status':    row[16],
            'nfl_team':      row[17],
            'nfl_team_logo': row[18],
            'draft_year':    row[19],
            'draft_round':   row[20],
            'draft_pick':    row[21],
        }

        # ── Transfer history + stats/current-team mismatch detection ──────────
        # player_id resolved at ingest; name match kept for unresolved rows.
        cursor.execute('''
            SELECT origin, destination, transfer_date, year, stars, rating
            FROM transfers
            WHERE player_id = %s
               OR (player_id IS NULL AND LOWER(first_name) = LOWER(%s)
                                     AND LOWER(last_name) = LOWER(%s))
            ORDER BY year ASC, transfer_date ASC
        ''', (player_id, player['first_name'], player['last_name']))
        transfers_history = []
        for origin, destination, transfer_date, t_year, stars, rating in cursor.fetchall():
            if not origin or not destination:
                continue  # still in the portal / no completed move to show
            transfers_history.append({
                'origin': origin,
                'destination': destination,
                'year': t_year,
                'stars': stars or 0,
            })

        # ── Career hardware (Heisman, Biletnikoff, …) for the hero band ──────
        cursor.execute('''
            SELECT award, season FROM awards
            WHERE player_id = %s AND kind = 'player'
            ORDER BY season, award
        ''', (player_id,))
        player_awards = [{'award': a, 'season': s} for a, s in cursor.fetchall()]

        # Every team this player has recorded player_stats under — may differ
        # from players.team (their current/latest team) if they transferred.
        cursor.execute('''
            SELECT DISTINCT team FROM player_stats
            WHERE (player_id = %s OR player_name = %s) AND team IS NOT NULL
        ''', (str(player_id), player['name']))
        all_stat_teams = [r[0] for r in cursor.fetchall()]
        previous_stat_teams = [t for t in all_stat_teams if t != player['team']]

        # One batched lookup (not one query per team) covering every team name
        # that could show up in the transfer badge or the game log Team column.
        teams_needed = set(all_stat_teams)
        for t in transfers_history:
            teams_needed.add(t['origin'])
            teams_needed.add(t['destination'])
        transfer_team_logos = {}
        team_abbrevs = {}
        if teams_needed:
            cursor.execute(
                'SELECT name, logo_dark, abbreviation FROM teams WHERE name = ANY(%s)',
                (list(teams_needed),)
            )
            for t_name, logo_dark, abbr in cursor.fetchall():
                transfer_team_logos[t_name] = logo_dark
                team_abbrevs[t_name] = abbr

        cursor.execute('SELECT category, stat_type, stat FROM player_stats WHERE player_id = %s AND season = %s',
                       (str(player_id), season))
        stats = {}
        for cat, st, val in cursor.fetchall():
            if cat not in stats: stats[cat] = {}
            stats[cat][st] = val

        # Normalize season stats — clean types for display
        def _i(v):
            try: return int(round(float(v))) if v is not None else None
            except: return v
        def _f(v, d=1):
            try: return round(float(v), d) if v is not None else None
            except: return v

        if 'passing' in stats:
            p = stats['passing']
            pct = p.get('PCT')
            if pct is not None:
                pct_f = float(pct)
                p['PCT'] = f"{pct_f * 100:.1f}%" if pct_f <= 1.0 else f"{pct_f:.1f}%"
            p['YDS'] = _i(p.get('YDS')); p['TD'] = _i(p.get('TD'))
            p['INT'] = _i(p.get('INT')); p['ATT'] = _i(p.get('ATT'))
            p['COMPLETIONS'] = _i(p.get('COMPLETIONS'))
            p['YPA'] = _f(p.get('YPA'))
        if 'rushing' in stats:
            r = stats['rushing']
            r['YDS'] = _i(r.get('YDS')); r['TD'] = _i(r.get('TD'))
            r['ATT'] = _i(r.get('ATT')); r['LONG'] = _i(r.get('LONG'))
            r['YPC'] = _f(r.get('YPC'))
        if 'receiving' in stats:
            rc = stats['receiving']
            rc['YDS'] = _i(rc.get('YDS')); rc['TD'] = _i(rc.get('TD'))
            rc['REC'] = _i(rc.get('REC')); rc['LONG'] = _i(rc.get('LONG'))
            rc['AVG'] = _f(rc.get('AVG'))
        if 'defensive' in stats:
            d = stats['defensive']
            d['TOT'] = _i(d.get('TOT')); d['SOLO'] = _i(d.get('SOLO'))
            d['PD'] = _i(d.get('PD'))
            d['TFL'] = _f(d.get('TFL')); d['SACKS'] = _f(d.get('SACKS'))
        if 'kicking' in stats:
            k = stats['kicking']
            pct = k.get('PCT')
            if pct is not None:
                pct_f = float(pct)
                k['PCT'] = f"{pct_f * 100:.1f}%" if pct_f <= 1.0 else f"{pct_f:.1f}%"
            k['FGM'] = _i(k.get('FGM')); k['FGA'] = _i(k.get('FGA'))
            k['LONG'] = _i(k.get('LONG'))
        if 'punting' in stats:
            pt = stats['punting']
            pt['NO'] = _i(pt.get('NO')); pt['YDS'] = _i(pt.get('YDS'))
            pt['LONG'] = _i(pt.get('LONG')); pt['AVG'] = _f(pt.get('AVG'))

        cursor.execute('''
            SELECT avg_ppa_all, avg_ppa_pass, avg_ppa_rush, total_ppa
            FROM player_ppa WHERE player_id = %s AND season = %s
        ''', (str(player_id), season))
        ppa_row = cursor.fetchone()
        ppa = None
        if ppa_row:
            ppa = {
                'avg_all':  round(ppa_row[0], 3) if ppa_row[0] is not None else None,
                'avg_pass': round(ppa_row[1], 3) if ppa_row[1] is not None else None,
                'avg_rush': round(ppa_row[2], 3) if ppa_row[2] is not None else None,
                'total':    round(ppa_row[3], 1)  if ppa_row[3] is not None else None,
            }

        cursor.execute('SELECT rank FROM ap_rankings WHERE team=%s AND season=%s ORDER BY week DESC LIMIT 1',
                       (player['team'], season))
        ap_row = cursor.fetchone()
        ap_rank = ap_row[0] if ap_row else None

        # ── NATIONAL RANKS + PERCENTILES ─────────────────────────────────────────
        # One place builds both the season-stat national ranks and the ordered,
        # advanced-first percentile bars for every position group (see
        # _build_percentiles / PERCENTILE_METRICS).
        national_ranks     = {}
        player_percentiles = {}
        percentile_rows    = []
        try:
            national_ranks, percentile_rows, _pgroup, _peer = _build_percentiles(
                cursor, player_id, player.get('position') or '', season)
            if _pgroup:
                player_percentiles = {'group': _pgroup, 'peer_count': _peer}
        except Exception as e:
            print(f"Rank/percentile error: {e}")
            import traceback; traceback.print_exc()
            national_ranks, player_percentiles, percentile_rows = {}, {}, []


        # Player usage
        cursor.execute('''
            SELECT overall, pass, rush, first_down, second_down, third_down,
                   standard_downs, passing_downs
            FROM player_usage WHERE player_id=%s AND season=%s
        ''', (player_id, season))
        usage_row = cursor.fetchone()
        usage = None
        if usage_row:
            def _pct(v):
                return round(v * 100, 1) if v is not None else None
            usage = {
                'overall':       _pct(usage_row[0]),
                'pass':          _pct(usage_row[1]),
                'rush':          _pct(usage_row[2]),
                'first_down':    _pct(usage_row[3]),
                'second_down':   _pct(usage_row[4]),
                'third_down':    _pct(usage_row[5]),
                'standard':      _pct(usage_row[6]),
                'passing_downs': _pct(usage_row[7]),
            }

    finally:
        release_db(conn)

    game_log = []
    # Persisted game logs: the ESPN scan below is the single most expensive
    # part of a cold player page (~a dozen HTTP calls). Completed seasons are
    # immutable, so the result is stored in Postgres and reused forever —
    # surviving restarts, unlike the in-memory page cache.
    _log_cached = None
    connG = get_db()
    try:
        curG = connG.cursor()
        curG.execute('''
            CREATE TABLE IF NOT EXISTS player_game_logs (
                player_id INTEGER NOT NULL,
                season    INTEGER NOT NULL,
                log       TEXT,
                updated_at TIMESTAMPTZ DEFAULT now(),
                PRIMARY KEY (player_id, season)
            )
        ''')
        connG.commit()
        curG.execute('SELECT log FROM player_game_logs WHERE player_id=%s AND season=%s',
                     (player_id, season))
        rowG = curG.fetchone()
        if rowG and rowG[0]:
            _log_cached = json.loads(rowG[0])
    except Exception:
        connG.rollback()
    finally:
        release_db(connG)
    if _log_cached is not None:
        game_log = _log_cached

    try:
        if _log_cached is not None:
            raise _SkipGameLog()   # already loaded from Postgres
        # The live ESPN scan (~13 blocking HTTP calls) is the only long
        # socket-wait in the app — under crawler traffic it can occupy every
        # worker thread and stall the whole site. Historical seasons are
        # fully covered by backfill_game_logs.py (empty rows included), so
        # live scanning is reserved for the current season, and only for
        # players who actually recorded stats that season.
        if season != CURRENT_SEASON or not stats:
            raise _SkipGameLog()
        # Get completed games for this team from DB (includes opponent/result info)
        conn2 = get_db()
        try:
            cur2 = conn2.cursor()
            # A player may have transferred — player_stats.team reflects whatever
            # team they actually recorded stats for, which can differ from
            # players.team (their current/latest team). Pull every team on
            # record so the game log isn't filtered down to just the current
            # school and missing games played elsewhere.
            cur2.execute(
                "SELECT DISTINCT team FROM player_stats WHERE player_id = %s AND team IS NOT NULL",
                (str(player_id),)
            )
            player_teams = [row[0] for row in cur2.fetchall() if row[0]]
            if not player_teams:
                player_teams = [player['team']]

            cur2.execute('''
                SELECT g.id, g.week,
                       CASE WHEN g.home_team = ANY(%s) THEN g.away_team ELSE g.home_team END,
                       CASE WHEN g.home_team = ANY(%s) THEN g.home_points ELSE g.away_points END,
                       CASE WHEN g.home_team = ANY(%s) THEN g.away_points ELSE g.home_points END,
                       CASE WHEN g.home_team = ANY(%s) THEN 'home' ELSE 'away' END,
                       CASE WHEN g.home_team = ANY(%s) THEN t2.logo_dark ELSE t1.logo_dark END,
                       CASE WHEN g.home_team = ANY(%s) THEN g.home_team ELSE g.away_team END,
                       g.season_type, g.notes
                FROM games g
                LEFT JOIN teams t1 ON g.home_team = t1.name
                LEFT JOIN teams t2 ON g.away_team = t2.name
                WHERE (g.home_team = ANY(%s) OR g.away_team = ANY(%s)) AND g.completed=1 AND g.season=%s
                ORDER BY g.start_date ASC
            ''', (player_teams,) * 8 + (season,))
            games_list = cur2.fetchall()
        finally:
            release_db(conn2)

        # Find ESPN team ID (one per school — a transfer means two different
        # ESPN team IDs across the season) + athlete ID by scanning boxscores
        search_name = f"{player['first_name']} {player['last_name']}"
        remaining_teams = set(player_teams)
        espn_team_id_by_team = {}
        espn_athlete_id = None

        for game_row in games_list:
            if not remaining_teams and espn_athlete_id:
                break
            game_id, _, _, _, _, _, _, my_team, _, _ = game_row
            if my_team not in remaining_teams and espn_athlete_id:
                continue
            try:
                r = req.get(
                    'https://site.api.espn.com/apis/site/v2/sports/football/college-football/summary',
                    params={'event': game_id}, timeout=6
                )
                data = r.json()
                for bp in (data.get('boxscore') or {}).get('players', []):
                    t_obj = bp.get('team', {}) or {}
                    t_name = t_obj.get('displayName', '')
                    tn_l, at_l = t_name.lower(), my_team.lower()
                    if not (tn_l == at_l or at_l in tn_l or tn_l in at_l
                            or any(w in tn_l for w in at_l.split() if len(w) >= 4)):
                        continue
                    espn_team_id_by_team[my_team] = str(t_obj.get('id', ''))
                    remaining_teams.discard(my_team)
                    if not espn_athlete_id:
                        for stat_cat in (bp.get('statistics') or []):
                            for ae in (stat_cat.get('athletes') or []):
                                ath = ae.get('athlete', {}) or {}
                                if ath.get('displayName', '').lower() == search_name.lower():
                                    espn_athlete_id = str(ath.get('id', ''))
                                    break
                            if espn_athlete_id:
                                break
                    break
            except Exception:
                pass

        if espn_athlete_id and espn_team_id_by_team:
            # One ESPN request per game — fetched in parallel. Sequentially
            # this was ~a dozen round trips and the single slowest part of a
            # cold player-page load.
            def _fetch_game_stats(game_row):
                game_id, week, opp, my_pts, opp_pts, ha, opp_logo, my_team, season_type, notes = game_row
                team_id = espn_team_id_by_team.get(my_team)
                if not team_id:
                    return None
                try:
                    r = req.get(
                        f'https://sports.core.api.espn.com/v2/sports/football/leagues/'
                        f'college-football/events/{game_id}/competitions/{game_id}'
                        f'/competitors/{team_id}/roster/{espn_athlete_id}/statistics/0',
                        timeout=5
                    )
                    if r.status_code != 200:
                        return None
                    gdata = r.json()
                    gstats = {}
                    for cat in gdata.get('splits', {}).get('categories', []):
                        for s in cat.get('stats', []):
                            gstats[s['name']] = s.get('value')

                    if my_pts is not None and opp_pts is not None:
                        if my_pts > opp_pts:   result = f"W {int(my_pts)}-{int(opp_pts)}"
                        elif my_pts < opp_pts: result = f"L {int(opp_pts)}-{int(my_pts)}"
                        else:                  result = f"T {int(my_pts)}-{int(opp_pts)}"
                    else:
                        result = ''

                    return {
                        'week':         week,
                        'game_id':      game_id,
                        'opponent':     opp or '',
                        'opp_logo':     opp_logo or '',
                        'home_away':    ha,
                        'result':       result,
                        'team':         my_team,
                        'season_type':  season_type,
                        'game_label':   shorten_game_label(season_type, week, notes),
                        'stats':        gstats,
                    }
                except Exception:
                    return None

            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=8) as ex:
                for entry in ex.map(_fetch_game_stats, games_list):
                    if entry:
                        game_log.append(entry)

        # Store for next time — completed seasons never change. An empty log
        # is stored too (ESPN has nothing for this player/season), so we don't
        # re-scan on every view.
        connG = get_db()
        try:
            curG = connG.cursor()
            curG.execute('''
                INSERT INTO player_game_logs (player_id, season, log)
                VALUES (%s, %s, %s)
                ON CONFLICT (player_id, season) DO UPDATE SET
                    log = EXCLUDED.log, updated_at = now()
            ''', (player_id, season, json.dumps(game_log)))
            connG.commit()
        except Exception:
            connG.rollback()
        finally:
            release_db(connG)

    except _SkipGameLog:
        pass
    except Exception as e:
        print(f"ESPN game log error: {e}")
        import traceback; traceback.print_exc()

    # ── Career log + season selector ─────────────────────────────────────
    # One row per season the player recorded stats, newest first, with the
    # team attributed from the rosters table (per-season truth) rather than
    # players.team (current team only) — same principle as the transfer badge
    # and game-log Team column. player_stats' own per-season team is the
    # fallback for seasons with no roster row (e.g. pre-2019 partial rosters).
    conn2b = get_db()
    try:
        cursor2 = conn2b.cursor()
        cursor2.execute('''
            SELECT season, category, stat_type, CAST(stat AS REAL), team
            FROM player_stats WHERE player_id = %s AND season IS NOT NULL
        ''', (str(player_id),))
        career_by_season = {}
        stat_team_by_season = {}
        for ssn, cat, st, val, tm in cursor2.fetchall():
            career_by_season.setdefault(ssn, {}).setdefault(cat, {})[st] = val
            if tm and ssn not in stat_team_by_season:
                stat_team_by_season[ssn] = tm
        player_seasons = sorted(career_by_season, reverse=True) or [season]

        cursor2.execute('SELECT season, team FROM rosters WHERE player_id = %s', (player_id,))
        roster_team_by_season = dict(cursor2.fetchall())
        cursor2.execute('SELECT season, avg_ppa_all FROM player_ppa WHERE player_id = %s', (str(player_id),))
        ppa_by_season = dict(cursor2.fetchall())

        career_teams = {roster_team_by_season.get(s) or stat_team_by_season.get(s)
                        for s in career_by_season} - {None}
        team_meta = {}
        if career_teams:
            cursor2.execute('SELECT name, logo_dark, slug, conference FROM teams WHERE name = ANY(%s)',
                            (list(career_teams),))
            team_meta = {r[0]: {'logo': r[1], 'slug': r[2], 'conf': r[3]} for r in cursor2.fetchall()}
    finally:
        release_db(conn2b)
    if season not in player_seasons:
        player_seasons = sorted(set(player_seasons) | {season}, reverse=True)

    # Significant stats per position group — mirrors the leaderboards'
    # standard column sets rather than inventing a new selection.
    pos_group = POS_GROUP_MAP.get((player.get('position') or '').upper(), 'WR')
    CAREER_COLS = {
        'QB': [('PCT', 'Completion %', 'passing', 'PCT', 'pct'),
               ('YDS', 'Passing yards', 'passing', 'YDS', 'int'),
               ('TD', 'Passing touchdowns', 'passing', 'TD', 'int'),
               ('INT', 'Interceptions thrown', 'passing', 'INT', 'int'),
               ('YPA', 'Yards per attempt', 'passing', 'YPA', 'dec'),
               ('RUSH YDS', 'Rushing yards', 'rushing', 'YDS', 'int'),
               ('RUSH TD', 'Rushing touchdowns', 'rushing', 'TD', 'int'),
               ('EPA', 'EPA per play', None, None, 'epa')],
        'RB': [('CAR', 'Carries', 'rushing', 'CAR', 'int'),
               ('YDS', 'Rushing yards', 'rushing', 'YDS', 'int'),
               ('YPC', 'Yards per carry', 'rushing', 'YPC', 'dec'),
               ('TD', 'Rushing touchdowns', 'rushing', 'TD', 'int'),
               ('REC', 'Receptions', 'receiving', 'REC', 'int'),
               ('REC YDS', 'Receiving yards', 'receiving', 'YDS', 'int'),
               ('EPA', 'EPA per play', None, None, 'epa')],
        'WR': [('REC', 'Receptions', 'receiving', 'REC', 'int'),
               ('YDS', 'Receiving yards', 'receiving', 'YDS', 'int'),
               ('YPR', 'Yards per reception', 'receiving', 'YPR', 'dec'),
               ('TD', 'Receiving touchdowns', 'receiving', 'TD', 'int'),
               ('LONG', 'Longest reception', 'receiving', 'LONG', 'int'),
               ('EPA', 'EPA per play', None, None, 'epa')],
        'DL': [('TOT', 'Total tackles', 'defensive', 'TOT', 'int'),
               ('SOLO', 'Solo tackles', 'defensive', 'SOLO', 'int'),
               ('TFL', 'Tackles for loss', 'defensive', 'TFL', 'dec'),
               ('SACKS', 'Sacks', 'defensive', 'SACKS', 'dec'),
               ('PD', 'Passes defended', 'defensive', 'PD', 'int'),
               ('INT', 'Interceptions', 'interceptions', 'INT', 'int')],
        'K':  [('FGM', 'Field goals made', 'kicking', 'FGM', 'int'),
               ('FGA', 'Field goals attempted', 'kicking', 'FGA', 'int'),
               ('PCT', 'Field goal %', 'kicking', 'PCT', 'pct'),
               ('LONG', 'Longest field goal', 'kicking', 'LONG', 'int'),
               ('PTS', 'Kicking points', 'kicking', 'PTS', 'int')],
        'P':  [('NO', 'Punts', 'punting', 'NO', 'int'),
               ('YDS', 'Punting yards', 'punting', 'YDS', 'int'),
               ('YPP', 'Yards per punt', 'punting', 'YPP', 'dec'),
               ('LONG', 'Longest punt', 'punting', 'LONG', 'int'),
               ('IN 20', 'Punts inside the 20', 'punting', 'In 20', 'int')],
    }
    CAREER_COLS['TE'] = CAREER_COLS['WR']
    CAREER_COLS['LB'] = CAREER_COLS['DL']
    CAREER_COLS['DB'] = CAREER_COLS['DL']
    cols = CAREER_COLS.get(pos_group, CAREER_COLS['WR'])

    def _career_val(seas, cat, st, kind):
        if kind == 'epa':
            v = ppa_by_season.get(seas)
            return f"{v:+.3f}" if v is not None else '—'
        v = career_by_season.get(seas, {}).get(cat, {}).get(st)
        if v is None:
            return '—'
        if kind == 'pct':
            return f"{v * 100:.1f}%" if v <= 1.0 else f"{v:.1f}%"
        if kind == 'dec':
            return f"{v:.1f}"
        return f"{int(round(v)):,}"

    career_log = []
    for s in sorted(career_by_season, reverse=True):
        team_name = roster_team_by_season.get(s) or stat_team_by_season.get(s)
        meta = team_meta.get(team_name, {})
        career_log.append({
            'season': s,
            'team': team_name,
            'logo': meta.get('logo'),
            # FCS teams have no team page; unknown teams degrade to plain text
            'team_slug': meta.get('slug') if meta.get('conf') not in FCS_CONFS else None,
            'stat_values': [_career_val(s, cat, st, kind) for _lbl, _tt, cat, st, kind in cols],
        })
    career_cols = [(lbl, tt) for lbl, tt, _c, _s, _k in cols]
    career_team_count = len({r['team'] for r in career_log if r['team']})

    # Re-scope the transfer chip to the viewed season (see hero note above):
    # only meaningful when this season's stats were recorded for a team other
    # than the one in the hero (e.g. new-team roster, last season's stats).
    _season_stat_team = stat_team_by_season.get(season)
    if _season_stat_team and _season_stat_team != player['team']:
        previous_stat_teams = [_season_stat_team]
    else:
        previous_stat_teams = []

    return render_template('player.html',
        player=player, stats=stats, ppa=ppa,
        season=season, is_current_season=(season == CURRENT_SEASON),
        available_seasons=player_seasons,
        career_log=career_log, career_cols=career_cols,
        career_team_count=career_team_count,
        ap_rank=ap_rank, c1=c1, c2=c2,
        game_log=game_log,
        player_percentiles=player_percentiles,
        percentile_rows=percentile_rows,
        national_ranks=national_ranks,
        usage=usage,
        is_active_2026=is_active_2026,
        draft_status=draft_status,
        transfers_history=transfers_history,
        player_awards=player_awards,
        previous_stat_teams=previous_stat_teams,
        transfer_team_logos=transfer_team_logos,
        team_abbrevs=team_abbrevs,
    )


TRANSFER_SORTS = {
    'rating': 't.rating DESC NULLS LAST, t.stars DESC NULLS LAST, t.last_name, t.first_name',
    'date':   't.transfer_date DESC NULLS LAST, t.last_name, t.first_name',
    'name':   't.last_name, t.first_name',
    'team':   't.destination NULLS LAST, t.last_name, t.first_name',
}


@app.route('/transfers')
@cache.cached(timeout=21600, query_string=True)
def transfers():
    conn = get_db()
    try:
        cursor = conn.cursor()

        cursor.execute('SELECT DISTINCT year FROM transfers ORDER BY year DESC')
        years = [r[0] for r in cursor.fetchall()] or [CURRENT_SEASON]
        try:
            year = int(request.args.get('year', years[0]))
        except (TypeError, ValueError):
            year = years[0]
        if year not in years:
            year = years[0]
        derived = year < 2021   # pre-portal era rows come from roster diffs

        pos_filter  = request.args.get('pos', '')
        conf_filter = request.args.get('conf', '')
        q           = request.args.get('q', '').strip()
        sort        = request.args.get('sort', '')
        if sort not in TRANSFER_SORTS:
            sort = 'name' if derived else 'rating'
        elif derived and sort in ('rating', 'date'):
            sort = 'name'
        try:
            page = max(1, int(request.args.get('page', 1)))
        except (TypeError, ValueError):
            page = 1
        per_page = 50

        where, params = ['t.year = %s'], [year]
        if pos_filter:
            where.append('t.position = %s')
            params.append(pos_filter)
        if conf_filter:
            where.append('t_dest.conference = %s')
            params.append(conf_filter)
        if q:
            where.append("(t.first_name || ' ' || t.last_name) ILIKE %s")
            params.append(f'%{q}%')
        where_sql = 'WHERE ' + ' AND '.join(where)

        cursor.execute(f'''
            SELECT COUNT(*) FROM transfers t
            LEFT JOIN teams t_dest ON t_dest.name = t.destination
            {where_sql}
        ''', params)
        total_count = cursor.fetchone()[0]
        total_pages = max(1, (total_count + per_page - 1) // per_page)
        page = min(page, total_pages)

        cursor.execute(f'''
            SELECT t.first_name, t.last_name, t.position, t.origin, t.destination,
                   t.transfer_date, t.rating, t.stars, t.eligibility,
                   t.player_id, p.headshot,
                   t_dest.logo_dark, t_orig.logo_dark
            FROM transfers t
            LEFT JOIN players p ON p.id = t.player_id
            LEFT JOIN teams t_dest ON t_dest.name = t.destination
            LEFT JOIN teams t_orig ON t_orig.name = t.origin
            {where_sql}
            ORDER BY {TRANSFER_SORTS[sort]}
            LIMIT %s OFFSET %s
        ''', params + [per_page, (page - 1) * per_page])
        def _fmt_date(iso):
            try:
                return datetime.datetime.strptime(iso[:10], '%Y-%m-%d').strftime('%b %d').replace(' 0', ' ')
            except (TypeError, ValueError):
                return None
        portal = [{
            'name': f"{r[0] or ''} {r[1] or ''}".strip(), 'pos': r[2],
            'origin': r[3], 'destination': r[4], 'date': _fmt_date(r[5]), 'rating': r[6],
            'stars': r[7], 'eligibility': r[8], 'player_id': r[9],
            'headshot': r[10], 'dest_logo': r[11], 'orig_logo': r[12],
            'initials': f"{(r[0] or '?')[0]}{(r[1] or ' ')[0]}".strip(),
        } for r in cursor.fetchall()]

        # Year-scoped movement summary: biggest gainers / biggest losers
        cursor.execute('''
            SELECT t.destination, tm.logo_dark, COUNT(*) AS c
            FROM transfers t JOIN teams tm ON tm.name = t.destination
            WHERE t.year = %s
            GROUP BY 1, 2 ORDER BY c DESC, t.destination LIMIT 5
        ''', (year,))
        top_in = cursor.fetchall()
        cursor.execute('''
            SELECT t.origin, tm.logo_dark, COUNT(*) AS c
            FROM transfers t JOIN teams tm ON tm.name = t.origin
            WHERE t.year = %s
            GROUP BY 1, 2 ORDER BY c DESC, t.origin LIMIT 5
        ''', (year,))
        top_out = cursor.fetchall()

        cursor.execute('''
            SELECT COUNT(*),
                   COUNT(*) FILTER (WHERE destination IS NOT NULL AND destination != ''),
                   COUNT(*) FILTER (WHERE rating IS NOT NULL)
            FROM transfers WHERE year = %s
        ''', (year,))
        year_total, committed_count, rated_count = cursor.fetchone()

        cursor.execute('SELECT DISTINCT conference FROM teams WHERE conference IS NOT NULL ORDER BY conference')
        conferences = [r[0] for r in cursor.fetchall()]
        cursor.execute('SELECT DISTINCT position FROM transfers WHERE year = %s AND position IS NOT NULL ORDER BY position', (year,))
        positions = [r[0] for r in cursor.fetchall()]
    finally:
        release_db(conn)

    return render_template('transfers.html', portal=portal, year=year, years=years,
                           derived=derived, conferences=conferences, positions=positions,
                           pos_filter=pos_filter, conf_filter=conf_filter, q=q, sort=sort,
                           top_in=top_in, top_out=top_out, year_total=year_total,
                           committed_count=committed_count, rated_count=rated_count,
                           page=page, total_pages=total_pages, total_count=total_count,
                           per_page=per_page)


@app.route('/rivalries')
@cache.cached(timeout=86400)  # 24 hours — static data
def rivalries_page():
    conn = get_db()
    try:
        cursor = conn.cursor()

        cursor.execute('''
            SELECT DISTINCT
                CASE WHEN r.team1 < r.team2 THEN r.team1 ELSE r.team2 END as ta,
                CASE WHEN r.team1 < r.team2 THEN r.team2 ELSE r.team1 END as tb,
                r.rivalry_name,
                CASE WHEN r.team1 < r.team2 THEN t1.logo_dark ELSE t2.logo_dark END,
                CASE WHEN r.team1 < r.team2 THEN t2.logo_dark ELSE t1.logo_dark END,
                CASE WHEN r.team1 < r.team2 THEN t1.color ELSE t2.color END,
                CASE WHEN r.team1 < r.team2 THEN t2.color ELSE t1.color END,
                t1.conference
            FROM rivalries r
            LEFT JOIN teams t1 ON t1.name = r.team1
            LEFT JOIN teams t2 ON t2.name = r.team2
            WHERE r.team1 < r.team2 AND r.rivalry_name != ''
            ORDER BY r.rivalry_name
        ''')
        rivalry_list = cursor.fetchall()

        rivalry_data = []
        for r in rivalry_list:
            ta, tb = r[0], r[1]

            cursor.execute('''
                SELECT g.id, g.home_team, g.away_team, g.home_points, g.away_points,
                       g.week, g.season_type, g.notes, g.start_date,
                       t1.logo_dark, t2.logo_dark
                FROM games g
                LEFT JOIN teams t1 ON t1.name = g.home_team
                LEFT JOIN teams t2 ON t2.name = g.away_team
                WHERE ((g.home_team=%s AND g.away_team=%s)
                    OR (g.home_team=%s AND g.away_team=%s))
                AND g.completed=1
                ORDER BY g.start_date DESC
                LIMIT 1
            ''', (ta, tb, tb, ta))
            last_game = cursor.fetchone()

            rivalry_data.append({
                'ta': ta,
                'tb': tb,
                'name': r[2],
                'logo_a': r[3],
                'logo_b': r[4],
                'color_a': r[5],
                'color_b': r[6],
                'conference': r[7],
                'last_game': {
                    'id':          last_game[0],
                    'home_team':   last_game[1],
                    'away_team':   last_game[2],
                    'home_pts':    last_game[3],
                    'away_pts':    last_game[4],
                    'week':        last_game[5],
                    'season_type': last_game[6],
                    'notes':       last_game[7],
                    'date':        last_game[8][:10] if last_game[8] else '',
                    'home_logo':   last_game[9],
                    'away_logo':   last_game[10],
                } if last_game else None,
            })

    finally:
        release_db(conn)
    return render_template('rivalries.html', rivalries=rivalry_data)


@app.route('/rivalry/<team_a>/<team_b>')
def rivalry_history(team_a, team_b):
    conn = get_db()
    try:
        cursor = conn.cursor()

        cursor.execute('SELECT rivalry_name FROM rivalries WHERE team1=%s AND team2=%s LIMIT 1',
                       (team_a, team_b))
        row = cursor.fetchone()
        rivalry_name = row[0] if row else f"{team_a} vs {team_b}"

        cursor.execute('''
            SELECT g.id, g.home_team, g.away_team, g.home_points, g.away_points,
                   g.week, g.season_type, g.start_date, g.notes,
                   t1.logo_dark, t2.logo_dark
            FROM games g
            LEFT JOIN teams t1 ON t1.name = g.home_team
            LEFT JOIN teams t2 ON t2.name = g.away_team
            WHERE ((g.home_team=%s AND g.away_team=%s) OR (g.home_team=%s AND g.away_team=%s))
            AND g.completed=1
            ORDER BY g.start_date DESC
        ''', (team_a, team_b, team_b, team_a))
        games = cursor.fetchall()

        team_a_wins = sum(1 for g in games if
            (g[1] == team_a and g[3] > g[4]) or (g[2] == team_a and g[4] > g[3]))
        team_b_wins = len(games) - team_a_wins

        cursor.execute('SELECT logo_dark, color FROM teams WHERE name=%s', (team_a,))
        ta_info = cursor.fetchone()
        cursor.execute('SELECT logo_dark, color FROM teams WHERE name=%s', (team_b,))
        tb_info = cursor.fetchone()

    finally:
        release_db(conn)

    return render_template('rivalry_history.html',
        team_a=team_a, team_b=team_b,
        rivalry_name=rivalry_name,
        games=games,
        team_a_wins=team_a_wins,
        team_b_wins=team_b_wins,
        ta_logo=ta_info[0] if ta_info else None,
        tb_logo=tb_info[0] if tb_info else None,
        ta_color=ta_info[1] if ta_info else '#1c9cf0',
        tb_color=tb_info[1] if tb_info else '#17181c',
    )



# ───────────────────────────── /compare page ─────────────────────────────
# Reuses _fetch_stats_pool / _fetch_ppa_pool / _qualify_pool / _qual_threshold /
# _rank_pct / QUALIFICATIONS exactly as player_detail() does, so percentiles here
# match the player page's numbers.

COMPARE_PEER_POSITIONS = {
    'QB': ['QB'],
    'RB': ['RB', 'HB', 'FB'],
    'WR': ['WR', 'TE'],
    'TE': ['WR', 'TE'],
    'DL': ['DE', 'DT', 'NT', 'DL', 'EDGE'],
    'LB': ['LB', 'ILB', 'OLB', 'MLB'],
    'DB': ['CB', 'S', 'SS', 'FS', 'SAF', 'DB'],
}
COMPARE_WIDE_DEF_POSITIONS = ['DE', 'DT', 'NT', 'DL', 'EDGE', 'LB', 'ILB', 'OLB', 'MLB']

def _cmp_games_played(cursor, team, cache, season=CURRENT_SEASON):
    """Completed team games in a slot's own season, used as a per-game divisor.
    player_stats has season totals only (no week/game_id column), so an
    individual player's games-played can't be counted directly — team games
    completed is the closest available proxy. Cached by (team, season) so the
    same team in two different seasons stays distinct."""
    key = (team, season)
    if key not in cache:
        cursor.execute('''
            SELECT COUNT(*) FROM games
            WHERE completed=1 AND season=%s AND season_type='SeasonType.REGULAR'
            AND (home_team=%s OR away_team=%s)
        ''', (season, team, team))
        cache[key] = max(cursor.fetchone()[0] or 0, 1)
    return cache[key]

# Each compare slot carries its own season, so values and percentiles come from
# that slot's season pool — a 2021 player is ranked against 2021 peers even when
# compared against a 2023 player. `slots` is an ordered list of dicts with keys
# 'id'/'team'/'season' (players) or 'name'/'season' (teams); pools are dicts
# keyed by season.
def _cmp_row(label, slots, full_pools, qual_pools, stat_key, games_by_slot,
             higher_better=True, per_game=False, decimals=1, suffix='', scale=1):
    values = []
    for idx, s in enumerate(slots):
        pid, yr = s['id'], s['season']
        raw = full_pools.get(yr, {}).get(str(pid), {}).get(stat_key)
        if raw is None:
            values.append({'raw': None, 'display': '—', 'percentile': None})
            continue
        shown = (raw / games_by_slot[idx] if per_game else raw) * scale
        _, pct, _ = _rank_pct(pid, qual_pools.get(yr, {}), stat_key, higher_better)
        values.append({'raw': raw, 'display': f'{shown:.{decimals}f}{suffix}', 'percentile': pct})
    return {'label': label, 'higher_better': higher_better, 'values': values}

def _cmp_team_proxy_row(cursor, label, slots, column, pct=False):
    """Row sourced from team_stats as a proxy for a player-level stat that
    isn't tracked individually. No percentile — the existing qualification
    system only covers player_stats/player_ppa pools. Each slot reads its own
    (team, season)."""
    valmap = {}
    for team, yr in {(s['team'], s['season']) for s in slots if s.get('team')}:
        cursor.execute(f'SELECT {column} FROM team_stats WHERE season=%s AND team=%s', (yr, team))
        r = cursor.fetchone()
        valmap[(team, yr)] = r[0] if r else None
    values = []
    for s in slots:
        v = valmap.get((s.get('team'), s['season']))
        if v is None:
            values.append({'raw': None, 'display': '—', 'percentile': None})
        else:
            shown = v * 100 if pct else v
            values.append({'raw': v, 'display': f'{shown:.1f}{"%" if pct else ""}', 'percentile': None})
    return {'label': label, 'higher_better': True, 'values': values}

def _cmp_usage_row(cursor, label, slots, column, pct=True):
    valmap = {}
    for s in slots:
        cursor.execute(f'SELECT {column} FROM player_usage WHERE season=%s AND player_id=%s', (s['season'], s['id']))
        r = cursor.fetchone()
        valmap[(s['id'], s['season'])] = r[0] if r else None
    values = []
    for s in slots:
        v = valmap.get((s['id'], s['season']))
        if v is None:
            values.append({'raw': None, 'display': '—', 'percentile': None})
        else:
            shown = v * 100 if pct else v
            values.append({'raw': v, 'display': f'{shown:.1f}{"%" if pct else ""}', 'percentile': None})
    return {'label': label, 'higher_better': True, 'values': values}

def _cmp_assign_colors(row):
    """Best value (accounting for higher_better) -> blue, worst -> red,
    anything in between (3-way compare) -> gray. Overrides the normal
    1-24/25-44/45-59/60-79/80-99 percentile color bands per the reference design."""
    raws = [v['raw'] for v in row['values'] if v['raw'] is not None]
    if len(raws) < 2:
        for v in row['values']:
            v['color'] = 'neutral'
        return row
    best  = max(raws) if row['higher_better'] else min(raws)
    worst = min(raws) if row['higher_better'] else max(raws)
    for v in row['values']:
        if v['raw'] is None:
            v['color'] = 'none'
        elif best == worst:
            v['color'] = 'neutral'
        elif v['raw'] == best:
            v['color'] = 'blue'
        elif v['raw'] == worst:
            v['color'] = 'red'
        else:
            v['color'] = 'gray'
    return row

def _build_compare_group_rows(cursor, group_name, slots):
    """slots: ordered list of {'id','team','season'} — each carries its own
    season, so pools are fetched per distinct season and every value/percentile
    is drawn from that slot's season."""
    games_cache = {}
    games_by_slot = [_cmp_games_played(cursor, s.get('team', ''), games_cache, s['season']) for s in slots]
    peer = COMPARE_PEER_POSITIONS[group_name]
    seasons = {s['season'] for s in slots}
    rows = []

    def full_pools(cat, positions):
        return {yr: _fetch_stats_pool(cursor, cat, positions, yr) for yr in seasons}

    def ppa_pools(positions):
        return {yr: _fetch_ppa_pool(cursor, positions, yr) for yr in seasons}

    def qual_pools(full_by_yr, source_by_yr, qstat, qmin):
        return {yr: _qualify_pool(full_by_yr[yr], source_by_yr[yr], qstat, qmin) for yr in seasons}

    if group_name == 'QB':
        sp = full_pools('passing', peer)
        pass_stat, pass_min = _qual_threshold('QB', 'passing')
        sp_q = qual_pools(sp, sp, pass_stat, pass_min)
        rows.append(_cmp_row('Comp %',     slots, sp, sp_q, 'PCT', games_by_slot, suffix='%', scale=100))
        rows.append(_cmp_row('Pass Yds/G', slots, sp, sp_q, 'YDS', games_by_slot, per_game=True))
        rows.append(_cmp_row('Pass TD/G',  slots, sp, sp_q, 'TD',  games_by_slot, per_game=True))
        rows.append(_cmp_row('INT/G',      slots, sp, sp_q, 'INT', games_by_slot, per_game=True, higher_better=False))
        rows.append(_cmp_row('Yds/Att',    slots, sp, sp_q, 'YPA', games_by_slot))

        rush_sp = full_pools('rushing', ['QB'])
        rush_stat, rush_min = _qual_threshold('QB', 'rushing')
        rush_sp_q = qual_pools(rush_sp, rush_sp, rush_stat, rush_min)
        rows.append(_cmp_row('Rush Yds/G', slots, rush_sp, rush_sp_q, 'YDS', games_by_slot, per_game=True))

        pp = ppa_pools(peer)
        ppa_stat, ppa_min = _qual_threshold('QB', 'ppa')
        pp_q = qual_pools(pp, sp_q, ppa_stat, ppa_min)
        rows.append(_cmp_row('EPA / Pass Play', slots, pp, pp_q, 'avg_ppa_pass', games_by_slot, decimals=3))

    elif group_name == 'RB':
        sp = full_pools('rushing', peer)
        rush_stat, rush_min = _qual_threshold('RB', 'rushing')
        sp_q = qual_pools(sp, sp, rush_stat, rush_min)
        rows.append(_cmp_row('Rush Yds/G', slots, sp, sp_q, 'YDS', games_by_slot, per_game=True))
        rows.append(_cmp_row('Yds/Carry',  slots, sp, sp_q, 'YPC', games_by_slot))
        rows.append(_cmp_row('Rush TD/G',  slots, sp, sp_q, 'TD',  games_by_slot, per_game=True))

        pp = ppa_pools(peer)
        ppa_stat, ppa_min = _qual_threshold('RB', 'ppa')
        pp_q = qual_pools(pp, sp_q, ppa_stat, ppa_min)
        rows.append(_cmp_row('EPA / Rush', slots, pp, pp_q, 'avg_ppa_rush', games_by_slot, decimals=3))

        rows.append(_cmp_team_proxy_row(cursor, 'Rush Success Rate (Team)', slots,
                                         'off_rushing_success_rate', pct=True))
        rows.append(_cmp_usage_row(cursor, 'Rush Usage', slots, 'rush', pct=True))

    elif group_name in ('WR', 'TE'):
        sp = full_pools('receiving', peer)
        rec_stat, rec_min = _qual_threshold(group_name, 'receiving')
        sp_q = qual_pools(sp, sp, rec_stat, rec_min)
        rows.append(_cmp_row('Rec/G',     slots, sp, sp_q, 'REC', games_by_slot, per_game=True))
        rows.append(_cmp_row('Rec Yds/G', slots, sp, sp_q, 'YDS', games_by_slot, per_game=True))
        rows.append(_cmp_row('Rec TD/G',  slots, sp, sp_q, 'TD',  games_by_slot, per_game=True))
        rows.append(_cmp_row('Yds/Rec',   slots, sp, sp_q, 'YPR', games_by_slot))

        pp = ppa_pools(peer)
        ppa_stat, ppa_min = _qual_threshold(group_name, 'ppa')
        pp_q = qual_pools(pp, sp_q, ppa_stat, ppa_min)
        rows.append(_cmp_row('EPA / Play', slots, pp, pp_q, 'avg_ppa_all', games_by_slot, decimals=3))

    elif group_name in ('DL', 'LB'):
        sp_wide = full_pools('defensive', COMPARE_WIDE_DEF_POSITIONS)
        sp_narrow = full_pools('defensive', peer)
        def_stat, def_min = _qual_threshold(group_name, 'defensive')
        sp_wide_q = qual_pools(sp_wide, sp_wide, def_stat, def_min)
        sp_narrow_q = qual_pools(sp_narrow, sp_narrow, def_stat, def_min)
        rows.append(_cmp_row('Tackles/G', slots, sp_wide, sp_wide_q, 'TOT',   games_by_slot, per_game=True))
        rows.append(_cmp_row('Sacks/G',   slots, sp_wide, sp_wide_q, 'SACKS', games_by_slot, per_game=True))
        rows.append(_cmp_row('TFL/G',     slots, sp_narrow, sp_narrow_q, 'TFL', games_by_slot, per_game=True))
        rows.append(_cmp_row('PBU/G',     slots, sp_narrow, sp_narrow_q, 'PD',  games_by_slot, per_game=True))
        # No EPA/Play row here — player_ppa only covers offensive skill positions
        # (QB/RB/FB/TE/WR) in this dataset, so it would always be empty for DL/LB.

    elif group_name == 'DB':
        sp = full_pools('defensive', peer)
        def_stat, def_min = _qual_threshold('DB', 'defensive')
        sp_q = qual_pools(sp, sp, def_stat, def_min)
        rows.append(_cmp_row('Tackles/G', slots, sp, sp_q, 'TOT',   games_by_slot, per_game=True))
        rows.append(_cmp_row('Sacks/G',   slots, sp, sp_q, 'SACKS', games_by_slot, per_game=True))
        rows.append(_cmp_row('TFL/G',     slots, sp, sp_q, 'TFL',   games_by_slot, per_game=True))
        rows.append(_cmp_row('PBU/G',     slots, sp, sp_q, 'PD',    games_by_slot, per_game=True))
        # No EPA/Play row — player_ppa has no DB rows in this dataset either.

    for row in rows:
        _cmp_assign_colors(row)
    return rows

COMPARE_TEAM_STAT_DEFS = [
    ('Off. EPA / Play',    'off_ppa',                  True,  3, ''),
    ('Off. Success Rate',  'off_success_rate',         True,  1, '%'),
    ('Off. Explosiveness', 'off_explosiveness',        True,  2, ''),
    ('Rush Success Rate',  'off_rushing_success_rate', True,  1, '%'),
    ('Pass Success Rate',  'off_passing_success_rate', True,  1, '%'),
    ('Def. EPA / Play',    'def_ppa',                  False, 3, ''),
    ('Def. Success Rate',  'def_success_rate',         False, 1, '%'),
]

def _build_compare_team_rows(cursor, slots):
    """slots: ordered list of {'name','season'} — each column reads its own
    (team, season), so the same program in two years compares cleanly."""
    pairs = {(s['name'], s['season']) for s in slots}
    ts_by_key, sp_by_key, svr_by_key = {}, {}, {}
    for team, yr in pairs:
        cursor.execute('SELECT * FROM team_stats WHERE season=%s AND team=%s', (yr, team))
        r = cursor.fetchone()
        if r:
            ts_by_key[(team, yr)] = dict(zip([d[0] for d in cursor.description], r))
        cursor.execute('SELECT rating, ranking FROM sp_ratings WHERE season=%s AND team=%s', (yr, team))
        r = cursor.fetchone()
        if r:
            sp_by_key[(team, yr)] = {'rating': r[0], 'ranking': r[1]}
        cursor.execute('''SELECT net_rating, net_ranking, off_rating, off_ranking, def_rating, def_ranking
                          FROM savant_ratings WHERE season=%s AND team=%s''', (yr, team))
        r = cursor.fetchone()
        if r:
            svr_by_key[(team, yr)] = {'net': r[0], 'net_rk': r[1], 'off': r[2], 'off_rk': r[3],
                                      'def': r[4], 'def_rk': r[5]}

    def _key(s):
        return (s['name'], s['season'])

    def _svr_row(label, key, rk_key, higher_better, signed=False):
        values = []
        for s in slots:
            sv = svr_by_key.get(_key(s))
            if sv and sv.get(key) is not None:
                num = f'{sv[key]:+.1f}' if signed else f'{sv[key]:.1f}'
                values.append({'raw': sv[key], 'display': f'{num} (#{sv[rk_key]})', 'percentile': None})
            else:
                values.append({'raw': None, 'display': '—', 'percentile': None})
        return {'label': label, 'higher_better': higher_better, 'values': values}

    rows = [
        _svr_row('Net Rating',        'net', 'net_rk', True, signed=True),
        _svr_row('Offensive Rating',  'off', 'off_rk', True),
        _svr_row('Defensive Rating',  'def', 'def_rk', False),
    ]
    for label, col, higher_better, decimals, suffix in COMPARE_TEAM_STAT_DEFS:
        values = []
        for s in slots:
            v = ts_by_key.get(_key(s), {}).get(col)
            if v is None:
                values.append({'raw': None, 'display': '—', 'percentile': None})
            else:
                shown = v * 100 if suffix == '%' else v
                values.append({'raw': v, 'display': f'{shown:.{decimals}f}{suffix}', 'percentile': None})
        rows.append({'label': label, 'higher_better': higher_better, 'values': values})

    sp_values = []
    for s in slots:
        sp = sp_by_key.get(_key(s))
        if sp and sp.get('rating') is not None:
            sp_values.append({'raw': sp['rating'], 'display': f"{sp['rating']:.1f} (#{sp['ranking']})", 'percentile': None})
        else:
            sp_values.append({'raw': None, 'display': '—', 'percentile': None})
    rows.append({'label': 'SP+ Rating', 'higher_better': True, 'values': sp_values})

    for row in rows:
        _cmp_assign_colors(row)
    return rows


@app.route('/img-proxy')
def img_proxy():
    """Same-origin passthrough for headshots/logos used by the compare
    export. The R2 headshot bucket sends no CORS headers, so loading those
    images cross-origin taints the html2canvas canvas and the download
    fails to render them. Routing them through our own origin fixes that.
    Host-allowlisted to image CDNs to avoid an open proxy."""
    from urllib.parse import urlparse
    url = request.args.get('url', '')
    netloc = urlparse(url).netloc.lower()
    if not (netloc.endswith('.r2.dev') or netloc.endswith('espncdn.com')):
        return 'Forbidden', 403
    try:
        r = req.get(url, timeout=3)
        ctype = r.headers.get('Content-Type', 'image/png')
        if r.status_code != 200 or not ctype.startswith('image/'):
            return '', 502
        return Response(r.content, content_type=ctype,
                        headers={'Cache-Control': 'public, max-age=604800'})
    except Exception:
        return '', 502


@app.route('/compare')
@cache.cached(timeout=21600, query_string=True)  # players/teams + season are all in the query string
def compare():
    season = requested_season()   # default season for slots without an explicit year
    avail = get_available_seasons()
    mode = request.args.get('type', 'player')
    if mode not in ('player', 'team'):
        mode = 'player'
    pos_filter = request.args.get('pos', '')

    def slot_season(i):
        """Per-slot season from ?y{i}=, falling back to the page default so old
        single-season links keep working."""
        raw = request.args.get(f'y{i}', type=int)
        return raw if (raw and raw in avail) else season
    slot_seasons = [slot_season(i) for i in (1, 2, 3)]

    conn = get_db()
    try:
        cursor = conn.cursor()
        # `slots` stays exactly 3 long (None for an empty/invalid slot) so the
        # search UI can address slot 1/2/3 correctly by index. `active_*` is the
        # compacted (2 or 3 long) list actually used for the card + stat rows.
        slots, rows, group_name = [None, None, None], [], None

        def ap_rank(team, yr):
            if not team:
                return None
            cursor.execute('SELECT rank FROM ap_rankings WHERE season=%s AND team=%s', (yr, team))
            r = cursor.fetchone()
            return r[0] if r else None

        if mode == 'team':
            slot_names = [request.args.get(f't{i}') for i in (1, 2, 3)]
            valid_names = [n for n in slot_names if n]
            info_by_name = {}
            if valid_names:
                ph = ','.join(['%s'] * len(valid_names))
                cursor.execute(f'''
                    SELECT name, conference, logo_dark, color, alt_color
                    FROM teams WHERE name IN ({ph})
                ''', valid_names)
                info_by_name = {r[0]: r for r in cursor.fetchall()}

            for i, name in enumerate(slot_names):
                info = info_by_name.get(name) if name else None
                if info:
                    slots[i] = {
                        'name': info[0], 'conference': info[1], 'logo_dark': info[2],
                        'color': info[3], 'alt_color': info[4], 'season': slot_seasons[i],
                        'years': avail,  # teams exist every loaded season
                        'ap_rank': ap_rank(name, slot_seasons[i]),
                    }

            active = [s for s in slots if s]
            if len(active) >= 2:
                rows = _build_compare_team_rows(cursor, active)

        else:
            slot_ids = [int(raw) if raw and raw.isdigit() else None
                        for raw in (request.args.get(f'p{i}') for i in (1, 2, 3))]
            valid_ids = [pid for pid in slot_ids if pid is not None]
            info_by_id, seasons_by_pid = {}, {}
            if valid_ids:
                ph = ','.join(['%s'] * len(valid_ids))
                cursor.execute(f'''
                    SELECT p.id, p.first_name, p.last_name, p.team, p.position, p.jersey,
                           p.headshot, t.logo_dark, t.color, t.alt_color, t.conference
                    FROM players p LEFT JOIN teams t ON p.team = t.name
                    WHERE p.id IN ({ph})
                ''', valid_ids)
                info_by_id = {r[0]: r for r in cursor.fetchall()}
                # Seasons each player actually recorded stats — the only years
                # worth offering in that slot's dropdown.
                cursor.execute(f'''
                    SELECT player_id, season FROM player_stats
                    WHERE player_id IN ({ph}) AND season IS NOT NULL
                    GROUP BY player_id, season
                ''', [str(v) for v in valid_ids])
                for pid_s, ssn in cursor.fetchall():
                    seasons_by_pid.setdefault(int(pid_s), []).append(ssn)
                for pid in seasons_by_pid:
                    seasons_by_pid[pid].sort(reverse=True)

            for i, pid in enumerate(slot_ids):
                info = info_by_id.get(pid) if pid is not None else None
                if info:
                    years = seasons_by_pid.get(pid, [])
                    # Clamp to a season the player actually played: honor the
                    # requested year if valid, else default to their most recent.
                    eff = slot_seasons[i] if slot_seasons[i] in years else (years[0] if years else slot_seasons[i])
                    slots[i] = {
                        'id': info[0], 'first_name': info[1], 'last_name': info[2],
                        'team': info[3], 'position': info[4], 'jersey': info[5],
                        'headshot': info[6], 'logo_dark': info[7], 'color': info[8],
                        'alt_color': info[9], 'conference': info[10], 'season': eff,
                        'years': years or [eff],
                        'ap_rank': ap_rank(info[3], eff),
                    }

            active = [s for s in slots if s]
            if not pos_filter and active:
                first_pos = (active[0]['position'] or '').upper()
                pos_filter = POS_GROUP_MAP.get(first_pos, 'QB')
            group_name = pos_filter if pos_filter in COMPARE_PEER_POSITIONS else 'QB'

            if len(active) >= 2:
                rows = _build_compare_group_rows(cursor, group_name, active)
    finally:
        release_db(conn)

    players = slots if mode == 'player' else [None, None, None]
    teams_out = slots if mode == 'team' else [None, None, None]
    active_entities = [s for s in slots if s]

    base_params = request.args.to_dict()
    tab_urls = {}
    for tab in ('QB', 'RB', 'WR', 'TE', 'DL', 'LB', 'DB', 'TEAMS'):
        params = dict(base_params)
        if tab == 'TEAMS':
            params['type'] = 'team'
            params.pop('pos', None)
        else:
            params['type'] = 'player'
            params['pos'] = tab
        tab_urls[tab] = '/compare?' + urlencode(params)

    return render_template('compare.html',
        mode=mode, players=players, teams=teams_out, active_entities=active_entities, rows=rows,
        season=season, available_seasons=get_available_seasons(),
        group_name=group_name, pos_filter=pos_filter, tab_urls=tab_urls,
    )


# ── CFP Bracket ──────────────────────────────────────────────────────────

_CFP_BOWL_SITES = OrderedDict([
    ('Rose Bowl', 'Pasadena, CA'),
    ('Sugar Bowl', 'New Orleans, LA'),
    ('Orange Bowl', 'Miami Gardens, FL'),
    ('Cotton Bowl', 'Arlington, TX'),
    ('Peach Bowl', 'Atlanta, GA'),
    ('Fiesta Bowl', 'Glendale, AZ'),
])

# In the 12-team format, the bye seed hosting each quarterfinal determines
# which first-round pairing feeds it: #1 gets the 8/9 winner, #4 the 5/12
# winner, #2 the 7/10 winner, #3 the 6/11 winner.
_CFP_FEED_BY_BYE = {1: (8, 9), 4: (5, 12), 2: (7, 10), 3: (6, 11)}


def _cfp_bowl_label(notes):
    low = (notes or '').lower()
    for bowl, site in _CFP_BOWL_SITES.items():
        if bowl.split()[0].lower() in low:
            return f'{bowl} · {site}'
    if 'national championship' in low:
        return 'Miami Gardens, FL'
    return None


def _cfp_game_winner(g):
    if not g or not g['completed']:
        return None
    if g['home_points'] is None or g['away_points'] is None:
        return None
    if g['home_points'] == g['away_points']:
        return None
    return g['home_team'] if g['home_points'] > g['away_points'] else g['away_team']


def _cfp_game_is_live(g):
    if not g or g['completed']:
        return False
    try:
        return g['start_date'][:10] == datetime.date.today().isoformat()
    except (TypeError, IndexError):
        return False


def _cfp_find_game(pool, *names):
    """Game in pool whose participants include every given (non-None) name."""
    wanted = {n for n in names if n}
    if not wanted:
        return None
    for g in pool:
        if wanted <= {g['home_team'], g['away_team']}:
            return g
    return None


def _cfp_seed_teams(poll, fr_games, qf_games):
    """team -> seed (1-12). Reconstructed from the playoff games themselves:
    first-round hosts are seeds 5-8 and the four bye teams are 1-4, with
    exact numbers pinned down by which first-round winner each bye met in
    the quarterfinals. The AP poll only breaks the tie of ordering the four
    byes 1-4 — the final poll can't be used directly as seeds because it
    re-ranks teams after the playoff ran."""
    ap_rank = {t: r for t, r in poll}

    fr_teams = {t for g in fr_games for t in (g['home_team'], g['away_team'])}
    qf_ready = len(fr_games) == 4 and len(qf_games) == 4

    if qf_ready:
        seeds = {}
        pairs = []  # (bye_team, feeder_fr_game)
        for g in qf_games:
            bye = next((t for t in (g['home_team'], g['away_team'])
                        if t not in fr_teams), None)
            other = g['away_team'] if bye == g['home_team'] else g['home_team']
            feeder = next((f for f in fr_games if _cfp_game_winner(f) == other), None)
            if bye is None or feeder is None:
                qf_ready = False
                break
            pairs.append((bye, feeder))
        if qf_ready:
            for i, (bye, _) in enumerate(
                    sorted(pairs, key=lambda p: ap_rank.get(p[0], 99))):
                seeds[bye] = i + 1
            for bye, feeder in pairs:
                hi, lo = _CFP_FEED_BY_BYE[seeds[bye]]
                seeds[feeder['home_team']] = hi
                seeds[feeder['away_team']] = lo
            return seeds

    # Fallback (playoff not yet played / partial data): straight AP top 12.
    return {t: r for t, r in poll[:12]}


FIRST_12_TEAM_SEASON = 2024  # the CFP expanded to 12 teams in 2024

@app.route('/bracket')
@cache.cached(timeout=21600, query_string=True)
def bracket_page():
    season = requested_season()
    conn = get_db()
    try:
        cursor = conn.cursor()

        # Seasons with stored committee seeds (backfill_cfp.py) drive the
        # selector — the CFP itself predates our data, which starts 2016.
        try:
            cursor.execute('SELECT DISTINCT season FROM cfp_seeds ORDER BY season DESC')
            bracket_seasons = [r[0] for r in cursor.fetchall()]
        except Exception:
            conn.rollback()
            bracket_seasons = [s for s in get_available_seasons() if s >= FIRST_12_TEAM_SEASON]
        if bracket_seasons and season not in bracket_seasons:
            return render_template('bracket.html', unsupported_season=season,
                                   season=season, bracket_seasons=bracket_seasons,
                                   four_team=False, bracket=None, champion=None)

        four_team = season < FIRST_12_TEAM_SEASON

        # Seeds come from the stored selection-day committee ranking — the
        # final AP poll can't seed a bracket (it re-ranks after the playoff,
        # which is exactly how the 2024 bracket ended up scrambled).
        seeds = {}
        try:
            cursor.execute('SELECT team, seed FROM cfp_seeds WHERE season = %s', (season,))
            seeds = {r[0]: r[1] for r in cursor.fetchall()}
        except Exception:
            conn.rollback()

        cursor.execute('''
            SELECT id, home_team, away_team, home_points, away_points,
                   completed, notes, start_date
            FROM games
            WHERE season = %s
              AND UPPER(season_type) LIKE '%%POSTSEASON%%'
            ORDER BY start_date ASC
        ''', (season,))
        post_games = [{
            'id': r[0], 'home_team': r[1], 'away_team': r[2],
            'home_points': r[3], 'away_points': r[4], 'completed': r[5],
            'notes': r[6] or '', 'start_date': r[7] or '',
        } for r in cursor.fetchall()]
        cfp_games = [g for g in post_games
                     if 'college football playoff' in g['notes'].lower()
                     or 'cfp' in g['notes'].lower()]

        fr_games = [g for g in cfp_games if 'first round' in g['notes'].lower()]
        qf_games = [g for g in cfp_games if 'quarterfinal' in g['notes'].lower()]
        sf_games = [g for g in cfp_games if 'semifinal' in g['notes'].lower()]
        nc_games = [g for g in cfp_games if 'national championship' in g['notes'].lower()]

        # Some early seasons (2016) carry no notes on the semifinal games —
        # recover them structurally: a postseason game between two seeded
        # teams that isn't the championship is a semifinal.
        if four_team and len(sf_games) < 2 and seeds:
            nc_ids = {g['id'] for g in nc_games}
            for g in post_games:
                if g['id'] in nc_ids or g in sf_games:
                    continue
                if g['home_team'] in seeds and g['away_team'] in seeds:
                    sf_games.append(g)

        if not seeds:
            # Fallback: derive from the playoff structure + final AP poll
            cursor.execute('''
                SELECT team, rank FROM ap_rankings
                WHERE season = %s
                ORDER BY (season_type = 'postseason') DESC, week DESC, rank ASC
                LIMIT 25
            ''', (season,))
            poll = cursor.fetchall()
            seeds = _cfp_seed_teams(poll, fr_games, qf_games)
        team_by_seed = {s: t for t, s in seeds.items()}

        cursor.execute('''
            SELECT name, logo_dark, logo, color, alt_color, abbreviation, conference
            FROM teams WHERE name = ANY(%s)
        ''', (list(seeds.keys()) or [''],))
        teams_map = {r[0]: {
            'name': r[0], 'logo': r[1] or r[2], 'color': r[3] or '#1c9cf0',
            'alt_color': r[4], 'abbreviation': r[5], 'conference': r[6],
        } for r in cursor.fetchall()}
    finally:
        release_db(conn)

    def team_side(name, seed, game, bye=False):
        if name is None:
            return None
        info = teams_map.get(name, {})
        pts = opp_pts = None
        if game:
            if game['home_team'] == name:
                pts, opp_pts = game['home_points'], game['away_points']
            elif game['away_team'] == name:
                pts, opp_pts = game['away_points'], game['home_points']
        decided = bool(game and game['completed'] and pts is not None
                       and opp_pts is not None and pts != opp_pts)
        return {
            'name': name,
            'seed': seed,
            'logo': info.get('logo'),
            'color': info.get('color', '#1c9cf0'),
            'conference': info.get('conference'),
            'abbreviation': info.get('abbreviation'),
            'points': pts,
            'is_winner': decided and pts > opp_pts,
            'is_loser': decided and pts < opp_pts,
            'is_bye': bye,
        }

    def matchup(slot, top_name, top_seed, bottom_name, bottom_seed, game,
                top_bye=False):
        winner = _cfp_game_winner(game)
        score = None
        if winner and game:
            hi, lo = sorted((game['home_points'], game['away_points']), reverse=True)
            score = f'{hi}-{lo}'
        return {
            'slot': slot,
            'top': team_side(top_name, top_seed, game, bye=top_bye),
            'bottom': team_side(bottom_name, bottom_seed, game),
            'winner': winner,
            'score': score,
            'game_id': game['id'] if game else None,
            'completed': bool(game and game['completed']),
            'live': _cfp_game_is_live(game),
            'bowl': _cfp_bowl_label(game['notes']) if game else None,
        }

    if four_team:
        # 4-team era (2016-23): two semifinals (1v4, 2v3) and the championship.
        semifinals = []
        for slot, hi, lo in (('SF1', 1, 4), ('SF2', 2, 3)):
            t_hi, t_lo = team_by_seed.get(hi), team_by_seed.get(lo)
            game = _cfp_find_game(sf_games, t_hi, t_lo) or _cfp_find_game(sf_games, t_hi)
            semifinals.append(matchup(slot, t_hi, hi, t_lo, lo, game))
        t_left = semifinals[0]['winner']
        t_right = semifinals[1]['winner']
        nc_game = _cfp_find_game(nc_games, t_left, t_right) or \
            (nc_games[0] if nc_games else None)
        championship = matchup('NC', t_left, seeds.get(t_left),
                               t_right, seeds.get(t_right), nc_game)
        champion = None
        if championship['winner']:
            champion = team_side(championship['winner'],
                                 seeds.get(championship['winner']), nc_game)
        bracket = {'round1': [], 'quarterfinals': [],
                   'semifinals': semifinals, 'championship': championship}
        cfp_teams = [dict(teams_map.get(team_by_seed.get(s), {}) or {},
                          seed=s, name=team_by_seed.get(s))
                     for s in range(1, 5) if team_by_seed.get(s)]
        return render_template('bracket.html', bracket=bracket,
                               season=season, bracket_seasons=bracket_seasons,
                               four_team=True,
                               cfp_teams=cfp_teams, champion=champion)

    # First round — slot letter, high seed, low seed
    round1 = []
    for slot, hi, lo in (('A', 8, 9), ('B', 5, 12), ('C', 6, 11), ('D', 7, 10)):
        t_hi, t_lo = team_by_seed.get(hi), team_by_seed.get(lo)
        round1.append(matchup(slot, t_hi, hi, t_lo, lo,
                              _cfp_find_game(fr_games, t_hi, t_lo)))
    r1_by_slot = {m['slot']: m for m in round1}

    # Quarterfinals — bye seed on top, first-round winner below
    quarterfinals = []
    for slot, bye_seed, feed_slot in (('QF1', 1, 'A'), ('QF2', 4, 'B'),
                                      ('QF3', 2, 'D'), ('QF4', 3, 'C')):
        bye_team = team_by_seed.get(bye_seed)
        adv = r1_by_slot[feed_slot]['winner']
        game = _cfp_find_game(qf_games, bye_team, adv) or \
            _cfp_find_game(qf_games, bye_team)
        quarterfinals.append(matchup(slot, bye_team, bye_seed, adv,
                                     seeds.get(adv), game, top_bye=True))
    qf_by_slot = {m['slot']: m for m in quarterfinals}

    # Semifinals — winners of the paired quarterfinals
    semifinals = []
    for slot, top_feed, bottom_feed in (('SF1', 'QF1', 'QF2'),
                                        ('SF2', 'QF3', 'QF4')):
        t_top = qf_by_slot[top_feed]['winner']
        t_bot = qf_by_slot[bottom_feed]['winner']
        game = _cfp_find_game(sf_games, t_top, t_bot)
        semifinals.append(matchup(slot, t_top, seeds.get(t_top),
                                  t_bot, seeds.get(t_bot), game))

    # Championship
    t_left = semifinals[0]['winner']
    t_right = semifinals[1]['winner']
    nc_game = _cfp_find_game(nc_games, t_left, t_right)
    championship = matchup('NC', t_left, seeds.get(t_left),
                           t_right, seeds.get(t_right), nc_game)

    champion = None
    if championship['winner']:
        champion = team_side(championship['winner'],
                             seeds.get(championship['winner']), nc_game)

    bracket = {
        'round1': round1,
        'quarterfinals': quarterfinals,
        'semifinals': semifinals,
        'championship': championship,
    }
    cfp_teams = [dict(teams_map.get(team_by_seed.get(s), {}) or {},
                      seed=s, name=team_by_seed.get(s))
                 for s in range(1, 13) if team_by_seed.get(s)]

    return render_template('bracket.html', bracket=bracket,
                           season=season, bracket_seasons=bracket_seasons,
                           four_team=False,
                           cfp_teams=cfp_teams, champion=champion)


# Player Explorer — per position-group scatter axes. Values are raw stats
# read from the same pools used for percentiles; categories match the hero
# cards. (key, label, category, stat_type, invert, decimals, unit)
EXPLORER_PLAYER_SPEC = {
    'QB': {'label': 'Quarterbacks', 'peer': ['QB'], 'axes': [
        ('pass_yds', 'Passing Yards',   'passing',  'YDS', False, 0, ''),
        ('pass_td',  'Passing TDs',     'passing',  'TD',  False, 0, ''),
        ('comp_pct', 'Completion %',    'passing',  'PCT', False, 1, '%'),
        ('ypa',      'Yards / Attempt', 'passing',  'YPA', False, 1, ''),
        ('interceptions', 'Interceptions', 'passing', 'INT', True, 0, ''),
        ('rush_yds', 'Rushing Yards',   'rushing',  'YDS', False, 0, ''),
        ('epa',      'EPA / Play',      'ppa', 'avg_ppa_all', False, 3, ''),
    ]},
    'RB': {'label': 'Running Backs', 'peer': ['RB', 'HB', 'FB'], 'axes': [
        ('rush_yds', 'Rushing Yards',   'rushing',  'YDS', False, 0, ''),
        ('rush_td',  'Rushing TDs',     'rushing',  'TD',  False, 0, ''),
        ('carries',  'Carries',         'rushing',  'CAR', False, 0, ''),
        ('ypc',      'Yards / Carry',   'rushing',  'YPC', False, 1, ''),
        ('rec_yds',  'Receiving Yards', 'receiving','YDS', False, 0, ''),
        ('receptions','Receptions',     'receiving','REC', False, 0, ''),
        ('epa',      'EPA / Play',      'ppa', 'avg_ppa_all', False, 3, ''),
    ]},
    'WR': {'label': 'Receivers (WR)', 'peer': ['WR'], 'axes': [
        ('rec_yds',  'Receiving Yards', 'receiving','YDS', False, 0, ''),
        ('rec_td',   'Receiving TDs',   'receiving','TD',  False, 0, ''),
        ('receptions','Receptions',     'receiving','REC', False, 0, ''),
        ('ypr',      'Yards / Reception','receiving','AVG',False, 1, ''),
        ('epa',      'EPA / Play',      'ppa', 'avg_ppa_all', False, 3, ''),
    ]},
    'TE': {'label': 'Tight Ends', 'peer': ['TE'], 'axes': [
        ('rec_yds',  'Receiving Yards', 'receiving','YDS', False, 0, ''),
        ('rec_td',   'Receiving TDs',   'receiving','TD',  False, 0, ''),
        ('receptions','Receptions',     'receiving','REC', False, 0, ''),
        ('ypr',      'Yards / Reception','receiving','AVG',False, 1, ''),
        ('epa',      'EPA / Play',      'ppa', 'avg_ppa_all', False, 3, ''),
    ]},
    # No EPA/Play axis for defensive groups — player_ppa only covers offensive
    # skill positions, so it would be an always-empty axis (and a blank default).
    'DL': {'label': 'Defensive Line', 'peer': ['DE','DT','NT','DL','EDGE'], 'axes': [
        ('tackles',  'Total Tackles',   'defensive','TOT', False, 0, ''),
        ('tfl',      'Tackles for Loss','defensive','TFL', False, 1, ''),
        ('sacks',    'Sacks',           'defensive','SACKS',False,1, ''),
    ]},
    'LB': {'label': 'Linebackers', 'peer': ['LB','ILB','OLB','MLB'], 'axes': [
        ('tackles',  'Total Tackles',   'defensive','TOT', False, 0, ''),
        ('tfl',      'Tackles for Loss','defensive','TFL', False, 1, ''),
        ('sacks',    'Sacks',           'defensive','SACKS',False,1, ''),
    ]},
    'DB': {'label': 'Defensive Backs', 'peer': ['CB','S','SS','FS','SAF','DB'], 'axes': [
        ('tackles',  'Total Tackles',   'defensive','TOT', False, 0, ''),
        ('interceptions','Interceptions','defensive','INT',False, 0, ''),
        ('pd',       'Passes Defended', 'defensive','PD',  False, 0, ''),
        ('tfl',      'Tackles for Loss','defensive','TFL', False, 1, ''),
    ]},
}

# Radar/percentile profile axes per position group — a curated subset of the
# hero-card percentiles (all higher-is-better so the shape reads cleanly).
# (label, kind, stat_key)  kind: 'stat' category / 'ppa'
EXPLORER_RADAR_SPEC = {
    'QB': [('Pass Yds','passing','YDS'), ('Pass TD','passing','TD'),
           ('Comp %','passing','PCT'), ('Yds/Att','passing','YPA'),
           ('EPA/Play','ppa','avg_ppa_all')],
    'RB': [('Rush Yds','rushing','YDS'), ('Rush TD','rushing','TD'),
           ('Yds/Carry','rushing','YPC'), ('Rec Yds','receiving_wide','YDS'),
           ('EPA/Play','ppa','avg_ppa_all')],
    'WR': [('Rec Yds','receiving','YDS'), ('Rec TD','receiving','TD'),
           ('Receptions','receiving','REC'), ('Yds/Rec','receiving','AVG'),
           ('EPA/Play','ppa','avg_ppa_all')],
    'TE': [('Rec Yds','receiving','YDS'), ('Rec TD','receiving','TD'),
           ('Receptions','receiving','REC'), ('Yds/Rec','receiving','AVG'),
           ('EPA/Play','ppa','avg_ppa_all')],
    'DL': [('Tackles','def_wide','TOT'), ('Sacks','def_wide','SACKS'),
           ('TFL','defensive','TFL'), ('EPA/Play','ppa','avg_ppa_all')],
    'LB': [('Tackles','def_wide','TOT'), ('Sacks','def_wide','SACKS'),
           ('TFL','defensive','TFL'), ('EPA/Play','ppa','avg_ppa_all')],
    'DB': [('Tackles','defensive','TOT'), ('Interceptions','defensive','INT'),
           ('Pass Defended','defensive','PD'), ('EPA/Play','ppa','avg_ppa_all')],
}
_DEF_WIDE = ['DE','DT','NT','DL','EDGE','LB','ILB','OLB','MLB']
_RB_REC_WIDE = ['WR','TE','RB','HB','FB']


def _explorer_player_scope(cursor, group, season, qualified_only=True, conference=None):
    """Players in a position group (scoped to one conference — plotting an
    entire position across all of FBS is too dense to be readable or cheap to
    ship) with their raw values for the group's scatter axes. Defaults to the
    leaderboards' qualified pool (min attempts/carries/etc.); qualified_only=
    False ships the whole group within the conference. Reuses the persisted
    percentile pools, so this is cheap."""
    spec = EXPLORER_PLAYER_SPEC[group]
    peer = spec['peer']
    cat_pools, need_ppa = {}, False
    for _k, _l, cat, _st, _inv, _dp, _u in spec['axes']:
        if cat == 'ppa':
            need_ppa = True
        elif cat not in cat_pools:
            cat_pools[cat] = _fetch_stats_pool(cursor, cat, peer, season)
    ppa_pool = _fetch_ppa_pool(cursor, peer, season) if need_ppa else {}

    qcat = QUAL_SOURCE_CATEGORY[group]
    qstat, qmin = _qual_threshold(group, qcat)
    qpool = cat_pools.get(qcat) or _fetch_stats_pool(cursor, qcat, peer, season)
    qualified = {pid for pid, d in qpool.items() if (d.get(qstat) or 0) >= qmin}

    pids = set(ppa_pool)
    for cp in cat_pools.values():
        pids |= set(cp)
    if qualified_only:
        pids &= qualified
    if not pids:
        return []
    int_pids = [int(p) for p in pids if str(p).lstrip('-').isdigit()]
    # Restrict to the requested conference at the DB, so only that conference's
    # players are ever built/shipped — never the full FBS position group.
    if conference:
        cursor.execute('''
            SELECT p.id, p.first_name, p.last_name, p.team, p.headshot,
                   t.logo_dark, t.color, t.conference
            FROM players p JOIN teams t ON t.name = p.team
            WHERE p.id = ANY(%s) AND t.conference = %s
        ''', (int_pids, conference))
    else:
        cursor.execute('''
            SELECT p.id, p.first_name, p.last_name, p.team, p.headshot,
                   t.logo_dark, t.color, t.conference
            FROM players p LEFT JOIN teams t ON t.name = p.team
            WHERE p.id = ANY(%s)
        ''', (int_pids,))
    meta = {str(r[0]): r for r in cursor.fetchall()}

    from urllib.parse import quote
    out = []
    for pid in pids:
        m = meta.get(str(pid))
        if not m or not (m[1] or m[2]):
            continue
        stats = {}
        for key, _l, cat, st, _inv, dp, _u in spec['axes']:
            src = ppa_pool if cat == 'ppa' else cat_pools[cat]
            v = src.get(pid, {}).get(st)
            stats[key] = round(v, dp) if v is not None else None
        # Headshots are R2-hosted (no CORS headers); Plotly draws marker images
        # to a canvas, which taints on cross-origin sources — so route them
        # through the same-origin image proxy (browser-cached a week).
        headshot = f'/img-proxy?url={quote(m[4], safe="")}' if m[4] else None
        out.append({
            'id': int(pid), 'name': f'{m[1] or ""} {m[2] or ""}'.strip(),
            'team': m[3] or '', 'headshot': headshot, 'logo': m[5],
            'color': m[6] or '#888', 'conf': m[7] or '',
            'qualified': pid in qualified,
            'url': player_url(int(pid), season), 'stats': stats,
        })
    return out


def _radar_pool(cursor, kind, group, season):
    """Resolve a radar axis's peer pool by its `kind` marker."""
    if kind == 'ppa':
        return _fetch_ppa_pool(cursor, COMPARE_PEER_POSITIONS[group], season)
    if kind == 'def_wide':
        return _fetch_stats_pool(cursor, 'defensive', _DEF_WIDE, season)
    if kind == 'receiving_wide':
        return _fetch_stats_pool(cursor, 'receiving', _RB_REC_WIDE, season)
    return _fetch_stats_pool(cursor, kind, COMPARE_PEER_POSITIONS[group], season)


def _explorer_radar(cursor, player_id, season):
    """Percentile profile for one player: same pools, qualification, and
    percentile math as the player hero cards, reduced to the radar axes."""
    cursor.execute('SELECT first_name, last_name, team, position, headshot FROM players WHERE id=%s',
                   (player_id,))
    row = cursor.fetchone()
    if not row:
        return None
    first, last, team, pos, headshot = row
    group = POS_GROUP_MAP.get((pos or '').upper())
    if not group or group not in EXPLORER_RADAR_SPEC:
        return None
    peer = COMPARE_PEER_POSITIONS[group]

    # Qualified peer set for this group (from the group's counting-stat pool),
    # applied to every stat pool so percentiles match the hero exactly.
    qcat = QUAL_SOURCE_CATEGORY[group]
    qstat, qmin = _qual_threshold(group, qcat)
    qsource = _fetch_stats_pool(cursor, qcat, peer, season)
    qualified_ids = {pid for pid, d in qsource.items() if (d.get(qstat) or 0) >= qmin}

    def _q(pool):
        return {pid: d for pid, d in pool.items() if pid in qualified_ids}

    axes = []
    for label, kind, st in EXPLORER_RADAR_SPEC[group]:
        pool = _radar_pool(cursor, kind, group, season)
        # EPA/ppa pools qualify against the same counting-stat set
        qpool = _q(pool)
        _, pct, _n = _rank_pct(player_id, qpool, st, higher_better=True)
        axes.append({'label': label, 'pct': pct})
    if all(a['pct'] is None for a in axes):
        return None
    return {
        'id': player_id, 'name': f'{first or ""} {last or ""}'.strip(),
        'team': team or '', 'pos': pos or '', 'group': group,
        'headshot': headshot, 'axes': axes,
    }


@app.route('/api/explorer/radar')
@cache.cached(timeout=21600, query_string=True)
def api_explorer_radar():
    """Percentile radar profile for a player in a given season (JSON)."""
    pid = request.args.get('pid', type=int)
    if not pid:
        return jsonify({'error': 'missing pid'}), 400
    s = request.args.get('season', type=int)
    season = s if (s and s in get_available_seasons()) else CURRENT_SEASON
    conn = get_db()
    try:
        cursor = conn.cursor()
        profile = _explorer_radar(cursor, pid, season)
    finally:
        release_db(conn)
    if not profile:
        return jsonify({'error': 'no profile'}), 404
    return jsonify(profile)


@app.route('/explorer')
@cache.cached(timeout=21600, query_string=True)
def explorer():
    """Stat Explorer: teams or players plotted by two selectable stats, with
    each team logo / player headshot as the marker. Team mode adds an optional
    bubble-size dimension; player mode adds a percentile radar. All values ship
    with the page so axis switching is instant; season/scope/position reload."""
    season = requested_season()
    scope = request.args.get('scope', 'team')
    if scope not in ('team', 'player'):
        scope = 'team'

    if scope == 'player':
        group = request.args.get('pos', 'QB').upper()
        if group not in EXPLORER_PLAYER_SPEC:
            group = 'QB'
        qualified_only = request.args.get('qual', '1') != '0'
        conn = get_db()
        try:
            cursor = conn.cursor()
            # The scatter is always scoped to a single conference — one FBS
            # position group is far too dense to plot or ship whole.
            cursor.execute('SELECT DISTINCT conference FROM teams '
                           'WHERE conference IS NOT NULL AND conference <> ALL(%s) '
                           'ORDER BY conference', (list(FCS_CONFS),))
            all_confs = [r[0] for r in cursor.fetchall()]
            conf = request.args.get('conf', '')
            if conf not in all_confs:
                conf = 'SEC' if 'SEC' in all_confs else (all_confs[0] if all_confs else '')
            players_data = _explorer_player_scope(cursor, group, season, qualified_only, conf)
        finally:
            release_db(conn)
        axes_meta = [{'key': k, 'label': l, 'invert': inv, 'dp': dp, 'unit': u}
                     for k, l, _c, _s, inv, dp, u in EXPLORER_PLAYER_SPEC[group]['axes']]
        groups_meta = [{'key': g, 'label': EXPLORER_PLAYER_SPEC[g]['label']}
                       for g in EXPLORER_PLAYER_SPEC]
        return render_template('explorer.html', scope='player',
                               players_data=players_data, player_axes=axes_meta,
                               player_group=group, player_groups=groups_meta,
                               player_confs=all_confs, player_conf=conf,
                               qualified_only=qualified_only,
                               season=season, available_seasons=get_available_seasons())

    conn = get_db()
    try:
        cursor = conn.cursor()
        # savant_ratings is the FBS roll call for a season (computed for every
        # team, every loaded year), so it anchors the join.
        cursor.execute('''
            SELECT sv.team, t.logo_dark, t.conference,
                   sv.net_rating, sv.off_rating, sv.def_rating,
                   sp.rating, sp.offense_rating, sp.defense_rating, sp.special_teams_rating,
                   ta.off_ppa, ta.def_ppa, ta.off_success_rate, ta.def_success_rate,
                   ta.off_explosiveness, ta.def_havoc_total,
                   pg.ppg_for, pg.ppg_against,
                   sv.sos, ta.off_scoring_opps, tst.off_plays
            FROM savant_ratings sv
            JOIN teams t ON t.name = sv.team
            LEFT JOIN sp_ratings sp ON sp.team = sv.team AND sp.season = sv.season
            LEFT JOIN team_advanced ta ON ta.team = sv.team AND ta.season = sv.season
            LEFT JOIN team_stats tst ON tst.team = sv.team AND tst.season = sv.season
            LEFT JOIN (
                SELECT tm, AVG(pf)::float AS ppg_for, AVG(pa)::float AS ppg_against
                FROM (
                    SELECT home_team AS tm, home_points AS pf, away_points AS pa
                    FROM games WHERE season = %s AND completed = 1
                    UNION ALL
                    SELECT away_team, away_points, home_points
                    FROM games WHERE season = %s AND completed = 1
                ) g GROUP BY tm
            ) pg ON pg.tm = sv.team
            WHERE sv.season = %s
              -- savant covers FCS opponents that show up in drives data;
              -- SP+ is FBS-only per season, so it is the realignment-correct
              -- FBS filter (an FCS-era Sam Houston stays out of 2019)
              AND sp.rating IS NOT NULL
        ''', (season, season, season))

        def _r(v, nd=2):
            return round(v, nd) if v is not None else None

        teams_data = []
        for (name, logo, conf, net_sv, off_sv, def_sv,
             sp_all, sp_off, sp_def, sp_st,
             off_ppa, def_ppa, off_sr, def_sr, off_expl, havoc,
             ppg_for, ppg_against, sos, scoring_opps, plays) in cursor.fetchall():
            teams_data.append({
                'name': name, 'slug': slugify_team(name),
                'url': team_url(name, season),
                'logo': logo, 'conf': conf,
                'stats': {
                    'net_savant': _r(net_sv), 'off_savant': _r(off_sv), 'def_savant': _r(def_sv),
                    'sp_overall': _r(sp_all), 'sp_off': _r(sp_off),
                    'sp_def': _r(sp_def), 'sp_st': _r(sp_st),
                    'off_ppa': _r(off_ppa, 3), 'def_ppa': _r(def_ppa, 3),
                    'off_sr': _r(off_sr * 100, 1) if off_sr is not None else None,
                    'def_sr': _r(def_sr * 100, 1) if def_sr is not None else None,
                    'off_expl': _r(off_expl, 3),
                    'def_havoc': _r(havoc * 100, 1) if havoc is not None else None,
                    'ppg_for': _r(ppg_for, 1), 'ppg_against': _r(ppg_against, 1),
                },
                # Bubble-size dimensions (optional third axis)
                'size': {
                    'sos': _r(sos, 2), 'scoring_opps': _r(scoring_opps, 1),
                    'plays': _r(plays, 0),
                },
            })
    finally:
        release_db(conn)
    return render_template('explorer.html', scope='team', teams_data=teams_data, season=season,
                           available_seasons=get_available_seasons())


@app.route('/sitemap.xml')
@cache.cached(timeout=86400)  # regenerated daily
def sitemap():
    """XML sitemap of every indexable page, for search-engine discovery."""
    base = f"https://{request.host}"
    paths = ['/', '/teams', '/rankings', '/leaderboards', '/leaderboards/teams',
             '/bracket', '/compare', '/transfers', '/rivalries', '/savant-rating',
             '/explorer']
    for cat in ('passing', 'rushing', 'receiving', 'defense'):
        paths.append(f'/leaderboards/{cat}')
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT slug FROM teams WHERE slug IS NOT NULL ORDER BY slug')
        paths += [f'/team/{r[0]}' for r in cur.fetchall()]
        # Current season's games and current-roster players only. The
        # historical expansion made every past player/game a real page, but
        # advertising all ~60k of them sent crawlers into a sustained frenzy
        # that saturated the instance — historical pages stay reachable
        # through internal links; the sitemap sticks to the fresh content.
        cur.execute('SELECT id FROM games WHERE completed = 1 AND season = %s', (CURRENT_SEASON,))
        paths += [f'/game/{r[0]}' for r in cur.fetchall()]
        cur.execute('SELECT id FROM players WHERE active_2026 = 1 ORDER BY id')
        paths += [f'/player/{r[0]}' for r in cur.fetchall()]
    finally:
        release_db(conn)
    from xml.sax.saxutils import escape
    body = ['<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    body += [f'<url><loc>{escape(base + p)}</loc></url>' for p in paths]
    body.append('</urlset>')
    return Response('\n'.join(body), mimetype='application/xml')


@app.route('/robots.txt')
def robots():
    # Disallow: /*? keeps crawlers off the query-string URL space (season
    # variants, leaderboard sorts/filters multiply every page ~10-50x) —
    # canonical paths stay fully crawlable.
    base = f"https://{request.host}"
    return Response(
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin/\n"
        "Disallow: /*?\n"
        "Disallow: /img-proxy\n"
        "Disallow: /api/\n"
        "Disallow: /search\n"
        "Crawl-delay: 5\n"
        f"Sitemap: {base}/sitemap.xml\n",
        mimetype='text/plain')


@app.route('/favicon.ico')
def favicon_ico():
    # Serve the multi-resolution .ico at the canonical root path crawlers probe
    # (Google, bots, older browsers), even though the <head> also links the
    # higher-res favicon-*.png. Same white-mark-on-navy brand icon, cached hard.
    return send_from_directory(
        os.path.join(app.root_path, 'static'), 'favicon.ico',
        mimetype='image/x-icon', max_age=60 * 60 * 24 * 30)


@app.route('/admin/clear-cache')
def clear_cache():
    # Fail CLOSED: if ADMIN_KEY isn't configured, reject every request rather
    # than falling back to a guessable default (the old 'changeme' default
    # silently left the endpoint open to anyone who guessed that string).
    admin_key = os.getenv('ADMIN_KEY')
    # Accept the key via the X-Admin-Key header (preferred — stays out of
    # server/proxy access logs) or the ?key= query param (kept for
    # compatibility). Constant-time compare avoids leaking it via timing.
    supplied = request.headers.get('X-Admin-Key') or request.args.get('key', '')
    if not admin_key or not hmac.compare_digest(supplied, admin_key):
        return 'Unauthorized', 401
    cache.clear()
    return 'Cache cleared', 200


def _warm_cache():
    import threading, time as _t
    def run():
        _t.sleep(3)   # let the worker finish booting
        paths = ['/', '/games', '/leaderboards/passing', '/leaderboards/rushing',
                 '/leaderboards/receiving', '/leaderboards/defense',
                 '/leaderboards/teams', '/rankings', '/teams', '/savant-rating', '/bracket']
        try:
            with app.test_client() as c:
                for p in paths:
                    try:
                        c.get(p)
                    except Exception:
                        pass
            print('cache warmup complete', flush=True)
        except Exception as e:
            print(f'cache warmup skipped: {e}', flush=True)
    threading.Thread(target=run, daemon=True).start()

_warm_cache()

if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5001))
    app.run(debug=True, host='0.0.0.0', port=port)