"""Bulk-populate player_game_logs from CFBD's per-game player stats.

The player page's game log was derived live from ESPN (~13 HTTP calls per
player) and persisted only on first view — 424 of ~90k player-seasons stored,
so nearly every player page paid an 8-9s first load. CFBD's
get_game_player_stats(year, week) returns EVERY player's stats for EVERY game
of that week: ~17 calls per season covers the whole player base.

CFBD stat names are mapped onto the ESPN keys the template already reads
(passing C/ATT is split into completions/attempts, completion % and NCAA
passer rating are computed from components, defensive AST = TOT - SOLO).
Columns with no CFBD equivalent (targets, forced fumbles, net punt average)
stay absent and render as the template's existing em-dash.

Existing rows are only overwritten with richer data (never the ESPN-derived
logs, which are kept when longer). The live ESPN path in main.py remains the
fallback for anything not stored.

Usage:  python3 backfill_game_logs.py            # all seasons
        python3 backfill_game_logs.py 2019       # one season
"""
import os
import sys
import json
import cfbd
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=True)

# CFBD (category, stat type) -> template stat key
STAT_MAP = {
    ('passing', 'YDS'): 'passingYards', ('passing', 'TD'): 'passingTouchdowns',
    ('passing', 'INT'): 'interceptions', ('passing', 'AVG'): 'yardsPerPassAttempt',
    ('rushing', 'CAR'): 'rushingAttempts', ('rushing', 'YDS'): 'rushingYards',
    ('rushing', 'AVG'): 'yardsPerRushAttempt', ('rushing', 'TD'): 'rushingTouchdowns',
    ('rushing', 'LONG'): 'longRushing',
    ('receiving', 'REC'): 'receptions', ('receiving', 'YDS'): 'receivingYards',
    ('receiving', 'AVG'): 'yardsPerReception', ('receiving', 'TD'): 'receivingTouchdowns',
    ('receiving', 'LONG'): 'longReception',
    ('defensive', 'TOT'): 'totalTackles', ('defensive', 'SOLO'): 'soloTackles',
    ('defensive', 'SACKS'): 'sacks', ('defensive', 'TFL'): 'tacklesForLoss',
    ('defensive', 'PD'): 'passesDefended', ('defensive', 'TD'): 'defensiveTouchdowns',
    ('interceptions', 'INT'): 'interceptions', ('interceptions', 'TD'): 'interceptionTouchdowns',
    ('kicking', 'PCT'): 'fieldGoalPct', ('kicking', 'LONG'): 'longFieldGoal',
    ('kicking', 'PTS'): 'totalKickingPoints',
    ('punting', 'NO'): 'punts', ('punting', 'YDS'): 'puntYards',
    ('punting', 'YPP'): 'grossAvgPuntYards', ('punting', 'AVG'): 'grossAvgPuntYards',
    ('punting', 'LONG'): 'longPunt', ('punting', 'In 20'): 'puntsInsideTwenty',
    ('punting', 'TB'): 'touchbacks',
}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def finalize_stats(raw):
    """raw: {(category, type): value-string} for one player in one game."""
    out = {}
    for (cat, st), val in raw.items():
        if (cat, st) == ('passing', 'C/ATT') and '/' in str(val):
            c, a = str(val).split('/', 1)
            out['completions'] = _num(c)
            out['passingAttempts'] = _num(a)
            continue
        if (cat, st) == ('kicking', 'FG') and '/' in str(val):
            m, a = str(val).split('/', 1)
            out['fieldGoalsMade'] = _num(m)
            out['fieldGoalAttempts'] = _num(a)
            continue
        if (cat, st) == ('kicking', 'XP') and '/' in str(val):
            m, a = str(val).split('/', 1)
            out['extraPointsMade'] = _num(m)
            out['extraPointAttempts'] = _num(a)
            continue
        key = STAT_MAP.get((cat, st))
        if key:
            out[key] = _num(val)
    # defensive assists + passing derivations, matching the season-stats conventions
    if out.get('totalTackles') is not None and out.get('soloTackles') is not None:
        out['assistTackles'] = out['totalTackles'] - out['soloTackles']
    cmp_, att = out.get('completions'), out.get('passingAttempts')
    if att:
        out['completionPct'] = round(cmp_ / att * 100, 1) if cmp_ is not None else None
        yds = out.get('passingYards') or 0
        td = out.get('passingTouchdowns') or 0
        i = out.get('interceptions') or 0
        out['QBRating'] = round((8.4 * yds + 330 * td + 100 * (cmp_ or 0) - 200 * i) / att, 1)
    return {k: v for k, v in out.items() if v is not None}


def backfill_season(apis, cur, conn, season):
    # Game metadata (opponent/result/labels) from our games table
    cur.execute('''
        SELECT g.id, g.week, g.season_type, g.home_team, g.away_team,
               g.home_points, g.away_points, g.notes, g.start_date,
               th.logo_dark, ta.logo_dark
        FROM games g
        LEFT JOIN teams th ON th.name = g.home_team
        LEFT JOIN teams ta ON ta.name = g.away_team
        WHERE g.season = %s AND g.completed = 1
    ''', (season,))
    meta = {r[0]: r for r in cur.fetchall()}

    cur.execute('''SELECT DISTINCT week, season_type FROM games
                   WHERE season = %s AND completed = 1''', (season,))
    week_rows = cur.fetchall()

    import main as _m  # shorten_game_label lives in main
    logs = {}  # player_id -> [(start_date, entry)]

    for week, stype in sorted(week_rows, key=lambda r: (('POSTSEASON' in r[1]), r[0])):
        st = 'postseason' if 'POSTSEASON' in (stype or '') else 'regular'
        try:
            games = apis.get_game_player_stats(year=season, week=week, season_type=st)
        except Exception as e:
            print(f"  {season} wk{week} {st}: {type(e).__name__} {str(e)[:70]}", flush=True)
            continue
        for gm in games:
            m = meta.get(gm.id)
            if not m:
                continue
            (_gid, g_week, g_stype, home, away, hp, ap, notes, start,
             home_logo, away_logo) = m
            for tm in gm.teams:
                my_team = tm.team
                if my_team == home:
                    opp, opp_logo, ha, my_pts, opp_pts = away, away_logo, 'home', hp, ap
                elif my_team == away:
                    opp, opp_logo, ha, my_pts, opp_pts = home, home_logo, 'away', ap, hp
                else:
                    continue
                if my_pts is not None and opp_pts is not None:
                    if my_pts > opp_pts:   result = f"W {int(my_pts)}-{int(opp_pts)}"
                    elif my_pts < opp_pts: result = f"L {int(opp_pts)}-{int(my_pts)}"
                    else:                  result = f"T {int(my_pts)}-{int(opp_pts)}"
                else:
                    result = ''
                per_player = {}
                for cat in (tm.categories or []):
                    for typ in (cat.types or []):
                        for ath in (typ.athletes or []):
                            if not ath.id or not str(ath.id).lstrip('-').isdigit():
                                continue
                            pid = int(ath.id)
                            if pid <= 0:
                                continue
                            per_player.setdefault(pid, {})[(cat.name, typ.name)] = ath.stat
                for pid, raw in per_player.items():
                    stats = finalize_stats(raw)
                    if not stats:
                        continue
                    entry = {
                        'week': g_week, 'game_id': gm.id, 'opponent': opp or '',
                        'opp_logo': opp_logo or '', 'home_away': ha, 'result': result,
                        'team': my_team, 'season_type': g_stype,
                        'game_label': _m.shorten_game_label(g_stype, g_week, notes),
                        'stats': stats,
                    }
                    logs.setdefault(pid, []).append((str(start or ''), entry))

    rows = []
    for pid, entries in logs.items():
        entries.sort(key=lambda e: e[0])
        rows.append((pid, season, json.dumps([e[1] for e in entries])))

    # Keep any existing (ESPN-derived) log that has MORE games than ours —
    # ESPN entries carry a few extra stat columns, so don't degrade them.
    execute_values(cur, '''
        INSERT INTO player_game_logs (player_id, season, log)
        VALUES %s
        ON CONFLICT (player_id, season) DO UPDATE SET
            log = EXCLUDED.log, updated_at = now()
        WHERE COALESCE(json_array_length(player_game_logs.log::json), 0)
              < json_array_length(EXCLUDED.log::json)
    ''', rows, page_size=500)
    conn.commit()
    print(f"{season}: logs for {len(rows)} players "
          f"({len(week_rows)} week-fetches)", flush=True)


def run():
    seasons = [int(a) for a in sys.argv[1:]]
    conn = psycopg2.connect(os.getenv('DATABASE_URL'))
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS player_game_logs (
            player_id INTEGER NOT NULL,
            season    INTEGER NOT NULL,
            log       TEXT,
            updated_at TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (player_id, season)
        )
    ''')
    conn.commit()
    if not seasons:
        cur.execute('SELECT DISTINCT season FROM games WHERE completed = 1 ORDER BY season')
        seasons = [r[0] for r in cur.fetchall()]
    cfg = cfbd.Configuration(access_token=os.getenv('CFBD_API_KEY'))
    with cfbd.ApiClient(cfg) as api:
        games_api = cfbd.GamesApi(api)
        for y in seasons:
            backfill_season(games_api, cur, conn, y)
    conn.close()
    print("game-log backfill complete", flush=True)


if __name__ == '__main__':
    run()
