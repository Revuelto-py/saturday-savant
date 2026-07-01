import cfbd
import psycopg2
from psycopg2 import pool as pg_pool
import os
import re
import datetime
import requests as req
from urllib.parse import urlencode
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify
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

def get_ap_rankings(cursor):
    cursor.execute('SELECT team, rank FROM ap_rankings ORDER BY rank')
    return {row[0]: row[1] for row in cursor.fetchall()}

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

def get_game_label(notes):
    if not notes: return 'Bowl Games'
    if 'National Championship' in notes: return 'National Championship'
    if 'Semifinal' in notes: return 'Semifinal'
    if 'Quarterfinal' in notes: return 'Quarterfinal'
    if 'First Round' in notes: return 'First Round'
    if 'Conference Championship' in notes: return 'Conference Championships'
    return 'Bowl Games'

def build_lineup(roster, player_stats_map=None):
    if player_stats_map is None:
        player_stats_map = {}
    slot_positions = {
        'QB': ['QB'], 'WR1': ['WR'], 'WR2': ['WR'], 'TE': ['TE'],
        'LT': ['LT','OT','OL'], 'LG': ['LG','OG','OL'], 'C': ['C','OL'],
        'RG': ['RG','OG','OL'], 'RT': ['RT','OT','OL'],
        'RB': ['RB','HB','FB','APB','ATH'],
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
        key = pos.upper()
        if key not in pos_pool: pos_pool[key] = []
        stats = player_stats_map.get(str(pid), {})
        pos_pool[key].append({'idx': pid, 'name': last, 'first': first,
            'jersey': jersey or '', 'pos': pos, 'headshot': headshot, 'yds': stats.get('YDS', 0) or 0})
    for pos_key in pos_pool:
        pos_pool[pos_key].sort(key=lambda x: x['yds'], reverse=True)
    lineup = {}
    used = set()
    for slot, positions in slot_positions.items():
        for pos_type in positions:
            if pos_type in pos_pool:
                for player in pos_pool[pos_type]:
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

FCS_CONFS = ('CAA','Big Sky','MVFC','SWAC','MEAC','Southland','Big South','OVC','Patriot','NEC','Pioneer','FCS Independents')

LEADERBOARD_PER_PAGE = 25

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
                FROM games WHERE completed = 1
            ) sub
            ORDER BY sort_order, week DESC
        ''')
        all_weeks = cursor.fetchall()

        if week is None:
            cursor.execute("SELECT MAX(week) FROM games WHERE completed = 1 AND season_type = 'SeasonType.REGULAR'")
            week = cursor.fetchone()[0]
            season_type = 'regular'

        db_season_type = 'SeasonType.POSTSEASON' if season_type == 'postseason' else 'SeasonType.REGULAR'

        cursor.execute('''
            SELECT g.home_team, g.home_points, g.away_team, g.away_points,
                   g.week, g.season_type, g.notes, t1.logo, t2.logo,
                   t1.logo_dark, t2.logo_dark
            FROM games g
            LEFT JOIN teams t1 ON g.home_team = t1.name
            LEFT JOIN teams t2 ON g.away_team = t2.name
            WHERE g.completed = 1 AND g.week = %s AND g.season_type = %s
            ORDER BY CASE WHEN g.notes LIKE '%%National Championship%%' THEN 1
                          WHEN g.notes LIKE '%%Semifinal%%' THEN 2
                          WHEN g.notes LIKE '%%Quarterfinal%%' THEN 3
                          WHEN g.notes LIKE '%%First Round%%' THEN 4
                          WHEN g.notes LIKE '%%Conference Championship%%' THEN 5
                          ELSE 6 END, g.notes, g.id
        ''', (week, db_season_type))
        raw_games = cursor.fetchall()

        # Enrich each game tuple with rivalry name as last element
        games = []
        for g in raw_games:
            rivalry = get_rivalry(cursor, g[0], g[2]) or ''
            games.append(g + (rivalry,))

        label_order = ['National Championship','Semifinal','Quarterfinal','First Round','Conference Championships','Bowl Games']
        grouped_games = OrderedDict((label, []) for label in label_order)
        for game in games:
            grouped_games[get_game_label(game[6])].append(game)
        grouped_games = {k: v for k, v in grouped_games.items() if v}

        top_receivers = leaders_query(cursor, 'receiving', 'YDS')
        top_rushers   = leaders_query(cursor, 'rushing',   'YDS')
        top_passers   = leaders_query(cursor, 'passing',   'YDS')

    finally:
        release_db(conn)
    return render_template('home.html',
        games=games, grouped_games=grouped_games, all_weeks=all_weeks,
        selected_week=week, season_type=season_type,
        top_receivers=top_receivers, top_rushers=top_rushers, top_passers=top_passers,
        ap_rankings=ap_rankings)

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
@cache.cached(timeout=3600, query_string=True)  # 1 hour, each filter combo cached separately
def leaderboards(category='passing'):
    conn = get_db()
    try:
        cursor = conn.cursor()

        conf_filter = request.args.get('conf', '')
        pos_filter  = request.args.get('pos', '')
        min_filter  = request.args.get('min', '')
        sort_col    = request.args.get('sort', '')
        sort_dir    = request.args.get('dir', 'desc')
        page_raw    = request.args.get('page', '1')

        cursor.execute('SELECT DISTINCT conference FROM teams WHERE conference IS NOT NULL ORDER BY conference')
        conferences = [r[0] for r in cursor.fetchall() if r[0] not in FCS_CONFS]

        ap_rankings = get_ap_rankings(cursor)
        players = []
        pagination = None

        fcs_in     = "','".join(FCS_CONFS)
        conf_sql   = f"AND t.conference = '{conf_filter}'" if conf_filter else ""
        pos_sql    = f"AND p.position = '{pos_filter}'"   if pos_filter  else ""
        dir_sql    = "ASC" if sort_dir == "asc" else "DESC"
        # Map URL sort param to SQL alias (handles reserved words)
        _sort_remap = {'int': 'int_', 'long': 'long_'}

        if category == 'passing':
            ALLOWED  = {'yds','td','int','att','cmp','pct','ypa','epa_play','epa_pass','total_epa'}
            sort_col = sort_col if sort_col in ALLOWED else 'yds'
            sort_sql = _sort_remap.get(sort_col, sort_col)
            min_att  = min_filter if min_filter.isdigit() else '100'

            cursor.execute(f'''
                SELECT COUNT(*) FROM (
                    SELECT p.id
                    FROM players p
                    JOIN teams t ON p.team = t.name
                    JOIN player_stats ps ON ps.player_id = p.id::text AND ps.category = 'passing'
                    WHERE p.position = 'QB'
                      AND t.conference NOT IN ('{fcs_in}')
                      {conf_sql}
                    GROUP BY p.id
                    HAVING MAX(CASE WHEN ps.stat_type='ATT' THEN CAST(ps.stat AS REAL) END) >= {min_att}
                ) sub
            ''')
            page, offset, pagination = _pagination_ctx(page_raw, cursor.fetchone()[0])

            cursor.execute(f'''
                SELECT
                    p.id, p.first_name, p.last_name, p.team, p.position, p.jersey, p.headshot,
                    t.logo_dark, t.conference, t.color,
                    MAX(CASE WHEN ps.stat_type='YDS'         THEN CAST(ps.stat AS REAL) END) as yds,
                    MAX(CASE WHEN ps.stat_type='TD'          THEN CAST(ps.stat AS REAL) END) as td,
                    MAX(CASE WHEN ps.stat_type='INT'         THEN CAST(ps.stat AS REAL) END) as int_,
                    MAX(CASE WHEN ps.stat_type='ATT'         THEN CAST(ps.stat AS REAL) END) as att,
                    MAX(CASE WHEN ps.stat_type='COMPLETIONS' THEN CAST(ps.stat AS REAL) END) as cmp,
                    MAX(CASE WHEN ps.stat_type='PCT'         THEN CAST(ps.stat AS REAL) END) as pct,
                    MAX(CASE WHEN ps.stat_type='YPA'         THEN CAST(ps.stat AS REAL) END) as ypa,
                    pp.avg_ppa_all  as epa_play,
                    pp.avg_ppa_pass as epa_pass,
                    pp.total_ppa    as total_epa
                FROM players p
                JOIN teams t ON p.team = t.name
                JOIN player_stats ps ON ps.player_id = p.id::text AND ps.category = 'passing'
                LEFT JOIN player_ppa pp ON pp.player_id = p.id::text::text
                WHERE p.position = 'QB'
                  AND t.conference NOT IN ('{fcs_in}')
                  {conf_sql}
                GROUP BY p.id, t.logo_dark, t.conference, t.color, pp.avg_ppa_all, pp.avg_ppa_pass, pp.total_ppa
                HAVING MAX(CASE WHEN ps.stat_type='ATT' THEN CAST(ps.stat AS REAL) END) >= {min_att}
                ORDER BY {sort_sql} {dir_sql} NULLS LAST
                LIMIT {LEADERBOARD_PER_PAGE} OFFSET {offset}
            ''')
            for i, r in enumerate(cursor.fetchall()):
                pct = float(r[15] or 0)
                if pct <= 1.0: pct *= 100
                players.append({
                    'rank': offset+i+1, 'id': r[0], 'name': f"{r[1]} {r[2]}", 'first': r[1], 'last': r[2],
                    'team': r[3], 'pos': r[4], 'jersey': r[5], 'headshot': r[6],
                    'logo': r[7], 'conf': r[8], 'color': r[9],
                    'yds': int(r[10] or 0), 'td': int(r[11] or 0), 'int': int(r[12] or 0),
                    'att': int(r[13] or 0), 'cmp': int(r[14] or 0), 'pct': round(pct, 1),
                    'ypa':      round(float(r[16] or 0), 1),
                    'epa_play': round(float(r[17]), 3) if r[17] is not None else None,
                    'epa_pass': round(float(r[18]), 3) if r[18] is not None else None,
                    'total_epa':round(float(r[19]), 1) if r[19] is not None else None,
                })

        elif category == 'rushing':
            ALLOWED  = {'yds','td','att','ypc','long','epa_rush','total_epa'}
            sort_col = sort_col if sort_col in ALLOWED else 'yds'
            sort_sql = _sort_remap.get(sort_col, sort_col)
            min_att  = min_filter if min_filter.isdigit() else '50'

            cursor.execute(f'''
                SELECT COUNT(*) FROM (
                    SELECT p.id
                    FROM players p
                    JOIN teams t ON p.team = t.name
                    JOIN player_stats ps ON ps.player_id = p.id::text AND ps.category = 'rushing'
                    WHERE p.position IN ('RB','FB','QB','WR','ATH')
                      AND t.conference NOT IN ('{fcs_in}')
                      {conf_sql}
                      {pos_sql}
                    GROUP BY p.id
                    HAVING MAX(CASE WHEN ps.stat_type='CAR' THEN CAST(ps.stat AS REAL) END) >= {min_att}
                ) sub
            ''')
            page, offset, pagination = _pagination_ctx(page_raw, cursor.fetchone()[0])

            cursor.execute(f'''
                SELECT
                    p.id, p.first_name, p.last_name, p.team, p.position, p.jersey, p.headshot,
                    t.logo_dark, t.conference, t.color,
                    MAX(CASE WHEN ps.stat_type='YDS'  THEN CAST(ps.stat AS REAL) END) as yds,
                    MAX(CASE WHEN ps.stat_type='TD'   THEN CAST(ps.stat AS REAL) END) as td,
                    MAX(CASE WHEN ps.stat_type='CAR'  THEN CAST(ps.stat AS REAL) END) as att,
                    MAX(CASE WHEN ps.stat_type='YPC'  THEN CAST(ps.stat AS REAL) END) as ypc,
                    MAX(CASE WHEN ps.stat_type='LONG' THEN CAST(ps.stat AS REAL) END) as long_,
                    pp.avg_ppa_rush as epa_rush,
                    pp.total_ppa    as total_epa
                FROM players p
                JOIN teams t ON p.team = t.name
                JOIN player_stats ps ON ps.player_id = p.id::text AND ps.category = 'rushing'
                LEFT JOIN player_ppa pp ON pp.player_id = p.id::text::text
                WHERE p.position IN ('RB','FB','QB','WR','ATH')
                  AND t.conference NOT IN ('{fcs_in}')
                  {conf_sql}
                  {pos_sql}
                GROUP BY p.id, t.logo_dark, t.conference, t.color, pp.avg_ppa_rush, pp.total_ppa
                HAVING MAX(CASE WHEN ps.stat_type='CAR' THEN CAST(ps.stat AS REAL) END) >= {min_att}
                ORDER BY {sort_sql} {dir_sql} NULLS LAST
                LIMIT {LEADERBOARD_PER_PAGE} OFFSET {offset}
            ''')
            for i, r in enumerate(cursor.fetchall()):
                players.append({
                    'rank': offset+i+1, 'id': r[0], 'name': f"{r[1]} {r[2]}", 'first': r[1], 'last': r[2],
                    'team': r[3], 'pos': r[4], 'jersey': r[5], 'headshot': r[6],
                    'logo': r[7], 'conf': r[8], 'color': r[9],
                    'yds': int(r[10] or 0), 'td': int(r[11] or 0), 'att': int(r[12] or 0),
                    'ypc': round(float(r[13] or 0), 1), 'long': int(r[14] or 0),
                    'epa_rush': round(float(r[15]), 3) if r[15] is not None else None,
                    'total_epa':round(float(r[16]), 1) if r[16] is not None else None,
                })

        elif category == 'receiving':
            ALLOWED  = {'yds','td','rec','ypr','long','epa_play','total_epa'}
            sort_col = sort_col if sort_col in ALLOWED else 'yds'
            sort_sql = _sort_remap.get(sort_col, sort_col)
            min_rec  = min_filter if min_filter.isdigit() else '20'

            cursor.execute(f'''
                SELECT COUNT(*) FROM (
                    SELECT p.id
                    FROM players p
                    JOIN teams t ON p.team = t.name
                    JOIN player_stats ps ON ps.player_id = p.id::text AND ps.category = 'receiving'
                    WHERE p.position IN ('WR','TE','RB','ATH')
                      AND t.conference NOT IN ('{fcs_in}')
                      {conf_sql}
                      {pos_sql}
                    GROUP BY p.id
                    HAVING MAX(CASE WHEN ps.stat_type='REC' THEN CAST(ps.stat AS REAL) END) >= {min_rec}
                ) sub
            ''')
            page, offset, pagination = _pagination_ctx(page_raw, cursor.fetchone()[0])

            cursor.execute(f'''
                SELECT
                    p.id, p.first_name, p.last_name, p.team, p.position, p.jersey, p.headshot,
                    t.logo_dark, t.conference, t.color,
                    MAX(CASE WHEN ps.stat_type='YDS'  THEN CAST(ps.stat AS REAL) END) as yds,
                    MAX(CASE WHEN ps.stat_type='TD'   THEN CAST(ps.stat AS REAL) END) as td,
                    MAX(CASE WHEN ps.stat_type='REC'  THEN CAST(ps.stat AS REAL) END) as rec,
                    MAX(CASE WHEN ps.stat_type='YPR'  THEN CAST(ps.stat AS REAL) END) as ypr,
                    MAX(CASE WHEN ps.stat_type='LONG' THEN CAST(ps.stat AS REAL) END) as long_,
                    pp.avg_ppa_all as epa_play,
                    pp.total_ppa   as total_epa
                FROM players p
                JOIN teams t ON p.team = t.name
                JOIN player_stats ps ON ps.player_id = p.id::text AND ps.category = 'receiving'
                LEFT JOIN player_ppa pp ON pp.player_id = p.id::text::text
                WHERE p.position IN ('WR','TE','RB','ATH')
                  AND t.conference NOT IN ('{fcs_in}')
                  {conf_sql}
                  {pos_sql}
                GROUP BY p.id, t.logo_dark, t.conference, t.color, pp.avg_ppa_all, pp.total_ppa
                HAVING MAX(CASE WHEN ps.stat_type='REC' THEN CAST(ps.stat AS REAL) END) >= {min_rec}
                ORDER BY {sort_sql} {dir_sql} NULLS LAST
                LIMIT {LEADERBOARD_PER_PAGE} OFFSET {offset}
            ''')
            for i, r in enumerate(cursor.fetchall()):
                players.append({
                    'rank': offset+i+1, 'id': r[0], 'name': f"{r[1]} {r[2]}", 'first': r[1], 'last': r[2],
                    'team': r[3], 'pos': r[4], 'jersey': r[5], 'headshot': r[6],
                    'logo': r[7], 'conf': r[8], 'color': r[9],
                    'yds': int(r[10] or 0), 'td': int(r[11] or 0), 'rec': int(r[12] or 0),
                    'ypr': round(float(r[13] or 0), 1), 'long': int(r[14] or 0),
                    'epa_play': round(float(r[15]), 3) if r[15] is not None else None,
                    'total_epa':round(float(r[16]), 1) if r[16] is not None else None,
                })

        elif category == 'defense':
            ALLOWED  = {'tot','solo','sacks','tfl','pd','qbh'}
            sort_col = sort_col if sort_col in ALLOWED else 'tot'
            sort_sql = sort_col
            min_tot  = min_filter if min_filter.isdigit() else '15'

            cursor.execute(f'''
                SELECT COUNT(*) FROM (
                    SELECT p.id
                    FROM players p
                    JOIN teams t ON p.team = t.name
                    JOIN player_stats ps ON ps.player_id = p.id::text AND ps.category = 'defensive'
                    WHERE p.position IN ('DE','DT','NT','DL','EDGE','LB','CB','S','DB')
                      AND t.conference NOT IN ('{fcs_in}')
                      {conf_sql}
                      {pos_sql}
                    GROUP BY p.id
                    HAVING MAX(CASE WHEN ps.stat_type='TOT' THEN CAST(ps.stat AS REAL) END) >= {min_tot}
                ) sub
            ''')
            page, offset, pagination = _pagination_ctx(page_raw, cursor.fetchone()[0])

            cursor.execute(f'''
                SELECT
                    p.id, p.first_name, p.last_name, p.team, p.position, p.jersey, p.headshot,
                    t.logo_dark, t.conference, t.color,
                    MAX(CASE WHEN ps.stat_type='TOT'    THEN CAST(ps.stat AS REAL) END) as tot,
                    MAX(CASE WHEN ps.stat_type='SOLO'   THEN CAST(ps.stat AS REAL) END) as solo,
                    MAX(CASE WHEN ps.stat_type='SACKS'  THEN CAST(ps.stat AS REAL) END) as sacks,
                    MAX(CASE WHEN ps.stat_type='TFL'    THEN CAST(ps.stat AS REAL) END) as tfl,
                    MAX(CASE WHEN ps.stat_type='PD'     THEN CAST(ps.stat AS REAL) END) as pd,
                    MAX(CASE WHEN ps.stat_type='QB HUR' THEN CAST(ps.stat AS REAL) END) as qbh
                FROM players p
                JOIN teams t ON p.team = t.name
                JOIN player_stats ps ON ps.player_id = p.id::text AND ps.category = 'defensive'
                WHERE p.position IN ('DE','DT','NT','DL','EDGE','LB','CB','S','DB')
                  AND t.conference NOT IN ('{fcs_in}')
                  {conf_sql}
                  {pos_sql}
                GROUP BY p.id, t.logo_dark, t.conference, t.color
                HAVING MAX(CASE WHEN ps.stat_type='TOT' THEN CAST(ps.stat AS REAL) END) >= {min_tot}
                ORDER BY {sort_sql} {dir_sql} NULLS LAST
                LIMIT {LEADERBOARD_PER_PAGE} OFFSET {offset}
            ''')
            for i, r in enumerate(cursor.fetchall()):
                players.append({
                    'rank': offset+i+1, 'id': r[0], 'name': f"{r[1]} {r[2]}", 'first': r[1], 'last': r[2],
                    'team': r[3], 'pos': r[4], 'jersey': r[5], 'headshot': r[6],
                    'logo': r[7], 'conf': r[8], 'color': r[9],
                    'tot':  int(r[10] or 0),  'solo': int(r[11] or 0),
                    'sacks':round(float(r[12] or 0), 1),
                    'tfl':  round(float(r[13] or 0), 1),
                    'pd':   int(r[14] or 0),  'qbh': int(r[15] or 0),
                })

        elif category == 'epa':
            ALLOWED  = {'epa_play','epa_pass','epa_rush','total_epa'}
            sort_col = sort_col if sort_col in ALLOWED else 'epa_play'
            sort_sql = sort_col
            min_epa  = min_filter if min_filter.lstrip('-').replace('.','',1).isdigit() else '10'

            cursor.execute(f'''
                SELECT COUNT(*)
                FROM players p
                JOIN teams t ON p.team = t.name
                JOIN player_ppa pp ON pp.player_id = p.id::text
                WHERE t.conference NOT IN ('{fcs_in}')
                  {conf_sql}
                  {pos_sql}
                  AND pp.avg_ppa_all IS NOT NULL
                  AND pp.total_ppa >= {min_epa}
            ''')
            page, offset, pagination = _pagination_ctx(page_raw, cursor.fetchone()[0])

            cursor.execute(f'''
                SELECT
                    p.id, p.first_name, p.last_name, p.team, p.position, p.jersey, p.headshot,
                    t.logo_dark, t.conference, t.color,
                    pp.avg_ppa_all  as epa_play,
                    pp.avg_ppa_pass as epa_pass,
                    pp.avg_ppa_rush as epa_rush,
                    pp.total_ppa    as total_epa
                FROM players p
                JOIN teams t ON p.team = t.name
                JOIN player_ppa pp ON pp.player_id = p.id::text
                WHERE t.conference NOT IN ('{fcs_in}')
                  {conf_sql}
                  {pos_sql}
                  AND pp.avg_ppa_all IS NOT NULL
                  AND pp.total_ppa >= {min_epa}
                ORDER BY {sort_sql} {dir_sql} NULLS LAST
                LIMIT {LEADERBOARD_PER_PAGE} OFFSET {offset}
            ''')
            for i, r in enumerate(cursor.fetchall()):
                players.append({
                    'rank': offset+i+1, 'id': r[0], 'name': f"{r[1]} {r[2]}", 'first': r[1], 'last': r[2],
                    'team': r[3], 'pos': r[4], 'jersey': r[5], 'headshot': r[6],
                    'logo': r[7], 'conf': r[8], 'color': r[9],
                    'epa_play': round(float(r[10]), 3) if r[10] is not None else None,
                    'epa_pass': round(float(r[11]), 3) if r[11] is not None else None,
                    'epa_rush': round(float(r[12]), 3) if r[12] is not None else None,
                    'total_epa':round(float(r[13]), 1) if r[13] is not None else None,
                })

        elif category == 'usage':
            ALLOWED  = {'overall','pass_usage','rush_usage','first_down','second_down','third_down','standard','passing_downs'}
            sort_col = sort_col if sort_col in ALLOWED else 'overall'
            sort_sql = sort_col
            min_use  = '0'

            cursor.execute(f'''
                SELECT COUNT(*)
                FROM players p
                JOIN teams t ON p.team = t.name
                JOIN player_usage pu ON pu.player_id = p.id
                WHERE t.conference NOT IN ('{fcs_in}')
                  {conf_sql}
                  {pos_sql}
                  AND pu.overall IS NOT NULL
            ''')
            page, offset, pagination = _pagination_ctx(page_raw, cursor.fetchone()[0])

            cursor.execute(f'''
                SELECT
                    p.id, p.first_name, p.last_name, p.team, p.position, p.jersey, p.headshot,
                    t.logo_dark, t.conference, t.color,
                    pu.overall      as overall,
                    pu.pass         as pass_usage,
                    pu.rush         as rush_usage,
                    pu.first_down   as first_down,
                    pu.second_down  as second_down,
                    pu.third_down   as third_down,
                    pu.standard_downs  as standard,
                    pu.passing_downs   as passing_downs
                FROM players p
                JOIN teams t ON p.team = t.name
                JOIN player_usage pu ON pu.player_id = p.id
                WHERE t.conference NOT IN ('{fcs_in}')
                  {conf_sql}
                  {pos_sql}
                  AND pu.overall IS NOT NULL
                ORDER BY {sort_sql} {dir_sql} NULLS LAST
                LIMIT {LEADERBOARD_PER_PAGE} OFFSET {offset}
            ''')
            def _pct(v): return round(v * 100, 1) if v is not None else None
            for i, r in enumerate(cursor.fetchall()):
                players.append({
                    'rank': offset+i+1, 'id': r[0], 'name': f"{r[1]} {r[2]}", 'first': r[1], 'last': r[2],
                    'team': r[3], 'pos': r[4], 'jersey': r[5], 'headshot': r[6],
                    'logo': r[7], 'conf': r[8], 'color': r[9],
                    'overall':       _pct(r[10]),
                    'pass_usage':    _pct(r[11]),
                    'rush_usage':    _pct(r[12]),
                    'first_down':    _pct(r[13]),
                    'second_down':   _pct(r[14]),
                    'third_down':    _pct(r[15]),
                    'standard':      _pct(r[16]),
                    'passing_downs': _pct(r[17]),
                })

    finally:
        release_db(conn)
    return render_template('leaderboards.html',
        mode='player', players=players, category=category,
        conferences=conferences,
        conf_filter=conf_filter, pos_filter=pos_filter,
        min_filter=min_filter, sort_col=sort_col, sort_dir=sort_dir,
        ap_rankings=ap_rankings, pagination=pagination,
    )

# ── Team leaderboards ───────────────────────────────────────────────────────
TEAM_CATEGORY_DEFAULTS = {
    'offense': ('off_ppa', 'desc'),
    'defense': ('def_ppa', 'asc'),   # lower is better, so ascending = best first
    'havoc':   ('def_havoc_total', 'desc'),
    'scoring': ('off_pts_per_opp', 'desc'),
    'sp':      ('rating', 'desc'),
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
}
TEAM_LOWER_BETTER = {
    'def_ppa','def_success_rate','def_explosiveness',
    'def_line_yards','def_open_field_yards','def_second_level_yards',
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

@app.route('/leaderboards/teams')
@app.route('/leaderboards/teams/<category>')
@cache.cached(timeout=3600, query_string=True)
def leaderboards_teams(category='offense'):
    if category not in TEAM_CATEGORY_DEFAULTS:
        category = 'offense'

    conf_filter = request.args.get('conf', '')
    sort_col    = request.args.get('sort', '')
    sort_dir    = request.args.get('dir', '')
    page_raw    = request.args.get('page', '1')

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

        fcs_in   = "','".join(FCS_CONFS)
        conf_sql = "AND t.conference = %s" if conf_filter else ""
        params   = [conf_filter] if conf_filter else []

        cursor.execute(f'''
            SELECT COUNT(*) FROM teams t
            WHERE t.conference NOT IN ('{fcs_in}')
            {conf_sql}
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
                sp.rating, sp.offense_rating, sp.defense_rating, sp.special_teams_rating,
                RANK() OVER (ORDER BY {sort_sql} {goodness_dir} NULLS LAST) as goodness_rank
            FROM teams t
            LEFT JOIN team_stats ts ON ts.team = t.name
            LEFT JOIN team_advanced adv ON adv.team = t.name
            LEFT JOIN sp_ratings sp ON sp.team = t.name
            LEFT JOIN ap_rankings ar ON ar.team = t.name
            WHERE t.conference NOT IN ('{fcs_in}')
            {conf_sql}
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
            })
    finally:
        release_db(conn)

    return render_template('leaderboards.html',
        mode='team', teams=teams_out, category=category,
        conferences=conferences, conf_filter=conf_filter,
        sort_col=sort_col, sort_dir=sort_dir,
        pagination=pagination,
    )

@app.route('/teams')
@cache.cached(timeout=86400)  # 24 hours — basically static
def teams():
    conn = get_db()
    try:
        cursor = conn.cursor()
        ap_rankings = get_ap_rankings(cursor)
        cursor.execute('SELECT name, conference, logo_dark, color, alt_color FROM teams ORDER BY conference, name')
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
    return render_template('teams.html', conferences=sorted_confs, ap_rankings=ap_rankings)

@app.route('/team/<path:team_name>')
@cache.cached(timeout=3600)  # 1 hour — stats don't change during offseason
def team(team_name):
    conn = get_db()
    try:
        cursor = conn.cursor()
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
        schedule = [g + (get_rivalry(cursor, team_name, g[2]) or '',) for g in raw_schedule]

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

        cursor.execute("SELECT player_id, stat_type, MAX(stat) FROM player_stats WHERE team=%s AND category IN ('passing','rushing','receiving') GROUP BY player_id, stat_type", (team_name,))
        player_stats_map = {}
        for pid, stat_type, val in cursor.fetchall():
            if pid not in player_stats_map: player_stats_map[pid] = {}
            player_stats_map[pid][stat_type] = val

        # Build lineup from first 6 columns only
        lineup_roster = [r[:6] for r in roster]
        lineup = build_lineup(lineup_roster, player_stats_map)

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

        # Add headshots and player IDs to stat tables
        cursor.execute("SELECT (first_name || ' ' || last_name), headshot, id FROM players WHERE team=%s", (team_name,))
        _player_rows = cursor.fetchall()
        headshot_map   = {row[0]: row[1] for row in _player_rows}
        player_id_map  = {row[0]: row[2] for row in _player_rows}

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

            return render_template('team.html',
                    team=team_info, record=record, season_stats=season_stats,
                    standings=standings, schedule=schedule, roster=roster, lineup=lineup,
                    passing_stats=passing_stats, rushing_stats=rushing_stats,
                    receiving_stats=receiving_stats, defensive_stats=defensive_stats,
                    kicking_stats=kicking_stats, punting_stats=punting_stats,
                    kick_return_stats=kick_return_stats, punt_return_stats=punt_return_stats,
                    team_adv=team_adv, percentiles=percentiles, sp=sp,
                    ap_rankings=ap_rankings, team_rank=team_rank,
                    recruiting=recruiting, havoc=havoc)
    finally:
        release_db(conn)

@app.route('/api/players')
def api_players():
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify([])
    conn = get_db()
    try:
        cursor = conn.cursor()
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
                        'logo': r[2], 'color': r[3], 'url': f'/team/{r[0]}'})
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
                   a.prev_rank, t.logo_dark
            FROM ap_rankings a
            LEFT JOIN teams t ON a.team = t.name
            LEFT JOIN sp_ratings sp ON a.team = sp.team
            LEFT JOIN games g ON (g.home_team=a.team OR g.away_team=a.team)
                AND g.completed=1 AND g.season_type='SeasonType.REGULAR'
            GROUP BY a.rank, a.team, a.points, a.first_place_votes, a.week, a.prev_rank,
                     t.logo, t.conference, t.color, t.logo_dark,
                     sp.rating, sp.ranking
            ORDER BY a.rank
        ''')
        rows = cursor.fetchall()
        cursor.execute('SELECT week, season FROM ap_rankings LIMIT 1')
        meta = cursor.fetchone()
    finally:
        release_db(conn)
    return render_template('rankings.html', rankings=rows, meta=meta)

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
        date_str = game_info[8][:10].replace('-', '') if game_info[8] else None
        if date_str:
            r = req.get(
                'https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard',
                params={'dates': date_str, 'limit': 200},
                timeout=8
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
                timeout=10
            )
            data = s.json()

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
                            'text':       _pl.get('text', ''),
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

            # Play by play + drives
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
                if drive_result == 'TD':     result_icon = '🏈'
                elif drive_result == 'FG':   result_icon = '🎯'
                elif drive_result == 'PUNT': result_icon = '↩'
                elif drive_result in ['INT', 'FUMBLE', 'FUMBLE RETURN TD']: result_icon = '❌'
                else:                        result_icon = '🔄'

                # Field position bar — 8% margins = end zones, 84% = 100 yards
                yl = min(max(start_yl, 1), 99)
                if play_side == 'away':
                    field_start = round(8 + yl / 99 * 84, 1)
                    field_width = round(min(abs(drive_yards) / 99 * 84, max(0, 92 - field_start)), 1)
                else:
                    raw_end = 8 + (99 - yl) / 99 * 84
                    field_width = round(min(abs(drive_yards) / 99 * 84, max(0, raw_end - 8)), 1)
                    field_start = round(max(8, raw_end - field_width), 1)

                for play in (drive.get('plays') or []):
                    play_type = (play.get('type') or {}).get('text', '')
                    is_scoring = bool(play.get('scoringPlay', False))
                    plays.append({
                        'team': team_name,
                        'side': play_side,
                        'text': play.get('text', ''),
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

                drive_play_list = []
                for p in (drive.get('plays') or []):
                    ptype = (p.get('type') or {}).get('text', '')
                    pyards = int(p.get('statYards', 0) or 0)
                    drive_play_list.append({
                        'type':  ptype,
                        'yards': pyards,
                        'text':  p.get('text', ''),
                    })
                start_yl_abs = yl if play_side == 'away' else (100 - yl)

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
                    'result_icon': result_icon,
                    'field_start': field_start,
                    'field_width': max(field_width, 1.0),
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
                        athletes.append({
                            'name':     ad.get('displayName', ''),
                            'headshot': hs.get('href', '') if isinstance(hs, dict) else (hs or ''),
                            'stats':    dict(zip(labels, ae.get('stats', []))),
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

    # Date + season type formatting
    start_date_raw = game_info[8] or ''
    try:
        dt = datetime.datetime.fromisoformat(start_date_raw.replace('Z', '').replace('+00:00', ''))
        game_date = dt.strftime('%A, %B %-d, %Y')
        game_time = dt.strftime('%-I:%M %p ET')
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
        records=records,
        game_date=game_date,
        game_time=game_time,
        season_type_display=season_type_display,
        week_num=week_num,
        notes=notes,
        rivalry_name=rivalry_name,
    )


@app.route('/player/<int:player_id>')
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
                       CASE WHEN g.home_team = ANY(%s) THEN g.home_team ELSE g.away_team END
                FROM games g
                LEFT JOIN teams t1 ON g.home_team = t1.name
                LEFT JOIN teams t2 ON g.away_team = t2.name
                WHERE (g.home_team = ANY(%s) OR g.away_team = ANY(%s)) AND g.completed=1
                ORDER BY g.week
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
            game_id, _, _, _, _, _, _, my_team = game_row
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
                game_id, week, opp, my_pts, opp_pts, ha, opp_logo, my_team = game_row
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
                        'week':      week,
                        'game_id':   game_id,
                        'opponent':  opp or '',
                        'opp_logo':  opp_logo or '',
                        'home_away': ha,
                        'result':    result,
                        'stats':     gstats,
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

    rows = []
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


@app.route('/admin/clear-cache')
def clear_cache():
    if request.args.get('key') != os.getenv('ADMIN_KEY', 'changeme'):
        return 'Unauthorized', 401
    cache.clear()
    return 'Cache cleared', 200


if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5001))
    app.run(debug=True, host='0.0.0.0', port=port)