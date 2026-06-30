import cfbd
import psycopg2
import os
import re
import datetime
import requests as req
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify
from collections import OrderedDict

load_dotenv()

app = Flask(__name__)

configuration = cfbd.Configuration(
    access_token=os.getenv("CFBD_API_KEY")
)

def get_db():
    return psycopg2.connect(os.getenv('DATABASE_URL'))

def get_ap_rankings(cursor):
    cursor.execute('SELECT team, rank FROM ap_rankings ORDER BY rank')
    return {row[0]: row[1] for row in cursor.fetchall()}

_VALID_PPA_COLS = {'avg_ppa_all', 'avg_ppa_pass', 'avg_ppa_rush', 'total_ppa'}

def compute_rank_and_percentile(cursor, player_id, stat_type, category, positions, higher_better=True):
    """Rank and percentile computed against the IDENTICAL player pool."""
    placeholders = ','.join(['%s' for _ in positions])
    pid_str = str(player_id)

    if category == 'ppa':
        col = stat_type if stat_type in _VALID_PPA_COLS else 'avg_ppa_all'
        cursor.execute(f'''
            SELECT CAST(pp.player_id AS TEXT), pp.{col}
            FROM player_ppa pp
            JOIN players pl ON CAST(pp.player_id AS INTEGER) = pl.id
            WHERE pl.position IN ({placeholders}) AND pp.{col} IS NOT NULL
        ''', list(positions))
    else:
        cursor.execute(f'''
            SELECT ps.player_id, CAST(ps.stat AS REAL)
            FROM player_stats ps
            JOIN players pl ON CAST(ps.player_id AS INTEGER) = pl.id
            WHERE ps.category=%s AND ps.stat_type=%s AND pl.position IN ({placeholders})
            AND ps.stat IS NOT NULL
        ''', [category, stat_type] + list(positions))

    all_rows = cursor.fetchall()
    if not all_rows:
        return None, None, 0

    my_val = None
    for pid, val in all_rows:
        if str(pid) == pid_str and val is not None:
            my_val = float(val)
            break

    if my_val is None:
        return None, None, len(all_rows)

    all_vals = [float(v) for _, v in all_rows if v is not None]
    n = len(all_vals)
    if n == 0:
        return None, None, 0

    if higher_better:
        rank  = sum(1 for v in all_vals if v > my_val) + 1
        below = sum(1 for v in all_vals if v < my_val)
    else:
        rank  = sum(1 for v in all_vals if v < my_val) + 1
        below = sum(1 for v in all_vals if v > my_val)

    percentile = max(1, min(99, round((below / n) * 100)))
    return rank, percentile, n


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

def sort_players(cat_dict, sort_key, min_val=0):
    players = []
    for name, stats in cat_dict.items():
        val = float(stats.get(sort_key, 0) or 0)
        if val > min_val:
            players.append({'name': name, **stats})
    return sorted(players, key=lambda x: float(x.get(sort_key, 0) or 0), reverse=True)

FCS_CONFS = ('CAA','Big Sky','MVFC','SWAC','MEAC','Southland','Big South','OVC','Patriot','NEC','Pioneer','FCS Independents')

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

    conn.close()
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
        conn.close()
    return render_template('search.html', player_results=player_results, team_results=team_results, query=q)

@app.route('/leaderboards')
@app.route('/leaderboards/<category>')
def leaderboards(category='passing'):
    conn = get_db()
    cursor = conn.cursor()

    conf_filter = request.args.get('conf', '')
    pos_filter  = request.args.get('pos', '')
    min_filter  = request.args.get('min', '')
    sort_col    = request.args.get('sort', '')
    sort_dir    = request.args.get('dir', 'desc')

    cursor.execute('SELECT DISTINCT conference FROM teams WHERE conference IS NOT NULL ORDER BY conference')
    conferences = [r[0] for r in cursor.fetchall() if r[0] not in FCS_CONFS]

    ap_rankings = get_ap_rankings(cursor)
    players = []

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
            LIMIT 200
        ''')
        for i, r in enumerate(cursor.fetchall()):
            pct = float(r[15] or 0)
            if pct <= 1.0: pct *= 100
            players.append({
                'rank': i+1, 'id': r[0], 'name': f"{r[1]} {r[2]}", 'first': r[1], 'last': r[2],
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
            LIMIT 200
        ''')
        for i, r in enumerate(cursor.fetchall()):
            players.append({
                'rank': i+1, 'id': r[0], 'name': f"{r[1]} {r[2]}", 'first': r[1], 'last': r[2],
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
            LIMIT 200
        ''')
        for i, r in enumerate(cursor.fetchall()):
            players.append({
                'rank': i+1, 'id': r[0], 'name': f"{r[1]} {r[2]}", 'first': r[1], 'last': r[2],
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
            LIMIT 200
        ''')
        for i, r in enumerate(cursor.fetchall()):
            players.append({
                'rank': i+1, 'id': r[0], 'name': f"{r[1]} {r[2]}", 'first': r[1], 'last': r[2],
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
            LIMIT 200
        ''')
        for i, r in enumerate(cursor.fetchall()):
            players.append({
                'rank': i+1, 'id': r[0], 'name': f"{r[1]} {r[2]}", 'first': r[1], 'last': r[2],
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
            LIMIT 200
        ''')
        def _pct(v): return round(v * 100, 1) if v is not None else None
        for i, r in enumerate(cursor.fetchall()):
            players.append({
                'rank': i+1, 'id': r[0], 'name': f"{r[1]} {r[2]}", 'first': r[1], 'last': r[2],
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

    conn.close()
    return render_template('leaderboards.html',
        players=players, category=category,
        conferences=conferences,
        conf_filter=conf_filter, pos_filter=pos_filter,
        min_filter=min_filter, sort_col=sort_col, sort_dir=sort_dir,
        ap_rankings=ap_rankings,
    )

@app.route('/teams')
def teams():
    conn = get_db()
    cursor = conn.cursor()
    ap_rankings = get_ap_rankings(cursor)
    cursor.execute('SELECT name, conference, logo_dark, color, alt_color FROM teams ORDER BY conference, name')
    rows = cursor.fetchall()
    conn.close()
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
def team(team_name):
    conn = get_db()
    cursor = conn.cursor()
    ap_rankings = get_ap_rankings(cursor)
    team_rank = ap_rankings.get(team_name)

    cursor.execute('SELECT name, conference, abbreviation, logo, color, alt_color, logo_dark FROM teams WHERE name = %s', (team_name,))
    team_info = cursor.fetchone()
    if not team_info:
        conn.close()
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

        # Havoc + field position (from team_advanced)
        cursor.execute('''
            SELECT def_havoc_total, def_havoc_front7, def_havoc_db,
                   off_field_pos_avg_start, def_field_pos_avg_start,
                   off_scoring_opps, off_pts_per_opp
            FROM team_advanced WHERE team=%s
        ''', (team_name,))
        adv_row = cursor.fetchone()
        havoc = None
        if adv_row and adv_row[0] is not None:
            havoc = {
                'total':   round(adv_row[0] * 100, 1),
                'front7':  round(adv_row[1] * 100, 1) if adv_row[1] else None,
                'db':      round(adv_row[2] * 100, 1) if adv_row[2] else None,
                'off_fp':  round(adv_row[3], 1)       if adv_row[3] else None,
                'def_fp':  round(adv_row[4], 1)       if adv_row[4] else None,
                'scoring_opps': adv_row[5],
                'pts_per_opp': round(adv_row[6], 2)   if adv_row[6] else None,
            }

        # SP+ historical trend (5 years)
        cursor.execute('''
            SELECT year, rating, ranking, offense_rating, defense_rating
            FROM sp_historical WHERE team=%s ORDER BY year
        ''', (team_name,))
        sp_trend = [{'year': r[0], 'rating': round(r[1], 1) if r[1] else None,
                     'ranking': r[2],
                     'off': round(r[3], 1) if r[3] else None,
                     'def': round(r[4], 1) if r[4] else None}
                    for r in cursor.fetchall()]

        conn.close()

        return render_template('team.html',
                team=team_info, record=record, season_stats=season_stats,
                standings=standings, schedule=schedule, roster=roster, lineup=lineup,
                passing_stats=passing_stats, rushing_stats=rushing_stats,
                receiving_stats=receiving_stats, defensive_stats=defensive_stats,
                kicking_stats=kicking_stats, punting_stats=punting_stats,
                kick_return_stats=kick_return_stats, punt_return_stats=punt_return_stats,
                team_adv=team_adv, percentiles=percentiles, sp=sp,
                ap_rankings=ap_rankings, team_rank=team_rank,
                recruiting=recruiting, havoc=havoc, sp_trend=sp_trend)

@app.route('/api/players')
def api_players():
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify([])
    conn = get_db()
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
    conn.close()
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
def rankings():
    conn = get_db()
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
    conn.close()
    return render_template('rankings.html', rankings=rows, meta=meta)

@app.route('/game/<int:game_id>')
def game_detail(game_id):
    conn = get_db()
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
        conn.close()
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
    conn.close()

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
    cursor = conn.cursor()

    cursor.execute('''
        SELECT p.id, p.first_name, p.last_name, p.team, p.position, p.jersey,
               p.headshot, p.height, p.weight, p.year,
               t.logo_dark, t.color, t.alt_color, t.conference
        FROM players p
        LEFT JOIN teams t ON p.team = t.name
        WHERE p.id = %s
    ''', (player_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return render_template('404.html', message='Player not found.'), 404

    cursor.execute('SELECT active_2026, draft_status FROM players WHERE id=%s', (player_id,))
    status_row = cursor.fetchone()
    is_active_2026 = status_row[0] if status_row else 1
    draft_status   = status_row[1] if status_row else None

    c1 = row[11] or '#1a2a4a'
    c2 = row[12] or '#0a1220'
    h = int(row[7]) if row[7] else None
    year_raw = str(row[9]).strip() if row[9] is not None else ''
    print(f"Player year raw value: '{year_raw}' type: {type(row[9])}")
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

        def _rp(st, cat, grp, hb=True):
            return compute_rank_and_percentile(cursor, player_id, st, cat, grp, higher_better=hb)

        if pos in _pos_groups:
            group_name, gp = _pos_groups[pos]
            pool_size = 0

            if pos == 'QB':
                for rk, st, cat, pk in [
                    ('pass_yds_rank', 'YDS', 'passing',  'pass_yards'),
                    ('pass_td_rank',  'TD',  'passing',  'pass_td'),
                    ('pct_rank',      'PCT', 'passing',  'completion'),
                    ('ypa_rank',      'YPA', 'passing',  'yards_per_att'),
                ]:
                    r, p, n = _rp(st, cat, gp)
                    if r is not None: national_ranks[rk] = r
                    if p is not None: player_percentiles[pk] = p
                    pool_size = max(pool_size, n)
                r, p, n = _rp('INT', 'passing', gp, hb=False)
                if r is not None: national_ranks['int_rank'] = r
                for col, pk in [('avg_ppa_all','epa_per_play'),('avg_ppa_pass','epa_pass'),
                                  ('avg_ppa_rush','epa_rush'),('total_ppa','total_epa')]:
                    r, p, n = _rp(col, 'ppa', gp)
                    if col == 'avg_ppa_all' and r is not None: national_ranks['epa_rank'] = r
                    if p is not None: player_percentiles[pk] = p

            elif pos in ('RB','HB','FB'):
                for rk, st, cat, pk in [
                    ('rush_yds_rank', 'YDS', 'rushing', 'rush_yards'),
                    ('rush_td_rank',  'TD',  'rushing', 'rush_td'),
                ]:
                    r, p, n = _rp(st, cat, gp)
                    if r is not None: national_ranks[rk] = r
                    if p is not None: player_percentiles[pk] = p
                    pool_size = max(pool_size, n)
                r, p, n = _rp('YPC', 'rushing', gp)
                if r is not None: national_ranks['ypc_rank'] = r
                if p is not None: player_percentiles['yards_per_carry'] = p
                _, p, _ = _rp('YDS', 'receiving', ['WR','TE','RB','HB','FB'])
                if p is not None: player_percentiles['rec_yards'] = p
                for col, pk in [('avg_ppa_all','epa_per_play'),('total_ppa','total_epa')]:
                    r, p, n = _rp(col, 'ppa', gp)
                    if col == 'avg_ppa_all' and r is not None: national_ranks['epa_rank'] = r
                    if p is not None: player_percentiles[pk] = p

            elif pos in ('WR','TE'):
                for rk, st, cat, pk in [
                    ('rec_yds_rank', 'YDS', 'receiving', 'rec_yards'),
                    ('rec_td_rank',  'TD',  'receiving', 'rec_td'),
                    ('rec_rank',     'REC', 'receiving', 'receptions'),
                    ('ypr_rank',     'AVG', 'receiving', 'yards_per_rec'),
                ]:
                    r, p, n = _rp(st, cat, gp)
                    if r is not None: national_ranks[rk] = r
                    if p is not None: player_percentiles[pk] = p
                    pool_size = max(pool_size, n)
                for col, pk in [('avg_ppa_all','epa_per_play'),('total_ppa','total_epa')]:
                    r, p, n = _rp(col, 'ppa', gp)
                    if col == 'avg_ppa_all' and r is not None: national_ranks['epa_rank'] = r
                    if p is not None: player_percentiles[pk] = p
                print(f"Sanity — rec_yds rank={national_ranks.get('rec_yds_rank')} "
                      f"pct={player_percentiles.get('rec_yards')} pool={pool_size}")

            elif pos in ('DE','DT','NT','DL','EDGE'):
                dl_all = ['DE','DT','NT','DL','EDGE','LB','ILB','OLB','MLB']
                for rk, st, pk, grp2 in [
                    ('tackles_rank', 'TOT',   'tackles', dl_all),
                    ('sacks_rank',   'SACKS', 'sacks',   dl_all),
                ]:
                    r, p, n = _rp(st, 'defensive', grp2)
                    if r is not None: national_ranks[rk] = r
                    if p is not None: player_percentiles[pk] = p
                    pool_size = max(pool_size, n)
                _, p, _ = _rp('TFL', 'defensive', gp)
                if p is not None: player_percentiles['tfl'] = p
                for col, pk in [('avg_ppa_all','epa_per_play'),('total_ppa','total_epa')]:
                    r, p, n = _rp(col, 'ppa', gp)
                    if col == 'avg_ppa_all' and r is not None: national_ranks['epa_rank'] = r
                    if p is not None: player_percentiles[pk] = p

            elif pos in ('LB','ILB','OLB','MLB'):
                lb_all = ['DE','DT','NT','DL','EDGE','LB','ILB','OLB','MLB']
                for rk, st, pk in [
                    ('tackles_rank', 'TOT',   'tackles'),
                    ('sacks_rank',   'SACKS', 'sacks'),
                ]:
                    r, p, n = _rp(st, 'defensive', lb_all)
                    if r is not None: national_ranks[rk] = r
                    if p is not None: player_percentiles[pk] = p
                    pool_size = max(pool_size, n)
                _, p, _ = _rp('TFL', 'defensive', gp)
                if p is not None: player_percentiles['tfl'] = p
                for col, pk in [('avg_ppa_all','epa_per_play'),('total_ppa','total_epa')]:
                    r, p, n = _rp(col, 'ppa', gp)
                    if col == 'avg_ppa_all' and r is not None: national_ranks['epa_rank'] = r
                    if p is not None: player_percentiles[pk] = p

            elif pos in ('CB','S','SS','FS','SAF','DB'):
                r, p, n = _rp('TOT', 'defensive', gp)
                if r is not None: national_ranks['tackles_rank'] = r
                if p is not None: player_percentiles['tackles'] = p
                pool_size = n
                _, p, _ = _rp('INT', 'defensive', gp)
                if p is not None: player_percentiles['interceptions'] = p
                _, p, _ = _rp('PD',  'defensive', gp)
                if p is not None: player_percentiles['pd'] = p
                for col, pk in [('avg_ppa_all','epa_per_play'),('total_ppa','total_epa')]:
                    r, p, n = _rp(col, 'ppa', gp)
                    if col == 'avg_ppa_all' and r is not None: national_ranks['epa_rank'] = r
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

    conn.close()

    game_log = []
    try:
        # Get completed games for this team from DB (includes opponent/result info)
        conn2 = get_db()
        cur2 = conn2.cursor()
        cur2.execute('''
            SELECT g.id, g.week,
                   CASE WHEN g.home_team=%s THEN g.away_team ELSE g.home_team END,
                   CASE WHEN g.home_team=%s THEN g.home_points ELSE g.away_points END,
                   CASE WHEN g.home_team=%s THEN g.away_points ELSE g.home_points END,
                   CASE WHEN g.home_team=%s THEN 'home' ELSE 'away' END,
                   CASE WHEN g.home_team=%s THEN t2.logo_dark ELSE t1.logo_dark END
            FROM games g
            LEFT JOIN teams t1 ON g.home_team = t1.name
            LEFT JOIN teams t2 ON g.away_team = t2.name
            WHERE (g.home_team=%s OR g.away_team=%s) AND g.completed=1
            ORDER BY g.week
        ''', (player['team'],) * 7)
        games_list = cur2.fetchall()
        conn2.close()

        # Find ESPN team ID + athlete ID by scanning one game's boxscore
        search_name = f"{player['first_name']} {player['last_name']}"
        espn_team_id = None
        espn_athlete_id = None

        for game_row in games_list[:3]:
            game_id = game_row[0]
            try:
                r = req.get(
                    'https://site.api.espn.com/apis/site/v2/sports/football/college-football/summary',
                    params={'event': game_id}, timeout=6
                )
                data = r.json()
                for bp in (data.get('boxscore') or {}).get('players', []):
                    t_obj = bp.get('team', {}) or {}
                    t_name = t_obj.get('displayName', '')
                    tn_l, at_l = t_name.lower(), player['team'].lower()
                    if not (tn_l == at_l or at_l in tn_l or tn_l in at_l
                            or any(w in tn_l for w in at_l.split() if len(w) >= 4)):
                        continue
                    espn_team_id = str(t_obj.get('id', ''))
                    for stat_cat in (bp.get('statistics') or []):
                        for ae in (stat_cat.get('athletes') or []):
                            ath = ae.get('athlete', {}) or {}
                            if ath.get('displayName', '').lower() == search_name.lower():
                                espn_athlete_id = str(ath.get('id', ''))
                                break
                        if espn_athlete_id:
                            break
                    if espn_athlete_id:
                        break
            except Exception:
                pass
            if espn_athlete_id:
                break

        if espn_team_id and espn_athlete_id:
            for game_row in games_list:
                game_id, week, opp, my_pts, opp_pts, ha, opp_logo = game_row
                try:
                    r = req.get(
                        f'https://sports.core.api.espn.com/v2/sports/football/leagues/'
                        f'college-football/events/{game_id}/competitions/{game_id}'
                        f'/competitors/{espn_team_id}/roster/{espn_athlete_id}/statistics/0',
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

    conn.close()
    return render_template('transfers.html', portal=portal, year=year,
                           conferences=conferences, positions=positions,
                           pos_filter=pos_filter, conf_filter=conf_filter,
                           page=page, total_pages=total_pages, total_count=total_count,
                           per_page=per_page)


@app.route('/rivalries')
def rivalries_page():
    conn = get_db()
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

    conn.close()
    return render_template('rivalries.html', rivalries=rivalry_data)


@app.route('/rivalry/<team_a>/<team_b>')
def rivalry_history(team_a, team_b):
    conn = get_db()
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

    conn.close()

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


if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5001))
    app.run(debug=True, host='0.0.0.0', port=port)