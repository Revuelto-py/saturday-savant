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
from flask import Flask, render_template, request, jsonify, Response, redirect
from flask_caching import Cache
from collections import OrderedDict

load_dotenv()

app = Flask(__name__)

cache = Cache(app, config={
    'CACHE_TYPE': 'SimpleCache',       # in-memory, no Redis needed
    'CACHE_DEFAULT_TIMEOUT': 3600,     # 1 hour default TTL
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
        ''')
        conn.commit()
    except Exception as e:
        print(f"Index setup skipped/failed (non-fatal): {e}")
        conn.rollback()
    finally:
        release_db(conn)

ensure_indexes()

def get_ap_rankings(cursor):
    cursor.execute('SELECT team, rank FROM ap_rankings ORDER BY rank')
    return {row[0]: row[1] for row in cursor.fetchall()}

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

def _fetch_stats_pool(cursor, category, positions):
    """One query: all stat_types for all players at these positions."""
    ph = ','.join(['%s'] * len(positions))
    cursor.execute(f'''
        SELECT ps.player_id, ps.stat_type, CAST(ps.stat AS REAL)
        FROM player_stats ps
        JOIN players pl ON ps.player_id = pl.id::text
        WHERE ps.category=%s AND pl.position IN ({ph}) AND ps.stat IS NOT NULL
    ''', [category] + list(positions))
    pool = {}
    for pid, st, val in cursor.fetchall():
        pool.setdefault(pid, {})[st] = val
    return pool

def _fetch_ppa_pool(cursor, positions):
    """One query: all PPA columns for all players at these positions."""
    ph = ','.join(['%s'] * len(positions))
    cursor.execute(f'''
        SELECT pp.player_id, pp.avg_ppa_all, pp.avg_ppa_pass, pp.avg_ppa_rush, pp.total_ppa
        FROM player_ppa pp
        JOIN players pl ON pp.player_id = pl.id::text
        WHERE pl.position IN ({ph})
    ''', list(positions))
    pool = {}
    for pid, *vals in cursor.fetchall():
        pool[pid] = dict(zip(('avg_ppa_all','avg_ppa_pass','avg_ppa_rush','total_ppa'), vals))
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
    percentile = max(1, min(99, round((below / n) * 100)))
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

def compute_rank_and_percentile(cursor, player_id, stat_type, category, positions, higher_better=True):
    """Single-stat rank, filtered to the qualified peer pool (kept for any legacy call sites)."""
    pos_group = POS_GROUP_MAP.get(positions[0], positions[0])
    qual_stat, qual_min = _qual_threshold(pos_group, category)

    if category == 'ppa':
        pool = _fetch_ppa_pool(cursor, positions)
        qual_category = QUAL_SOURCE_CATEGORY.get(pos_group)
        qual_source = _fetch_stats_pool(cursor, qual_category, positions) if qual_category else pool
        pool = _qualify_pool(pool, qual_source, qual_stat, qual_min)
        return _rank_pct(player_id, pool, stat_type, higher_better)

    pool = _fetch_stats_pool(cursor, category, positions)
    pool = _qualify_pool(pool, pool, qual_stat, qual_min)
    single = {pid: {stat_type: d.get(stat_type)} for pid, d in pool.items()}
    return _rank_pct(player_id, single, stat_type, higher_better)


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

    Layered on top of the above is EA Sports College Football 27 overall
    rating (fetch_ea_ratings.py) as a SECONDARY, internal-only signal. It is a
    subjective developer rating rather than production, so it supplements
    rather than replaces the statistical signals: a small tiebreaker for
    skill/defense (breaking near-ties and ranking transfers with no usage/
    box-score yet) and the primary talent signal for the OL group, where no
    production data exists at all. Exact weighting is documented inline below.
    EA ratings are never surfaced in the UI (licensed/proprietary data).
    """
    ids = [str(p[4]) for p in roster if p[4] is not None]
    int_ids = [int(p[4]) for p in roster if p[4] is not None]
    if not ids:
        return {}

    usage = {}
    cursor.execute('SELECT player_id, overall FROM player_usage WHERE player_id = ANY(%s)', (int_ids,))
    for pid, overall in cursor.fetchall():
        usage[str(pid)] = float(overall or 0)

    dstat = {}
    cursor.execute('''
        SELECT player_id, stat_type, MAX(CAST(stat AS REAL))
        FROM player_stats
        WHERE player_id = ANY(%s) AND category IN ('defensive','interceptions')
        GROUP BY player_id, stat_type
    ''', (ids,))
    for pid, st, val in cursor.fetchall():
        dstat.setdefault(pid, {})[st] = val or 0

    yds = {}
    cursor.execute('''
        SELECT player_id, SUM(CAST(stat AS REAL))
        FROM player_stats
        WHERE player_id = ANY(%s)
          AND category IN ('passing','rushing','receiving') AND stat_type = 'YDS'
        GROUP BY player_id
    ''', (ids,))
    for pid, total in cursor.fetchall():
        yds[str(pid)] = float(total or 0)

    # EA Sports College Football 27 overall rating, matched to our players by
    # name+team at ingest (see fetch_ea_ratings.py). This is a SECONDARY,
    # internal-only signal — a subjective game-developer talent rating, not
    # on-field production — so it is weighted to *supplement* the statistical
    # signals above, never replace them. It is intentionally never displayed
    # in the UI (licensed/proprietary data). Table may not exist on a fresh
    # DB, so a missing table degrades gracefully to production-only scoring.
    ea = {}
    if use_ea:
        try:
            cursor.execute('SELECT player_id, overall FROM ea_ratings '
                           'WHERE player_id = ANY(%s) AND overall IS NOT NULL', (int_ids,))
            for pid, ovr in cursor.fetchall():
                ea[str(pid)] = float(ovr)
        except Exception:
            cursor.connection.rollback()  # ea_ratings not populated yet

    # How EA overall is combined with the existing production signals, by
    # group. Because build_lineup() sorts each position pool independently,
    # these weights only need to order players *within* a position group.
    #
    #   • skill (QB/RB/WR/TE): usage/yards stay PRIMARY. EA is a bounded
    #     tiebreaker at 0.3 — its max effect (~30) is under a 3%-usage gap
    #     (30 = 0.03*1000), so it only reorders players who are near-tied on
    #     usage or have no usage row at all (e.g. early-season transfers),
    #     never overturning a clear statistical leader.
    #   • defense: tackle-based production stays PRIMARY. EA at 0.25 (max ~25)
    #     likewise only breaks near-ties or ranks rotational players/transfers
    #     with little box-score production.
    #   • offensive line: no individual OL production exists in any data
    #     source, so here EA is the PRIMARY talent signal, blended 70/30 with
    #     class-year seniority (the previous sole proxy). A lineman with no EA
    #     rating is treated as replacement-level talent (EA_OL_PRIOR ≈ the
    #     25th-percentile FBS OL rating), NOT as his seniority-implied value —
    #     otherwise an unrated senior would leapfrog genuinely EA-rated
    #     starters. Within an all-unrated OL group every player gets the same
    #     prior, so ordering collapses back to seniority (no regression).
    EA_SKILL_W, EA_DEF_W, EA_OL_W = 0.3, 0.25, 0.7
    EA_OL_PRIOR = 68

    year_rank = {'4': 4, '3': 3, '2': 2, '1': 1}
    scores = {}
    for p in roster:
        if p[4] is None:
            continue
        pid = str(p[4])
        pos = (p[2] or '').upper()
        ovr = ea.get(pid)
        if pos in _LINEUP_OL:
            yr = year_rank.get(str(p[8]), 0)
            jersey = int(p[3]) if str(p[3]).isdigit() else 99
            senior_100 = yr / 4.0 * 100          # freshman 25 … senior 100
            ea_100 = ovr if ovr is not None else EA_OL_PRIOR
            scores[pid] = (EA_OL_W * ea_100 + (1 - EA_OL_W) * senior_100
                           + (99 - min(jersey, 99)) * 0.001)  # jersey: final tiebreak
        elif pos in _LINEUP_SKILL:
            scores[pid] = (usage.get(pid, 0) * 1000 + yds.get(pid, 0) / 1000.0
                           + (ovr or 0) * EA_SKILL_W)
        else:  # defense
            d = dstat.get(pid, {})
            scores[pid] = (d.get('TOT', 0) + 2 * d.get('SACKS', 0) + 1.5 * d.get('TFL', 0)
                           + 1.5 * d.get('PD', 0) + 3 * d.get('INT', 0) + 0.5 * d.get('QB HUR', 0)
                           + (ovr or 0) * EA_DEF_W)
    return scores


def build_lineup(roster, starter_scores=None):
    """Slot the highest-scoring available player into each formation spot.
    `starter_scores` comes from compute_starter_scores(); absent, everyone
    scores 0 and slots fall back to roster order."""
    if starter_scores is None:
        starter_scores = {}
    # Slot -> eligible positions, most-specific first so dedicated players
    # claim their natural spot before generic pools (DL, DB) fill the gaps.
    slot_positions = {
        'QB': ['QB'], 'RB': ['RB','HB','FB','APB','ATH'],
        'WR1': ['WR'], 'WR2': ['WR'], 'TE': ['TE'],
        'LT': ['OT','LT','OL','OG','G'], 'LG': ['OG','G','LG','OL','OT'],
        'C':  ['C','OL','OG','G'], 'RG': ['OG','G','RG','OL','OT'], 'RT': ['OT','RT','OL','OG','G'],
        'DE1': ['DE','EDGE','DL'], 'DE2': ['DE','EDGE','DL'],
        'DT1': ['DT','NT','DL'], 'DT2': ['DT','NT','DL'],
        'LB1': ['LB','ILB','MLB','OLB'], 'LB2': ['LB','ILB','MLB','OLB'], 'LB3': ['LB','ILB','MLB','OLB'],
        'CB1': ['CB','DB'], 'CB2': ['CB','DB'],
        'S1': ['S','SS','FS','SAF','DB'], 'S2': ['S','SS','FS','SAF','DB'],
    }
    pos_pool = {}
    for player in roster:
        first, last, pos, jersey, pid, headshot = player[0], player[1], player[2], player[3], player[4], player[5]
        if not pos: continue
        pos_pool.setdefault(pos.upper(), []).append({
            'idx': pid, 'name': last, 'first': first, 'jersey': jersey or '',
            'pos': pos, 'headshot': headshot, 'score': starter_scores.get(str(pid), 0)})
    for pool in pos_pool.values():
        pool.sort(key=lambda x: x['score'], reverse=True)

    lineup, used = {}, set()
    # Explicit fill order: skill first, then OL, then dedicated D before generic pools
    fill_order = ['QB','RB','WR1','WR2','TE','C','LT','RT','LG','RG',
                  'DE1','DE2','DT1','DT2','LB1','LB2','LB3','CB1','CB2','S1','S2']
    for slot in fill_order:
        for pos_type in slot_positions[slot]:
            for player in pos_pool.get(pos_type, []):
                if player['idx'] not in used:
                    lineup[slot] = player
                    used.add(player['idx'])
                    break
            if slot in lineup: break
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

def sort_players(cat_dict, sort_key, min_val=0):
    players = []
    for name, stats in cat_dict.items():
        val = float(stats.get(sort_key, 0) or 0)
        if val > min_val:
            players.append({'name': name, **stats})
    return sorted(players, key=lambda x: float(x.get(sort_key, 0) or 0), reverse=True)

# FCS conferences excluded from FBS-only pages. Includes the CFBD labels used
# by the FCS opponents added for logo display in fetch_fcs_logos.py — notably
# 'Southern' (SoCon), 'Big South-OVC' and 'UAC', which must be listed here so
# those teams don't leak onto the Teams grid / Rankings / Leaderboards.
FCS_CONFS = ('CAA','Big Sky','MVFC','SWAC','MEAC','Southland','Big South','OVC',
             'Big South-OVC','Southern','UAC','Patriot','NEC','Pioneer','Ivy','FCS Independents')

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

def leaders_query(cursor, category, stat_type, limit=5):
    cursor.execute(f'''
        SELECT ps.player_name, ps.team,
            CAST(MAX(CASE WHEN ps.stat_type = '{stat_type}' THEN ps.stat END) AS INTEGER) as val,
            MAX(p.headshot) as headshot, MAX(t.logo_dark) as logo_dark, MAX(p.id) as player_id
        FROM player_stats ps
        INNER JOIN teams t ON ps.team = t.name
        LEFT JOIN players p ON ps.player_id = p.id::text
        WHERE ps.category = '{category}'
        AND ps.conference NOT IN {FCS_CONFS}
        GROUP BY ps.player_name, ps.team
        ORDER BY val DESC
        LIMIT {limit}
    ''')
    return cursor.fetchall()

@cache.memoize(timeout=3600)
def get_cached_season_leaders():
    """Season-wide leaders are identical no matter which week the home page
    is showing, so this is memoized independently of the /week/<n>/<type>
    route — otherwise the same queries would re-run for every distinct
    week URL instead of being computed once per hour.

    Returns (label, leaderboard href, accent color, rows) per category, in
    the display order used by the home page sidebar."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        return [
            ('Passing Yards',   '/leaderboards/passing',   '#fbbf24', leaders_query(cursor, 'passing',       'YDS')),
            ('Rushing Yards',   '/leaderboards/rushing',   '#60a5fa', leaders_query(cursor, 'rushing',       'YDS')),
            ('Receiving Yards', '/leaderboards/receiving', '#34d399', leaders_query(cursor, 'receiving',     'YDS')),
            ('Tackles',         '/leaderboards/defense',   '#a78bfa', leaders_query(cursor, 'defensive',     'TOT')),
            ('Interceptions',   '/leaderboards/defense',   '#f87171', leaders_query(cursor, 'interceptions', 'INT')),
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

@cache.memoize(timeout=3600)
def get_ticker_data():
    """Sitewide scores ticker under the navbar: the most recent completed
    week, with postseason outranking regular season so the offseason shows
    playoff/bowl results instead of the last regular-season week."""
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT week, season_type FROM games
            WHERE completed = 1 AND season = 2025
            ORDER BY CASE WHEN season_type = 'SeasonType.POSTSEASON' THEN 0 ELSE 1 END,
                     week DESC
            LIMIT 1
        ''')
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
            WHERE g.completed = 1 AND g.season = 2025 AND g.week = %s AND g.season_type = %s
            ORDER BY CASE WHEN g.notes LIKE '%%National Championship%%' THEN 1
                          WHEN g.notes LIKE '%%Semifinal%%' THEN 2
                          WHEN g.notes LIKE '%%Quarterfinal%%' THEN 3
                          WHEN g.notes LIKE '%%First Round%%' THEN 4
                          WHEN g.notes LIKE '%%Conference Championship%%' THEN 5
                          ELSE 6 END, g.notes, g.id
        ''', (week, stype))
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

@app.route('/')
@app.route('/week/<int:week>/<season_type>')
def home(week=None, season_type='regular'):
    conn = get_db()
    try:
        cursor = conn.cursor()
        ap_rankings = get_ap_rankings(cursor)

        cursor.execute('''
            SELECT week, season_type FROM (
                SELECT DISTINCT week, season_type,
                    CASE WHEN season_type = 'SeasonType.POSTSEASON' THEN 0 ELSE 1 END as sort_order
                FROM games WHERE completed = 1 AND season = 2025
            ) sub
            ORDER BY sort_order, week DESC
        ''')
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

        cursor.execute('''
            SELECT g.home_team, g.home_points, g.away_team, g.away_points,
                   g.week, g.season_type, g.notes, t1.logo, t2.logo,
                   t1.logo_dark, t2.logo_dark, g.id
            FROM games g
            LEFT JOIN teams t1 ON g.home_team = t1.name
            LEFT JOIN teams t2 ON g.away_team = t2.name
            WHERE g.completed = 1 AND g.season = 2025 AND g.week = %s AND g.season_type = %s
            ORDER BY CASE WHEN g.notes LIKE '%%National Championship%%' THEN 1
                          WHEN g.notes LIKE '%%Semifinal%%' THEN 2
                          WHEN g.notes LIKE '%%Quarterfinal%%' THEN 3
                          WHEN g.notes LIKE '%%First Round%%' THEN 4
                          WHEN g.notes LIKE '%%Conference Championship%%' THEN 5
                          ELSE 6 END, g.notes, g.id
        ''', (week, db_season_type))
        raw_games = cursor.fetchall()

        # Enrich each game tuple with rivalry name as last element — one query
        # for the whole rivalries table instead of one per game (N+1 fix)
        rivalry_map = get_rivalry_map(cursor)
        games = []
        for g in raw_games:
            rivalry = rivalry_map.get((g[0], g[2]), '')
            games.append(g + (rivalry,))

        label_order = ['National Championship','Semifinal','Quarterfinal','First Round','Conference Championships','Bowl Games']
        grouped_games = OrderedDict((label, []) for label in label_order)
        for game in games:
            grouped_games[get_game_label(game[6])].append(game)
        grouped_games = {k: v for k, v in grouped_games.items() if v}

        leaders = get_cached_season_leaders()

        # Live count of FBS teams for the hero pill, so it stays accurate
        # through realignment instead of a hardcoded "130+".
        cursor.execute('SELECT COUNT(*) FROM teams WHERE conference NOT IN %s', (FCS_CONFS,))
        fbs_team_count = cursor.fetchone()[0]
        # Drive tracking lives on the game page (Drives tab); point the hero
        # pill at the most prominent recent game rather than an unrelated page.
        featured_game_id = games[0][11] if games else None

    finally:
        release_db(conn)
    return render_template('home.html',
        games=games, grouped_games=grouped_games, all_weeks=all_weeks,
        selected_week=week, season_type=season_type,
        leaders=leaders, ap_rankings=ap_rankings,
        fbs_team_count=fbs_team_count, featured_game_id=featured_game_id)

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
                       p.jersey, p.headshot, t.conference, t.logo_dark
                FROM players p
                INNER JOIN teams t ON p.team = t.name
                WHERE (p.first_name || ' ' || p.last_name) ILIKE %s
                   OR p.last_name ILIKE %s
                   OR p.first_name ILIKE %s
                ORDER BY p.last_name, p.first_name
                LIMIT 50
            ''', (f'%{q}%', f'%{q}%', f'%{q}%'))
            player_results = cursor.fetchall()
            cursor.execute('''
                SELECT name, conference, logo_dark, color
                FROM teams WHERE name ILIKE %s OR abbreviation ILIKE %s
                ORDER BY name LIMIT 10
            ''', (f'%{q}%', f'%{q}%'))
            team_results = cursor.fetchall()
        finally:
            release_db(conn)
    return render_template('search.html', player_results=player_results, team_results=team_results, query=q)

@app.route('/leaderboards')
@app.route('/leaderboards/<category>')
@cache.cached(timeout=3600, query_string=True)  # 1 hour — view/team/qualified are part of the query string, so each combo caches separately
def leaderboards(category='passing'):
    if category not in PLAYER_COLUMNS:
        category = 'passing'

    conn = get_db()
    try:
        cursor = conn.cursor()

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

        ap_rankings = get_ap_rankings(cursor)
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

            ppa_join   = 'LEFT JOIN player_ppa pp ON pp.player_id = p.id::text' if view == 'advanced' else ''
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
                JOIN player_stats ps ON ps.player_id = p.id::text AND ps.category = 'passing'
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

            ppa_join     = 'LEFT JOIN player_ppa pp ON pp.player_id = p.id::text' if view == 'advanced' else ''
            ppa_select   = ', pp.avg_ppa_rush as epa_rush, pp.total_ppa as total_epa' if view == 'advanced' else ''
            ppa_group    = ', pp.avg_ppa_rush, pp.total_ppa' if view == 'advanced' else ''
            usage_join   = 'LEFT JOIN player_usage pu ON pu.player_id = p.id' if view == 'advanced' else ''
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
                JOIN player_stats ps ON ps.player_id = p.id::text AND ps.category = 'rushing'
                JOIN teams t ON ps.team = t.name
                LEFT JOIN player_stats pf ON pf.player_id = p.id::text AND pf.category = 'fumbles'
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

            ppa_join   = 'LEFT JOIN player_ppa pp ON pp.player_id = p.id::text' if view == 'advanced' else ''
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
                JOIN player_stats ps ON ps.player_id = p.id::text AND ps.category = 'receiving'
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
                JOIN player_stats ps ON ps.player_id = p.id::text AND ps.category = 'defensive'
                JOIN teams t ON ps.team = t.name
                LEFT JOIN player_stats pi ON pi.player_id = p.id::text AND pi.category = 'interceptions'
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
                    FROM player_stats WHERE category='defensive' AND stat_type='TOT'
                    GROUP BY team
                ''')
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
        'mode': 'player', 'category': category, 'view': view,
        'conf': conf_filter, 'team': team_filter, 'pos': pos_filter,
        'qualified': '1' if qualified else '0', 'sort': sort_col, 'dir': sort_dir,
    }
    has_advanced = len(PLAYER_COLUMNS[category]['advanced']) > 0
    return render_template('leaderboards.html',
        mode='player', players=players, category=category, view=view,
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
@cache.cached(timeout=3600, query_string=True)  # view/team are part of the query string, so each combo caches separately
def leaderboards_teams(category='offense'):
    if category not in TEAM_CATEGORY_DEFAULTS:
        category = 'offense'

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
            LEFT JOIN team_stats ts ON ts.team = t.name
            LEFT JOIN team_advanced adv ON adv.team = t.name
            LEFT JOIN sp_ratings sp ON sp.team = t.name
            LEFT JOIN savant_ratings svr ON svr.team = t.name
            LEFT JOIN ap_rankings ar ON ar.team = t.name
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
        'mode': 'team', 'category': category, 'view': view,
        'conf': conf_filter, 'team': team_filter,
        'sort': sort_col, 'dir': sort_dir,
    }
    has_advanced = len(TEAM_COLUMNS[category]['advanced']) > 0
    return render_template('leaderboards.html',
        mode='team', teams=teams_out, category=category, view=view,
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
            LEFT JOIN ap_rankings ar ON ar.team = sr.team
            ORDER BY sr.net_ranking
            LIMIT 10
        ''')
        top10 = [{'team': r[0], 'logo': r[1], 'conf': r[2], 'net': r[3], 'off': r[4],
                  'def': r[5], 'sos': r[6], 'rank': r[7], 'ap': r[8]}
                 for r in cursor.fetchall()]
        cursor.execute('SELECT COUNT(*), SUM(drives_off), SUM(games)/2 FROM savant_ratings')
        n_teams, n_drives, n_games = cursor.fetchone()
    finally:
        release_db(conn)
    return render_template('savant_rating.html', top10=top10,
                           n_teams=n_teams, n_drives=n_drives, n_games=n_games)

@app.route('/team/<path:team_ref>')
@cache.cached(timeout=3600)  # 1 hour — stats don't change during offseason
def team(team_ref):
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

        ap_rankings = get_ap_rankings(cursor)
        team_rank = ap_rankings.get(team_name)

        cursor.execute('SELECT name, conference, abbreviation, logo, color, alt_color, logo_dark FROM teams WHERE name = %s', (team_name,))
        team_info = cursor.fetchone()
        if not team_info:
            return render_template('404.html', message=f'Team "{team_name}" not found.'), 404

        cursor.execute('''
            SELECT
                SUM(CASE WHEN (home_team=%s AND home_points>away_points) OR (away_team=%s AND away_points>home_points) THEN 1 ELSE 0 END),
                SUM(CASE WHEN (home_team=%s AND home_points<away_points) OR (away_team=%s AND away_points<home_points) THEN 1 ELSE 0 END)
            FROM games WHERE (home_team=%s OR away_team=%s) AND completed=1 AND season_type='SeasonType.REGULAR'
        ''', (team_name,)*6)
        record = cursor.fetchone()

        cursor.execute('''
            SELECT COUNT(*),
                SUM(CASE WHEN home_team=%s THEN home_points ELSE away_points END),
                SUM(CASE WHEN home_team=%s THEN away_points ELSE home_points END)
            FROM games WHERE (home_team=%s OR away_team=%s) AND completed=1 AND season_type='SeasonType.REGULAR'
        ''', (team_name, team_name, team_name, team_name))
        g = cursor.fetchone()
        games_played = g[0] or 1
        pts_for = g[1] or 0
        pts_against = g[2] or 0

        cursor.execute("SELECT SUM(stat) FROM player_stats WHERE team=%s AND category='passing' AND stat_type='YDS'", (team_name,))
        pass_yds = cursor.fetchone()[0] or 0
        cursor.execute("SELECT SUM(stat) FROM player_stats WHERE team=%s AND category='rushing' AND stat_type='YDS'", (team_name,))
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
                  FROM games WHERE completed=1 AND season_type='SeasonType.REGULAR'
                UNION ALL
                SELECT away_team AS team, away_points AS pf, home_points AS pa
                  FROM games WHERE completed=1 AND season_type='SeasonType.REGULAR'
            ) s
            JOIN teams t ON t.name = s.team AND t.conference NOT IN %s
            GROUP BY s.team
        ''', (FCS_CONFS,))
        pf_pg, pa_pg, gp_map = {}, {}, {}
        for tm, gp, pf, pa in cursor.fetchall():
            if gp:
                pf_pg[tm], pa_pg[tm], gp_map[tm] = (pf or 0) / gp, (pa or 0) / gp, gp

        cursor.execute('''
            SELECT ps.team, ps.category, SUM(ps.stat)
            FROM player_stats ps
            JOIN teams t ON t.name = ps.team AND t.conference NOT IN %s
            WHERE ps.category IN ('passing','rushing') AND ps.stat_type='YDS'
            GROUP BY ps.team, ps.category
        ''', (FCS_CONFS,))
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
                    AND g.completed=1 AND g.season_type='SeasonType.REGULAR'
                WHERE t.conference=%s
                GROUP BY t.name, t.logo, t.logo_dark ORDER BY wins DESC
            ''', (team_info[1],))
            standings = cursor.fetchall()

        cursor.execute('''
            SELECT g.id,
                CASE WHEN g.home_team=%s THEN 'home' ELSE 'away' END,
                CASE WHEN g.home_team=%s THEN g.away_team ELSE g.home_team END,
                CASE WHEN g.home_team=%s THEN t2.logo ELSE t1.logo END,
                CASE WHEN g.home_team=%s THEN g.home_points ELSE g.away_points END,
                CASE WHEN g.home_team=%s THEN g.away_points ELSE g.home_points END,
                g.week, g.season_type, g.notes,
                CASE WHEN g.home_team=%s THEN t2.logo_dark ELSE t1.logo_dark END
            FROM games g
            LEFT JOIN teams t1 ON g.home_team=t1.name
            LEFT JOIN teams t2 ON g.away_team=t2.name
            WHERE (g.home_team=%s OR g.away_team=%s) AND g.completed=1
            ORDER BY CASE WHEN g.season_type='SeasonType.REGULAR' THEN 0 ELSE 1 END, g.week
        ''', (team_name,)*8)
        raw_schedule = cursor.fetchall()
        # One query for the whole rivalries table instead of one per
        # schedule row (N+1 fix)
        rivalry_map = get_rivalry_map(cursor)
        schedule = [g + (rivalry_map.get((team_name, g[2]), ''),) for g in raw_schedule]

        cursor.execute('''
            SELECT first_name, last_name, position, jersey, id, headshot, height, weight, year
            FROM players WHERE team=%s AND active_2026=1
            ORDER BY
                CASE position
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
                    ELSE 14 END, last_name
        ''', (team_name,))
        roster = cursor.fetchall()

        # Starters are picked from real 2025 usage/production keyed on
        # player_id (transfer-aware — see compute_starter_scores), not from
        # roster order. Pass the full roster tuples so OL seniority (the
        # year column) is available to the scorer and the slotter.
        starter_scores = compute_starter_scores(cursor, roster)
        lineup = build_lineup(roster, starter_scores)

        cursor.execute('SELECT player_name, category, stat_type, stat FROM player_stats WHERE team=%s', (team_name,))
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
            WHERE ps.team = %s
        ''', (team_name,))
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

        cursor.execute('SELECT * FROM team_stats')
        all_teams_stats = cursor.fetchall()
        percentiles = compute_percentiles(all_teams_stats, team_name)

        cursor.execute('SELECT * FROM team_stats WHERE team=%s', (team_name,))
        ts = cursor.fetchone()
        team_adv = None
        if ts:
            team_adv = {
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
        cursor.execute('SELECT rating, ranking, offense_rating, offense_ranking, defense_rating, defense_ranking, special_teams_rating FROM sp_ratings WHERE team=%s', (team_name,))
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
            FROM savant_ratings WHERE team=%s
        ''', (team_name,))
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

        # Recruiting rankings trend
        cursor.execute('''
            SELECT year, rank, points FROM team_recruiting
            WHERE team=%s AND year >= 2022 ORDER BY year DESC
        ''', (team_name,))
        recruiting = [{'year': r[0], 'rank': r[1], 'points': round(r[2], 1) if r[2] else None}
                      for r in cursor.fetchall()]

        # Havoc + field position (from team_advanced) — fetch every team so we
        # can rank this team's havoc/field-position numbers into percentiles,
        # same as the team_stats-based metrics above.
        cursor.execute('SELECT * FROM team_advanced')
        adv_cols = [d[0] for d in cursor.description]
        all_teams_advanced = {row[0]: dict(zip(adv_cols, row)) for row in cursor.fetchall()}
        adv_row = all_teams_advanced.get(team_name)
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
        percentiles.update(compute_havoc_field_pos_percentiles(all_teams_advanced, team_name))

        conf_logo = get_conference_logos(cursor).get(team_info[1])

        return render_template('team.html',
                team=team_info, record=record, season_stats=season_stats,
                hero_ranks=hero_ranks,
                standings=standings, schedule=schedule, roster=roster, lineup=lineup,
                passing_stats=passing_stats, rushing_stats=rushing_stats,
                receiving_stats=receiving_stats, defensive_stats=defensive_stats,
                kicking_stats=kicking_stats, punting_stats=punting_stats,
                kick_return_stats=kick_return_stats, punt_return_stats=punt_return_stats,
                team_adv=team_adv, percentiles=percentiles, sp=sp, svr=svr,
                ap_rankings=ap_rankings, team_rank=team_rank,
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
                WHERE name ILIKE %s OR abbreviation ILIKE %s
                ORDER BY name
                LIMIT 4
            ''', (f'%{q}%', f'%{q}%'))
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
@cache.cached(timeout=3600)  # 1 hour — AP rankings update weekly during season
def rankings():
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT a.rank, a.team, a.points, a.first_place_votes, a.week,
                   t.logo, t.conference, t.color,
                   sp.rating, sp.ranking as sp_rank,
                   SUM(CASE WHEN (g.home_team=a.team AND g.home_points>g.away_points) OR (g.away_team=a.team AND g.away_points>g.home_points) THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN (g.home_team=a.team AND g.home_points<g.away_points) OR (g.away_team=a.team AND g.away_points<g.home_points) THEN 1 ELSE 0 END) as losses,
                   a.prev_rank, t.logo_dark, t.alt_color
            FROM ap_rankings a
            LEFT JOIN teams t ON a.team = t.name
            LEFT JOIN sp_ratings sp ON a.team = sp.team
            LEFT JOIN games g ON (g.home_team=a.team OR g.away_team=a.team)
                AND g.completed=1 AND g.season_type='SeasonType.REGULAR'
            GROUP BY a.rank, a.team, a.points, a.first_place_votes, a.week, a.prev_rank,
                     t.logo, t.conference, t.color, t.logo_dark, t.alt_color,
                     sp.rating, sp.ranking
            ORDER BY a.rank
        ''')
        rows = cursor.fetchall()
        cursor.execute('SELECT week, season, season_type FROM ap_rankings LIMIT 1')
        meta = cursor.fetchone()
    finally:
        release_db(conn)
    return render_template('rankings.html', rankings=rows, meta=meta)


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
def game_detail(game_id):
    conn = get_db()
    try:
        cursor = conn.cursor()

        cursor.execute('''
            SELECT g.id, g.home_team, g.away_team, g.home_points, g.away_points,
                   g.week, g.season_type, g.notes, g.start_date,
                   t1.logo_dark, t2.logo_dark, t1.color, t2.color,
                   t1.alt_color, t2.alt_color
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
        ap_rankings = get_ap_rankings(cursor)
        rivalry_name = get_rivalry(cursor, home_team, away_team)

        def _record(team):
            cursor.execute('''
                SELECT
                    SUM(CASE WHEN (home_team=%s AND home_points>away_points) OR (away_team=%s AND away_points>home_points) THEN 1 ELSE 0 END),
                    SUM(CASE WHEN (home_team=%s AND home_points<away_points) OR (away_team=%s AND away_points<home_points) THEN 1 ELSE 0 END)
                FROM games
                WHERE (home_team=%s OR away_team=%s) AND home_points IS NOT NULL AND away_points IS NOT NULL
                  AND id <= %s
            ''', (team, team, team, team, team, team, game_id))
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
        summary_row = None
        try:
            cursor.execute('SELECT summary_gz FROM game_summaries WHERE game_id = %s', (game_id,))
            summary_row = cursor.fetchone()
        except Exception:
            conn.rollback()  # table not created yet — fall back to live fetch
    finally:
        release_db(conn)

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

                is_scoring_drive = drive_result in ['TD', 'FG']
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
                # start_yl_abs / pos use a single 0-100 scale across the WHOLE
                # field where 0 = the away team's own goal line (left endzone)
                # and 100 = the home team's own goal line (right endzone) —
                # matching the away=left/home=right convention already used in
                # the score hero and quarter table elsewhere on this page.
                # Whichever team is on offense drives toward the OPPONENT's
                # side: away moves toward 100, home moves toward 0.
                start_yl_abs = yl if play_side == 'away' else (100 - yl)
                direction = 1 if play_side == 'away' else -1
                pos = float(start_yl_abs)

                drive_play_list = []
                for p in (drive.get('plays') or []):
                    ptype = (p.get('type') or {}).get('text', '')
                    classified = _classify_play(ptype)
                    if classified is None:
                        continue  # kickoff/timeout/period marker — not a scrimmage snap
                    label, color, is_turnover = classified

                    # Bug fix: the field that actually holds yards gained on
                    # THIS play is statYardage. statYards (what this route
                    # used to read) is always null in the live ESPN response,
                    # which is why every play used to collapse to a 0-yard
                    # bar stacked at the same starting position instead of
                    # progressing across the field.
                    yards = int(p.get('statYardage', 0) or 0)

                    new_pos = pos + direction * yards
                    start_pct = max(0.0, min(100.0, min(pos, new_pos)))
                    end_pct   = max(0.0, min(100.0, max(pos, new_pos)))
                    # Clamp the running tracker itself (not just the display
                    # values) — ESPN occasionally attributes extra yardage to
                    # a play right at the goal line (e.g. a two-point try
                    # folded into the same drive's play list), which would
                    # otherwise push every later play in this drive out of
                    # [0, 100] too.
                    pos = max(0.0, min(100.0, new_pos))

                    p_start = p.get('start') or {}
                    down, distance = p_start.get('down'), p_start.get('distance')
                    down_map = {1: '1st', 2: '2nd', 3: '3rd', 4: '4th'}
                    down_dist = f"{down_map.get(down, '')} & {distance}" if down and distance is not None else ''
                    play_clock = (p.get('clock') or {}).get('displayValue', '')
                    play_period = (p.get('period') or {}).get('number') or quarter
                    tooltip_bits = [b for b in [
                        down_dist,
                        f"{label} — {yards:+d} yds" if yards else f"{label} — 0 yds",
                        f"Q{play_period} · {play_clock}" if play_clock else f"Q{play_period}",
                    ] if b]

                    drive_play_list.append({
                        'label':       label,
                        'color':       color,
                        'start_pct':   round(start_pct, 2),
                        'width_pct':   round(end_pct - start_pct, 2),
                        'yards':       yards,
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

                badge_label, badge_bg, badge_color = _classify_drive_result(drive_result)

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

    return render_template('game.html',
        game=game_info,
        home_team=home_team,
        away_team=away_team,
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


@app.route('/player/<int:player_id>')
@cache.memoize(timeout=3600)
def player_detail(player_id):
    conn = get_db()
    try:
        cursor = conn.cursor()

        cursor.execute('''
            SELECT p.id, p.first_name, p.last_name, p.team, p.position, p.jersey,
                   p.headshot, p.height, p.weight, p.year,
                   t.logo_dark, t.color, t.alt_color, t.conference,
                   p.active_2026, p.draft_status,
                   p.nfl_status, p.nfl_team, p.nfl_team_logo,
                   p.draft_year, p.draft_round, p.draft_pick
            FROM players p
            LEFT JOIN teams t ON p.team = t.name
            WHERE p.id = %s
        ''', (player_id,))
        row = cursor.fetchone()
        if not row:
            return render_template('404.html', message='Player not found.'), 404

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
            'year_fmt':   year_map.get(year_raw, ''),
            'nfl_status':    row[16],
            'nfl_team':      row[17],
            'nfl_team_logo': row[18],
            'draft_year':    row[19],
            'draft_round':   row[20],
            'draft_pick':    row[21],
        }

        # ── Transfer history + stats/current-team mismatch detection ──────────
        # Matched by name since transfers has no player_id foreign key.
        cursor.execute('''
            SELECT origin, destination, transfer_date, year, stars, rating
            FROM transfers
            WHERE LOWER(first_name) = LOWER(%s) AND LOWER(last_name) = LOWER(%s)
            ORDER BY year ASC, transfer_date ASC
        ''', (player['first_name'], player['last_name']))
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

        cursor.execute('SELECT category, stat_type, stat FROM player_stats WHERE player_id = %s', (str(player_id),))
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
            FROM player_ppa WHERE player_id = %s
        ''', (str(player_id),))
        ppa_row = cursor.fetchone()
        ppa = None
        if ppa_row:
            ppa = {
                'avg_all':  round(ppa_row[0], 3) if ppa_row[0] is not None else None,
                'avg_pass': round(ppa_row[1], 3) if ppa_row[1] is not None else None,
                'avg_rush': round(ppa_row[2], 3) if ppa_row[2] is not None else None,
                'total':    round(ppa_row[3], 1)  if ppa_row[3] is not None else None,
            }

        cursor.execute('SELECT rank FROM ap_rankings WHERE team=%s ORDER BY week DESC LIMIT 1', (player['team'],))
        ap_row = cursor.fetchone()
        ap_rank = ap_row[0] if ap_row else None

        # ── NATIONAL RANKS + PERCENTILES (unified identical pool) ─────────────────
        national_ranks     = {}
        player_percentiles = {}
        try:
            pos = player.get('position') or ''

            _pos_groups = {
                'QB':   ('QB',  ['QB']),
                'RB':   ('RB',  ['RB','HB','FB']),
                'HB':   ('RB',  ['RB','HB','FB']),
                'FB':   ('RB',  ['RB','HB','FB']),
                'WR':   ('WR',  ['WR','TE']),
                'TE':   ('TE',  ['WR','TE']),
                'DE':   ('DL',  ['DE','DT','NT','DL','EDGE']),
                'DT':   ('DL',  ['DE','DT','NT','DL','EDGE']),
                'NT':   ('DL',  ['DE','DT','NT','DL','EDGE']),
                'DL':   ('DL',  ['DE','DT','NT','DL','EDGE']),
                'EDGE': ('DL',  ['DE','DT','NT','DL','EDGE']),
                'LB':   ('LB',  ['LB','ILB','OLB','MLB']),
                'ILB':  ('LB',  ['LB','ILB','OLB','MLB']),
                'OLB':  ('LB',  ['LB','ILB','OLB','MLB']),
                'MLB':  ('LB',  ['LB','ILB','OLB','MLB']),
                'CB':   ('DB',  ['CB','S','SS','FS','SAF','DB']),
                'S':    ('DB',  ['CB','S','SS','FS','SAF','DB']),
                'SS':   ('DB',  ['CB','S','SS','FS','SAF','DB']),
                'FS':   ('DB',  ['CB','S','SS','FS','SAF','DB']),
                'SAF':  ('DB',  ['CB','S','SS','FS','SAF','DB']),
                'DB':   ('DB',  ['CB','S','SS','FS','SAF','DB']),
            }

            if pos in _pos_groups:
                group_name, gp = _pos_groups[pos]
                pool_size = 0

                if pos == 'QB':
                    sp = _fetch_stats_pool(cursor, 'passing', gp)
                    pp = _fetch_ppa_pool(cursor, gp)
                    pass_stat, pass_min = _qual_threshold(group_name, 'passing')
                    ppa_stat,  ppa_min  = _qual_threshold(group_name, 'ppa')
                    sp = _qualify_pool(sp, sp, pass_stat, pass_min)
                    pp = _qualify_pool(pp, sp, ppa_stat, ppa_min)
                    for rk, st, pk, hb in [
                        ('pass_yds_rank','YDS','pass_yards',True),
                        ('pass_td_rank', 'TD', 'pass_td',   True),
                        ('pct_rank',     'PCT','completion', True),
                        ('ypa_rank',     'YPA','yards_per_att',True),
                    ]:
                        r, p, n = _rank_pct(player_id, sp, st, hb)
                        if r is not None: national_ranks[rk] = r
                        if p is not None: player_percentiles[pk] = p
                        pool_size = max(pool_size, n)
                    r, p, _ = _rank_pct(player_id, sp, 'INT', higher_better=False)
                    if r is not None: national_ranks['int_rank'] = r
                    for col, rk, pk in [
                        ('avg_ppa_all', 'epa_rank', 'epa_per_play'),
                        ('avg_ppa_pass', None,       'epa_pass'),
                        ('avg_ppa_rush', None,        'epa_rush'),
                        ('total_ppa',   None,        'total_epa'),
                    ]:
                        r, p, _ = _rank_pct(player_id, pp, col)
                        if rk and r is not None: national_ranks[rk] = r
                        if p is not None: player_percentiles[pk] = p

                elif pos in ('RB','HB','FB'):
                    sp = _fetch_stats_pool(cursor, 'rushing', gp)
                    rp = _fetch_stats_pool(cursor, 'receiving', ['WR','TE','RB','HB','FB'])
                    pp = _fetch_ppa_pool(cursor, gp)
                    rush_stat, rush_min = _qual_threshold(group_name, 'rushing')
                    rec_stat,  rec_min  = _qual_threshold(group_name, 'receiving')
                    ppa_stat,  ppa_min  = _qual_threshold(group_name, 'ppa')
                    sp = _qualify_pool(sp, sp, rush_stat, rush_min)
                    rp = _qualify_pool(rp, rp, rec_stat, rec_min)
                    pp = _qualify_pool(pp, sp, ppa_stat, ppa_min)
                    for rk, st, pk in [
                        ('rush_yds_rank','YDS','rush_yards'),
                        ('rush_td_rank', 'TD', 'rush_td'),
                    ]:
                        r, p, n = _rank_pct(player_id, sp, st)
                        if r is not None: national_ranks[rk] = r
                        if p is not None: player_percentiles[pk] = p
                        pool_size = max(pool_size, n)
                    r, p, _ = _rank_pct(player_id, sp, 'YPC')
                    if r is not None: national_ranks['ypc_rank'] = r
                    if p is not None: player_percentiles['yards_per_carry'] = p
                    _, p, _ = _rank_pct(player_id, rp, 'YDS')
                    if p is not None: player_percentiles['rec_yards'] = p
                    for col, rk, pk in [
                        ('avg_ppa_all', 'epa_rank', 'epa_per_play'),
                        ('total_ppa',   None,       'total_epa'),
                    ]:
                        r, p, _ = _rank_pct(player_id, pp, col)
                        if rk and r is not None: national_ranks[rk] = r
                        if p is not None: player_percentiles[pk] = p

                elif pos in ('WR','TE'):
                    sp = _fetch_stats_pool(cursor, 'receiving', gp)
                    pp = _fetch_ppa_pool(cursor, gp)
                    rec_stat, rec_min = _qual_threshold(group_name, 'receiving')
                    ppa_stat, ppa_min = _qual_threshold(group_name, 'ppa')
                    sp = _qualify_pool(sp, sp, rec_stat, rec_min)
                    pp = _qualify_pool(pp, sp, ppa_stat, ppa_min)
                    for rk, st, pk in [
                        ('rec_yds_rank','YDS','rec_yards'),
                        ('rec_td_rank', 'TD', 'rec_td'),
                        ('rec_rank',    'REC','receptions'),
                        ('ypr_rank',    'AVG','yards_per_rec'),
                    ]:
                        r, p, n = _rank_pct(player_id, sp, st)
                        if r is not None: national_ranks[rk] = r
                        if p is not None: player_percentiles[pk] = p
                        pool_size = max(pool_size, n)
                    for col, rk, pk in [
                        ('avg_ppa_all', 'epa_rank', 'epa_per_play'),
                        ('total_ppa',   None,       'total_epa'),
                    ]:
                        r, p, _ = _rank_pct(player_id, pp, col)
                        if rk and r is not None: national_ranks[rk] = r
                        if p is not None: player_percentiles[pk] = p

                elif pos in ('DE','DT','NT','DL','EDGE'):
                    dl_all = ['DE','DT','NT','DL','EDGE','LB','ILB','OLB','MLB']
                    sp_wide = _fetch_stats_pool(cursor, 'defensive', dl_all)
                    sp_dl   = _fetch_stats_pool(cursor, 'defensive', gp)
                    pp = _fetch_ppa_pool(cursor, gp)
                    def_stat, def_min = _qual_threshold(group_name, 'defensive')
                    ppa_stat, ppa_min = _qual_threshold(group_name, 'ppa')
                    sp_wide = _qualify_pool(sp_wide, sp_wide, def_stat, def_min)
                    sp_dl   = _qualify_pool(sp_dl, sp_dl, def_stat, def_min)
                    pp      = _qualify_pool(pp, sp_dl, ppa_stat, ppa_min)
                    for rk, st, pk in [
                        ('tackles_rank','TOT',  'tackles'),
                        ('sacks_rank',  'SACKS','sacks'),
                    ]:
                        r, p, n = _rank_pct(player_id, sp_wide, st)
                        if r is not None: national_ranks[rk] = r
                        if p is not None: player_percentiles[pk] = p
                        pool_size = max(pool_size, n)
                    _, p, _ = _rank_pct(player_id, sp_dl, 'TFL')
                    if p is not None: player_percentiles['tfl'] = p
                    for col, rk, pk in [
                        ('avg_ppa_all', 'epa_rank', 'epa_per_play'),
                        ('total_ppa',   None,       'total_epa'),
                    ]:
                        r, p, _ = _rank_pct(player_id, pp, col)
                        if rk and r is not None: national_ranks[rk] = r
                        if p is not None: player_percentiles[pk] = p

                elif pos in ('LB','ILB','OLB','MLB'):
                    lb_all = ['DE','DT','NT','DL','EDGE','LB','ILB','OLB','MLB']
                    sp_wide = _fetch_stats_pool(cursor, 'defensive', lb_all)
                    sp_lb   = _fetch_stats_pool(cursor, 'defensive', gp)
                    pp = _fetch_ppa_pool(cursor, gp)
                    def_stat, def_min = _qual_threshold(group_name, 'defensive')
                    ppa_stat, ppa_min = _qual_threshold(group_name, 'ppa')
                    sp_wide = _qualify_pool(sp_wide, sp_wide, def_stat, def_min)
                    sp_lb   = _qualify_pool(sp_lb, sp_lb, def_stat, def_min)
                    pp      = _qualify_pool(pp, sp_lb, ppa_stat, ppa_min)
                    for rk, st, pk in [
                        ('tackles_rank','TOT',  'tackles'),
                        ('sacks_rank',  'SACKS','sacks'),
                    ]:
                        r, p, n = _rank_pct(player_id, sp_wide, st)
                        if r is not None: national_ranks[rk] = r
                        if p is not None: player_percentiles[pk] = p
                        pool_size = max(pool_size, n)
                    _, p, _ = _rank_pct(player_id, sp_lb, 'TFL')
                    if p is not None: player_percentiles['tfl'] = p
                    for col, rk, pk in [
                        ('avg_ppa_all', 'epa_rank', 'epa_per_play'),
                        ('total_ppa',   None,       'total_epa'),
                    ]:
                        r, p, _ = _rank_pct(player_id, pp, col)
                        if rk and r is not None: national_ranks[rk] = r
                        if p is not None: player_percentiles[pk] = p

                elif pos in ('CB','S','SS','FS','SAF','DB'):
                    sp = _fetch_stats_pool(cursor, 'defensive', gp)
                    pp = _fetch_ppa_pool(cursor, gp)
                    def_stat, def_min = _qual_threshold(group_name, 'defensive')
                    ppa_stat, ppa_min = _qual_threshold(group_name, 'ppa')
                    sp = _qualify_pool(sp, sp, def_stat, def_min)
                    pp = _qualify_pool(pp, sp, ppa_stat, ppa_min)
                    r, p, n = _rank_pct(player_id, sp, 'TOT')
                    if r is not None: national_ranks['tackles_rank'] = r
                    if p is not None: player_percentiles['tackles'] = p
                    pool_size = n
                    _, p, _ = _rank_pct(player_id, sp, 'INT')
                    if p is not None: player_percentiles['interceptions'] = p
                    _, p, _ = _rank_pct(player_id, sp, 'PD')
                    if p is not None: player_percentiles['pd'] = p
                    for col, rk, pk in [
                        ('avg_ppa_all', 'epa_rank', 'epa_per_play'),
                        ('total_ppa',   None,       'total_epa'),
                    ]:
                        r, p, _ = _rank_pct(player_id, pp, col)
                        if rk and r is not None: national_ranks[rk] = r
                        if p is not None: player_percentiles[pk] = p

                player_percentiles['group']      = group_name
                player_percentiles['peer_count'] = pool_size

        except Exception as e:
            print(f"Rank/percentile error: {e}")
            import traceback; traceback.print_exc()
            player_percentiles = {}


        # Player usage
        cursor.execute('''
            SELECT overall, pass, rush, first_down, second_down, third_down,
                   standard_downs, passing_downs
            FROM player_usage WHERE player_id=%s
        ''', (player_id,))
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
    try:
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
                WHERE (g.home_team = ANY(%s) OR g.away_team = ANY(%s)) AND g.completed=1
                ORDER BY g.start_date ASC
            ''', (player_teams,) * 8)
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
            for game_row in games_list:
                game_id, week, opp, my_pts, opp_pts, ha, opp_logo, my_team, season_type, notes = game_row
                team_id = espn_team_id_by_team.get(my_team)
                if not team_id:
                    continue
                try:
                    r = req.get(
                        f'https://sports.core.api.espn.com/v2/sports/football/leagues/'
                        f'college-football/events/{game_id}/competitions/{game_id}'
                        f'/competitors/{team_id}/roster/{espn_athlete_id}/statistics/0',
                        timeout=5
                    )
                    if r.status_code != 200:
                        continue
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

                    game_log.append({
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
                    })
                except Exception:
                    pass

    except Exception as e:
        print(f"ESPN game log error: {e}")
        import traceback; traceback.print_exc()

    return render_template('player.html',
        player=player, stats=stats, ppa=ppa,
        ap_rank=ap_rank, c1=c1, c2=c2,
        game_log=game_log,
        player_percentiles=player_percentiles,
        national_ranks=national_ranks,
        usage=usage,
        is_active_2026=is_active_2026,
        draft_status=draft_status,
        transfers_history=transfers_history,
        previous_stat_teams=previous_stat_teams,
        transfer_team_logos=transfer_team_logos,
        team_abbrevs=team_abbrevs,
    )


@app.route('/transfers')
def transfers():
    conn = get_db()
    try:
        cursor = conn.cursor()

        year        = request.args.get('year', '2026')
        pos_filter  = request.args.get('pos', '')
        conf_filter = request.args.get('conf', '')
        page        = max(1, int(request.args.get('page', 1)))
        per_page    = 50

        pos_sql  = f"AND t.position='{pos_filter}'"        if pos_filter  else ""
        conf_sql = f"AND t_dest.conference='{conf_filter}'" if conf_filter else ""

        where = f"WHERE t.year=%s {pos_sql} {conf_sql}"

        cursor.execute(f'''
            SELECT COUNT(*) FROM transfers t
            LEFT JOIN teams t_dest ON t_dest.name=t.destination
            {where}
        ''', (year,))
        total_count = cursor.fetchone()[0]
        total_pages = max(1, (total_count + per_page - 1) // per_page)
        page = min(page, total_pages)
        offset = (page - 1) * per_page

        cursor.execute(f'''
            SELECT t.first_name, t.last_name, t.position, t.origin, t.destination,
                   t.transfer_date, t.rating, t.stars, t.eligibility,
                   p.id as player_id, p.headshot,
                   t_dest.logo_dark as dest_logo,
                   t_orig.logo_dark as orig_logo,
                   t_dest.conference as dest_conf
            FROM transfers t
            LEFT JOIN players p ON p.first_name=t.first_name AND p.last_name=t.last_name
            LEFT JOIN teams t_dest ON t_dest.name=t.destination
            LEFT JOIN teams t_orig ON t_orig.name=t.origin
            {where}
            ORDER BY t.rating DESC NULLS LAST, t.stars DESC NULLS LAST
            LIMIT %s OFFSET %s
        ''', (year, per_page, offset))
        portal = cursor.fetchall()

        cursor.execute('SELECT DISTINCT conference FROM teams WHERE conference IS NOT NULL ORDER BY conference')
        conferences = [r[0] for r in cursor.fetchall()]

        cursor.execute('SELECT DISTINCT position FROM transfers WHERE year=%s AND position IS NOT NULL ORDER BY position', (year,))
        positions = [r[0] for r in cursor.fetchall()]

    finally:
        release_db(conn)
    return render_template('transfers.html', portal=portal, year=year,
                           conferences=conferences, positions=positions,
                           pos_filter=pos_filter, conf_filter=conf_filter,
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
                t1.logo_dark, t2.logo_dark,
                t1.color, t2.color, t1.conference
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
        ta_color=ta_info[1] if ta_info else '#1e3a5f',
        tb_color=tb_info[1] if tb_info else '#0f1e3a',
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

def _cmp_games_played(cursor, team, cache):
    """Completed team games this season, used as a per-game divisor.
    player_stats has season totals only (no week/game_id column), so an
    individual player's games-played can't be counted directly — team
    games completed is the closest available proxy."""
    if team not in cache:
        cursor.execute('''
            SELECT COUNT(*) FROM games
            WHERE completed=1 AND season_type='SeasonType.REGULAR'
            AND (home_team=%s OR away_team=%s)
        ''', (team, team))
        cache[team] = max(cursor.fetchone()[0] or 0, 1)
    return cache[team]

def _cmp_row(label, player_ids, full_pool, qual_pool, stat_key, games_by_pid,
             higher_better=True, per_game=False, decimals=1, suffix='', scale=1):
    values = []
    for pid in player_ids:
        raw = full_pool.get(str(pid), {}).get(stat_key)
        if raw is None:
            values.append({'raw': None, 'display': '—', 'percentile': None})
            continue
        shown = (raw / games_by_pid[pid] if per_game else raw) * scale
        _, pct, _ = _rank_pct(pid, qual_pool, stat_key, higher_better)
        values.append({'raw': raw, 'display': f'{shown:.{decimals}f}{suffix}', 'percentile': pct})
    return {'label': label, 'higher_better': higher_better, 'values': values}

def _cmp_team_proxy_row(cursor, label, player_ids, player_teams, column, pct=False):
    """Row sourced from team_stats as a proxy for a player-level stat that
    isn't tracked individually. No percentile — the existing qualification
    system only covers player_stats/player_ppa pools."""
    teams = sorted({player_teams[pid] for pid in player_ids if player_teams.get(pid)})
    vals_by_team = {}
    if teams:
        ph = ','.join(['%s'] * len(teams))
        cursor.execute(f'SELECT team, {column} FROM team_stats WHERE team IN ({ph})', teams)
        vals_by_team = dict(cursor.fetchall())
    values = []
    for pid in player_ids:
        v = vals_by_team.get(player_teams.get(pid))
        if v is None:
            values.append({'raw': None, 'display': '—', 'percentile': None})
        else:
            shown = v * 100 if pct else v
            values.append({'raw': v, 'display': f'{shown:.1f}{"%" if pct else ""}', 'percentile': None})
    return {'label': label, 'higher_better': True, 'values': values}

def _cmp_usage_row(cursor, label, player_ids, column, pct=True):
    ph = ','.join(['%s'] * len(player_ids))
    cursor.execute(f'SELECT player_id, {column} FROM player_usage WHERE player_id IN ({ph})', player_ids)
    vals_by_pid = dict(cursor.fetchall())
    values = []
    for pid in player_ids:
        v = vals_by_pid.get(pid)
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

def _build_compare_group_rows(cursor, group_name, player_ids, player_teams):
    games_cache = {}
    games_by_pid = {pid: _cmp_games_played(cursor, player_teams.get(pid, ''), games_cache)
                     for pid in player_ids}
    peer = COMPARE_PEER_POSITIONS[group_name]
    rows = []

    if group_name == 'QB':
        sp = _fetch_stats_pool(cursor, 'passing', peer)
        pass_stat, pass_min = _qual_threshold('QB', 'passing')
        sp_q = _qualify_pool(sp, sp, pass_stat, pass_min)
        rows.append(_cmp_row('Comp %',     player_ids, sp, sp_q, 'PCT', games_by_pid, suffix='%', scale=100))
        rows.append(_cmp_row('Pass Yds/G', player_ids, sp, sp_q, 'YDS', games_by_pid, per_game=True))
        rows.append(_cmp_row('Pass TD/G',  player_ids, sp, sp_q, 'TD',  games_by_pid, per_game=True))
        rows.append(_cmp_row('INT/G',      player_ids, sp, sp_q, 'INT', games_by_pid, per_game=True, higher_better=False))
        rows.append(_cmp_row('Yds/Att',    player_ids, sp, sp_q, 'YPA', games_by_pid))

        rush_sp = _fetch_stats_pool(cursor, 'rushing', ['QB'])
        rush_stat, rush_min = _qual_threshold('QB', 'rushing')
        rush_sp_q = _qualify_pool(rush_sp, rush_sp, rush_stat, rush_min)
        rows.append(_cmp_row('Rush Yds/G', player_ids, rush_sp, rush_sp_q, 'YDS', games_by_pid, per_game=True))

        pp = _fetch_ppa_pool(cursor, peer)
        ppa_stat, ppa_min = _qual_threshold('QB', 'ppa')
        pp_q = _qualify_pool(pp, sp_q, ppa_stat, ppa_min)
        rows.append(_cmp_row('EPA / Pass Play', player_ids, pp, pp_q, 'avg_ppa_pass', games_by_pid, decimals=3))

    elif group_name == 'RB':
        sp = _fetch_stats_pool(cursor, 'rushing', peer)
        rush_stat, rush_min = _qual_threshold('RB', 'rushing')
        sp_q = _qualify_pool(sp, sp, rush_stat, rush_min)
        rows.append(_cmp_row('Rush Yds/G', player_ids, sp, sp_q, 'YDS', games_by_pid, per_game=True))
        rows.append(_cmp_row('Yds/Carry',  player_ids, sp, sp_q, 'YPC', games_by_pid))
        rows.append(_cmp_row('Rush TD/G',  player_ids, sp, sp_q, 'TD',  games_by_pid, per_game=True))

        pp = _fetch_ppa_pool(cursor, peer)
        ppa_stat, ppa_min = _qual_threshold('RB', 'ppa')
        pp_q = _qualify_pool(pp, sp_q, ppa_stat, ppa_min)
        rows.append(_cmp_row('EPA / Rush', player_ids, pp, pp_q, 'avg_ppa_rush', games_by_pid, decimals=3))

        rows.append(_cmp_team_proxy_row(cursor, 'Rush Success Rate (Team)', player_ids, player_teams,
                                         'off_rushing_success_rate', pct=True))
        rows.append(_cmp_usage_row(cursor, 'Rush Usage', player_ids, 'rush', pct=True))

    elif group_name in ('WR', 'TE'):
        sp = _fetch_stats_pool(cursor, 'receiving', peer)
        rec_stat, rec_min = _qual_threshold(group_name, 'receiving')
        sp_q = _qualify_pool(sp, sp, rec_stat, rec_min)
        rows.append(_cmp_row('Rec/G',     player_ids, sp, sp_q, 'REC', games_by_pid, per_game=True))
        rows.append(_cmp_row('Rec Yds/G', player_ids, sp, sp_q, 'YDS', games_by_pid, per_game=True))
        rows.append(_cmp_row('Rec TD/G',  player_ids, sp, sp_q, 'TD',  games_by_pid, per_game=True))
        rows.append(_cmp_row('Yds/Rec',   player_ids, sp, sp_q, 'YPR', games_by_pid))

        pp = _fetch_ppa_pool(cursor, peer)
        ppa_stat, ppa_min = _qual_threshold(group_name, 'ppa')
        pp_q = _qualify_pool(pp, sp_q, ppa_stat, ppa_min)
        rows.append(_cmp_row('EPA / Play', player_ids, pp, pp_q, 'avg_ppa_all', games_by_pid, decimals=3))

    elif group_name in ('DL', 'LB'):
        sp_wide = _fetch_stats_pool(cursor, 'defensive', COMPARE_WIDE_DEF_POSITIONS)
        sp_narrow = _fetch_stats_pool(cursor, 'defensive', peer)
        def_stat, def_min = _qual_threshold(group_name, 'defensive')
        sp_wide_q = _qualify_pool(sp_wide, sp_wide, def_stat, def_min)
        sp_narrow_q = _qualify_pool(sp_narrow, sp_narrow, def_stat, def_min)
        rows.append(_cmp_row('Tackles/G', player_ids, sp_wide, sp_wide_q, 'TOT',   games_by_pid, per_game=True))
        rows.append(_cmp_row('Sacks/G',   player_ids, sp_wide, sp_wide_q, 'SACKS', games_by_pid, per_game=True))
        rows.append(_cmp_row('TFL/G',     player_ids, sp_narrow, sp_narrow_q, 'TFL', games_by_pid, per_game=True))
        rows.append(_cmp_row('PBU/G',     player_ids, sp_narrow, sp_narrow_q, 'PD',  games_by_pid, per_game=True))
        # No EPA/Play row here — player_ppa only covers offensive skill positions
        # (QB/RB/FB/TE/WR) in this dataset, so it would always be empty for DL/LB.

    elif group_name == 'DB':
        sp = _fetch_stats_pool(cursor, 'defensive', peer)
        def_stat, def_min = _qual_threshold('DB', 'defensive')
        sp_q = _qualify_pool(sp, sp, def_stat, def_min)
        rows.append(_cmp_row('Tackles/G', player_ids, sp, sp_q, 'TOT',   games_by_pid, per_game=True))
        rows.append(_cmp_row('Sacks/G',   player_ids, sp, sp_q, 'SACKS', games_by_pid, per_game=True))
        rows.append(_cmp_row('TFL/G',     player_ids, sp, sp_q, 'TFL',   games_by_pid, per_game=True))
        rows.append(_cmp_row('PBU/G',     player_ids, sp, sp_q, 'PD',    games_by_pid, per_game=True))
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

def _build_compare_team_rows(cursor, team_names):
    ph = ','.join(['%s'] * len(team_names))
    cursor.execute(f'SELECT * FROM team_stats WHERE team IN ({ph})', team_names)
    cols = [d[0] for d in cursor.description]
    ts_by_team = {r[0]: dict(zip(cols, r)) for r in cursor.fetchall()}
    cursor.execute(f'SELECT team, rating, ranking FROM sp_ratings WHERE team IN ({ph})', team_names)
    sp_by_team = {r[0]: {'rating': r[1], 'ranking': r[2]} for r in cursor.fetchall()}

    # Savant Rating — the site's signature metric leads the team comparison.
    cursor.execute(f'''SELECT team, net_rating, net_ranking, off_rating, off_ranking, def_rating, def_ranking
                       FROM savant_ratings WHERE team IN ({ph})''', team_names)
    svr_by_team = {r[0]: {'net': r[1], 'net_rk': r[2], 'off': r[3], 'off_rk': r[4],
                          'def': r[5], 'def_rk': r[6]} for r in cursor.fetchall()}

    def _svr_row(label, key, rk_key, higher_better, signed=False):
        values = []
        for team in team_names:
            s = svr_by_team.get(team)
            if s and s.get(key) is not None:
                num = f'{s[key]:+.1f}' if signed else f'{s[key]:.1f}'
                values.append({'raw': s[key], 'display': f'{num} (#{s[rk_key]})', 'percentile': None})
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
        for team in team_names:
            v = ts_by_team.get(team, {}).get(col)
            if v is None:
                values.append({'raw': None, 'display': '—', 'percentile': None})
            else:
                shown = v * 100 if suffix == '%' else v
                values.append({'raw': v, 'display': f'{shown:.{decimals}f}{suffix}', 'percentile': None})
        rows.append({'label': label, 'higher_better': higher_better, 'values': values})

    sp_values = []
    for team in team_names:
        s = sp_by_team.get(team)
        if s and s.get('rating') is not None:
            sp_values.append({'raw': s['rating'], 'display': f"{s['rating']:.1f} (#{s['ranking']})", 'percentile': None})
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
        r = req.get(url, timeout=8)
        ctype = r.headers.get('Content-Type', 'image/png')
        if r.status_code != 200 or not ctype.startswith('image/'):
            return '', 502
        return Response(r.content, content_type=ctype,
                        headers={'Cache-Control': 'public, max-age=604800'})
    except Exception:
        return '', 502


@app.route('/compare')
def compare():
    mode = request.args.get('type', 'player')
    if mode not in ('player', 'team'):
        mode = 'player'
    pos_filter = request.args.get('pos', '')

    conn = get_db()
    try:
        cursor = conn.cursor()
        # `slots` stays exactly 3 long (None for an empty/invalid slot) so the
        # search UI can address slot 1/2/3 correctly by index. `active_*` is the
        # compacted (2 or 3 long) list actually used for the card + stat rows.
        slots, rows, group_name = [None, None, None], [], None

        if mode == 'team':
            slot_names = [request.args.get(f't{i}') for i in (1, 2, 3)]
            valid_names = [n for n in slot_names if n]
            info_by_name, rank_by_team = {}, {}
            if valid_names:
                ph = ','.join(['%s'] * len(valid_names))
                cursor.execute(f'''
                    SELECT name, conference, logo_dark, color, alt_color
                    FROM teams WHERE name IN ({ph})
                ''', valid_names)
                info_by_name = {r[0]: r for r in cursor.fetchall()}
                cursor.execute(f'SELECT team, rank FROM ap_rankings WHERE team IN ({ph})', valid_names)
                rank_by_team = dict(cursor.fetchall())

            for i, name in enumerate(slot_names):
                info = info_by_name.get(name) if name else None
                if info:
                    slots[i] = {
                        'name': info[0], 'conference': info[1], 'logo_dark': info[2],
                        'color': info[3], 'alt_color': info[4], 'ap_rank': rank_by_team.get(name),
                    }

            active = [s for s in slots if s]
            if len(active) >= 2:
                rows = _build_compare_team_rows(cursor, [t['name'] for t in active])

        else:
            slot_ids = [int(raw) if raw and raw.isdigit() else None
                        for raw in (request.args.get(f'p{i}') for i in (1, 2, 3))]
            valid_ids = [pid for pid in slot_ids if pid is not None]
            info_by_id, rank_by_team = {}, {}
            if valid_ids:
                ph = ','.join(['%s'] * len(valid_ids))
                cursor.execute(f'''
                    SELECT p.id, p.first_name, p.last_name, p.team, p.position, p.jersey,
                           p.headshot, t.logo_dark, t.color, t.alt_color, t.conference
                    FROM players p LEFT JOIN teams t ON p.team = t.name
                    WHERE p.id IN ({ph})
                ''', valid_ids)
                info_by_id = {r[0]: r for r in cursor.fetchall()}
                teams_involved = sorted({r[3] for r in info_by_id.values() if r[3]})
                if teams_involved:
                    ph2 = ','.join(['%s'] * len(teams_involved))
                    cursor.execute(f'SELECT team, rank FROM ap_rankings WHERE team IN ({ph2})', teams_involved)
                    rank_by_team = dict(cursor.fetchall())

            for i, pid in enumerate(slot_ids):
                info = info_by_id.get(pid) if pid is not None else None
                if info:
                    slots[i] = {
                        'id': info[0], 'first_name': info[1], 'last_name': info[2],
                        'team': info[3], 'position': info[4], 'jersey': info[5],
                        'headshot': info[6], 'logo_dark': info[7], 'color': info[8],
                        'alt_color': info[9], 'conference': info[10],
                        'ap_rank': rank_by_team.get(info[3]),
                    }

            active = [s for s in slots if s]
            if not pos_filter and active:
                first_pos = (active[0]['position'] or '').upper()
                pos_filter = POS_GROUP_MAP.get(first_pos, 'QB')
            group_name = pos_filter if pos_filter in COMPARE_PEER_POSITIONS else 'QB'

            if len(active) >= 2:
                player_ids_ordered = [p['id'] for p in active]
                player_teams = {p['id']: p['team'] for p in active}
                rows = _build_compare_group_rows(cursor, group_name, player_ids_ordered, player_teams)
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


@app.route('/bracket')
@cache.cached(timeout=3600)
def bracket_page():
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT team, rank FROM ap_rankings
            WHERE season = 2025
            ORDER BY (season_type = 'postseason') DESC, week DESC, rank ASC
            LIMIT 25
        ''')
        poll = cursor.fetchall()

        cursor.execute('''
            SELECT id, home_team, away_team, home_points, away_points,
                   completed, notes, start_date
            FROM games
            WHERE season = 2025
              AND UPPER(season_type) LIKE '%%POSTSEASON%%'
              AND notes ILIKE '%%college football playoff%%'
            ORDER BY start_date ASC
        ''')
        cfp_games = [{
            'id': r[0], 'home_team': r[1], 'away_team': r[2],
            'home_points': r[3], 'away_points': r[4], 'completed': r[5],
            'notes': r[6] or '', 'start_date': r[7] or '',
        } for r in cursor.fetchall()]

        fr_games = [g for g in cfp_games if 'first round' in g['notes'].lower()]
        qf_games = [g for g in cfp_games if 'quarterfinal' in g['notes'].lower()]
        sf_games = [g for g in cfp_games if 'semifinal' in g['notes'].lower()]
        nc_games = [g for g in cfp_games if 'national championship' in g['notes'].lower()]

        seeds = _cfp_seed_teams(poll, fr_games, qf_games)
        team_by_seed = {s: t for t, s in seeds.items()}

        cursor.execute('''
            SELECT name, logo_dark, logo, color, alt_color, abbreviation, conference
            FROM teams WHERE name = ANY(%s)
        ''', (list(seeds.keys()) or [''],))
        teams_map = {r[0]: {
            'name': r[0], 'logo': r[1] or r[2], 'color': r[3] or '#f59e0b',
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
            'color': info.get('color', '#f59e0b'),
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
                           cfp_teams=cfp_teams, champion=champion)


@app.route('/sitemap.xml')
@cache.cached(timeout=86400)  # regenerated daily
def sitemap():
    """XML sitemap of every indexable page, for search-engine discovery."""
    base = f"https://{request.host}"
    paths = ['/', '/teams', '/rankings', '/leaderboards', '/leaderboards/teams',
             '/bracket', '/compare', '/transfers', '/rivalries', '/savant-rating']
    for cat in ('passing', 'rushing', 'receiving', 'defense'):
        paths.append(f'/leaderboards/{cat}')
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT slug FROM teams WHERE slug IS NOT NULL ORDER BY slug')
        paths += [f'/team/{r[0]}' for r in cur.fetchall()]
        cur.execute('SELECT id FROM games WHERE completed = 1')
        paths += [f'/game/{r[0]}' for r in cur.fetchall()]
        cur.execute('SELECT id FROM players ORDER BY id')
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
    base = f"https://{request.host}"
    return Response(
        f"User-agent: *\nAllow: /\nDisallow: /admin/\nSitemap: {base}/sitemap.xml\n",
        mimetype='text/plain')


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


if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5001))
    app.run(debug=True, host='0.0.0.0', port=port)