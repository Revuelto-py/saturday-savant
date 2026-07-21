"""Phase 1a: backfill historical seasons (2016-2024) from CFBD.

Usage:
    python3 backfill_history.py                 # all of 2016-2024
    python3 backfill_history.py 2019 2020       # specific seasons

Loads, per season (~11 CFBD calls each):
    games (regular + postseason, FBS home)      -> games
    player season stats (regular)               -> player_stats(season)
    player PPA                                  -> player_ppa(season)
    advanced team stats (garbage time excluded) -> team_stats(season) + team_advanced(season)
    player usage                                -> player_usage(season)
    SP+                                         -> sp_ratings(season)
    final AP poll (postseason if available)     -> ap_rankings(season)
    recruiting class ranks                      -> team_recruiting(year)

Every insert mirrors the corresponding current-season fetch script's shape so
historical rows are indistinguishable from 2025's, just with a different
season value. Each table is refreshed with DELETE-that-season-then-insert, so
the script is idempotent and never touches other seasons. 2025/2026 are
refused as targets — they are owned by the existing fetch scripts.

One-time schema fixes handled here (idempotent): player_usage and
savant_ratings primary keys widen to include season so multiple years can
coexist ((player_id) -> (player_id, season), (team) -> (team, season)).
"""
import sys
import cfbd
import psycopg2
from psycopg2.extras import execute_values
import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=True)

PROTECTED_SEASONS = (2025, 2026)  # owned by the current-season fetch scripts

configuration = cfbd.Configuration(access_token=os.getenv("CFBD_API_KEY"))
conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cursor = conn.cursor()


def widen_pk(table, old_cols, new_cols):
    """Idempotently replace a primary key (e.g. (player_id) -> (player_id, season))."""
    cursor.execute('''
        SELECT string_agg(kcu.column_name, ',' ORDER BY kcu.ordinal_position)
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name
        WHERE tc.table_name = %s AND tc.constraint_type = 'PRIMARY KEY'
    ''', (table,))
    row = cursor.fetchone()
    current = row[0] if row else None
    if current == ','.join(new_cols):
        return
    if current == ','.join(old_cols):
        cursor.execute(f'ALTER TABLE {table} DROP CONSTRAINT {table}_pkey')
    cursor.execute(f"ALTER TABLE {table} ADD PRIMARY KEY ({', '.join(new_cols)})")
    conn.commit()
    print(f"{table}: primary key widened to ({', '.join(new_cols)})")


widen_pk('player_usage', ['player_id'], ['player_id', 'season'])
widen_pk('savant_ratings', ['team'], ['team', 'season'])


def _refresh(table, season, season_col='season'):
    cursor.execute(f'DELETE FROM {table} WHERE {season_col} = %s', (season,))


def backfill_games(apis, y):
    games_api = apis['games']
    all_games = []
    for st in ('regular', 'postseason'):
        try:
            all_games.extend(games_api.get_games(y, season_type=st))
        except Exception as e:
            print(f"  games {st}: {type(e).__name__}: {str(e)[:100]}")
    fbs = [g for g in all_games if g.home_classification == 'fbs']
    _refresh('games', y)
    execute_values(cursor, '''
        INSERT INTO games (id, season, week, season_type, home_team, home_points,
                           away_team, away_points, completed, start_date, notes, start_time_tbd)
        VALUES %s
        ON CONFLICT (id) DO UPDATE SET
            season=EXCLUDED.season, week=EXCLUDED.week, season_type=EXCLUDED.season_type,
            home_team=EXCLUDED.home_team, home_points=EXCLUDED.home_points,
            away_team=EXCLUDED.away_team, away_points=EXCLUDED.away_points,
            completed=EXCLUDED.completed, start_date=EXCLUDED.start_date,
            notes=EXCLUDED.notes, start_time_tbd=EXCLUDED.start_time_tbd
    ''', [(g.id, g.season, g.week, str(g.season_type), g.home_team, g.home_points,
           g.away_team, g.away_points, 1 if g.completed else 0,
           str(g.start_date) if g.start_date else None, g.notes,
           1 if getattr(g, 'start_time_tbd', False) else 0) for g in fbs],
        page_size=500)
    conn.commit()
    print(f"  games: {len(fbs)} (of {len(all_games)})")


def backfill_player_stats(apis, y):
    stats = apis['stats'].get_player_season_stats(year=y, season_type='regular')
    _refresh('player_stats', y)
    execute_values(cursor, '''
        INSERT INTO player_stats (player_id, player_name, team, conference, position,
                                  category, stat_type, stat, season)
        VALUES %s
    ''', [(s.player_id, s.player, s.team, s.conference, s.position,
           s.category, s.stat_type, s.stat, y) for s in stats],
        page_size=2000)
    conn.commit()
    print(f"  player_stats: {len(stats)}")


def backfill_player_ppa(apis, y):
    ppa = apis['metrics'].get_predicted_points_added_by_player_season(year=y)
    _refresh('player_ppa', y)
    execute_values(cursor, '''
        INSERT INTO player_ppa (player_id, player_name, position, team, conference,
                                avg_ppa_all, avg_ppa_pass, avg_ppa_rush, total_ppa, season)
        VALUES %s
        ON CONFLICT (player_id, season) DO NOTHING
    ''', [(p.id, p.name, p.position, p.team, p.conference,
           p.average_ppa.all, p.average_ppa.var_pass, p.average_ppa.rush,
           p.total_ppa.all, y) for p in ppa],
        page_size=1000)
    conn.commit()
    print(f"  player_ppa: {len(ppa)}")


def backfill_team_stats(apis, y):
    advanced = apis['stats'].get_advanced_season_stats(year=y, exclude_garbage_time=True)
    _refresh('team_stats', y)
    _refresh('team_advanced', y)
    n = 0
    for s in advanced:
        o, d = s.offense, s.defense
        if not o and not d:
            continue
        # team_stats — 35 legacy columns + season (mirrors fetch_team_stats.py)
        cursor.execute('''
            INSERT INTO team_stats VALUES (
                %s,
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                %s
            )
        ''', (
            s.team,
            getattr(o, 'plays', None), getattr(o, 'drives', None),
            getattr(o, 'ppa', None), getattr(o, 'total_ppa', None),
            getattr(o, 'success_rate', None), getattr(o, 'explosiveness', None),
            getattr(o, 'power_success', None), getattr(o, 'stuff_rate', None),
            getattr(o, 'line_yards', None), getattr(o, 'open_field_yards', None),
            getattr(o, 'second_level_yards', None),
            getattr(o, 'rushing_plays', None) and getattr(o.rushing_plays, 'ppa', None),
            getattr(o, 'passing_plays', None) and getattr(o.passing_plays, 'ppa', None),
            getattr(o, 'rushing_plays', None) and getattr(o.rushing_plays, 'success_rate', None),
            getattr(o, 'passing_plays', None) and getattr(o.passing_plays, 'success_rate', None),
            getattr(o, 'rushing_plays', None) and getattr(o.rushing_plays, 'explosiveness', None),
            getattr(o, 'passing_plays', None) and getattr(o.passing_plays, 'explosiveness', None),
            getattr(d, 'plays', None), getattr(d, 'drives', None),
            getattr(d, 'ppa', None), getattr(d, 'total_ppa', None),
            getattr(d, 'success_rate', None), getattr(d, 'explosiveness', None),
            getattr(d, 'power_success', None), getattr(d, 'stuff_rate', None),
            getattr(d, 'line_yards', None), getattr(d, 'open_field_yards', None),
            getattr(d, 'second_level_yards', None),
            getattr(d, 'rushing_plays', None) and getattr(d.rushing_plays, 'ppa', None),
            getattr(d, 'passing_plays', None) and getattr(d.passing_plays, 'ppa', None),
            getattr(d, 'rushing_plays', None) and getattr(d.rushing_plays, 'success_rate', None),
            getattr(d, 'passing_plays', None) and getattr(d.passing_plays, 'success_rate', None),
            getattr(d, 'rushing_plays', None) and getattr(d.rushing_plays, 'explosiveness', None),
            getattr(d, 'passing_plays', None) and getattr(d.passing_plays, 'explosiveness', None),
            y,
        ))
        # team_advanced — 19 legacy columns + season (mirrors fetch_advanced.py)
        off_havoc = getattr(o, 'havoc', None) if o else None
        def_havoc = getattr(d, 'havoc', None) if d else None
        off_fp = getattr(o, 'field_position', None) if o else None
        def_fp = getattr(d, 'field_position', None) if d else None
        off_pd = getattr(o, 'passing_downs', None) if o else None
        off_sd = getattr(o, 'standard_downs', None) if o else None
        cursor.execute('''
            INSERT INTO team_advanced VALUES (
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
            )
        ''', (
            s.team,
            getattr(o, 'ppa', None) if o else None,
            getattr(o, 'success_rate', None) if o else None,
            getattr(o, 'explosiveness', None) if o else None,
            getattr(off_pd, 'ppa', None) if off_pd else None,
            getattr(off_sd, 'ppa', None) if off_sd else None,
            getattr(off_fp, 'average_start', None) if off_fp else None,
            getattr(o, 'total_opportunies', None) if o else None,
            getattr(o, 'points_per_opportunity', None) if o else None,
            getattr(o, 'total_ppa', None) if o else None,
            getattr(d, 'ppa', None) if d else None,
            getattr(d, 'success_rate', None) if d else None,
            getattr(d, 'explosiveness', None) if d else None,
            getattr(def_havoc, 'total', None) if def_havoc else None,
            getattr(def_havoc, 'front_seven', None) if def_havoc else None,
            getattr(def_havoc, 'db', None) if def_havoc else None,
            getattr(def_fp, 'average_start', None) if def_fp else None,
            getattr(d, 'total_opportunies', None) if d else None,
            getattr(d, 'points_per_opportunity', None) if d else None,
            y,
        ))
        n += 1
    conn.commit()
    print(f"  team_stats + team_advanced: {n}")


def backfill_usage(apis, y):
    usage = apis['players'].get_player_usage(year=y)
    _refresh('player_usage', y)
    rows, seen = [], set()
    for u in usage:
        pid = getattr(u, 'id', None)
        ud = getattr(u, 'usage', None)
        if not ud or pid is None or pid in seen:
            continue
        seen.add(pid)
        rows.append((
            pid, u.name, u.team, u.position, y,
            getattr(ud, 'overall', None), getattr(ud, 'var_pass', None),
            getattr(ud, 'rush', None), getattr(ud, 'first_down', None),
            getattr(ud, 'second_down', None), getattr(ud, 'third_down', None),
            getattr(ud, 'standard_downs', None), getattr(ud, 'passing_downs', None),
        ))
    execute_values(cursor, '''
        INSERT INTO player_usage VALUES %s
        ON CONFLICT (player_id, season) DO UPDATE SET
            player_name=EXCLUDED.player_name, team=EXCLUDED.team,
            position=EXCLUDED.position,
            overall=EXCLUDED.overall, pass=EXCLUDED.pass,
            rush=EXCLUDED.rush, first_down=EXCLUDED.first_down,
            second_down=EXCLUDED.second_down, third_down=EXCLUDED.third_down,
            standard_downs=EXCLUDED.standard_downs, passing_downs=EXCLUDED.passing_downs
    ''', rows, page_size=1000)
    conn.commit()
    print(f"  player_usage: {len(rows)}")


def backfill_sp(apis, y):
    sp = apis['ratings'].get_sp(year=y)
    _refresh('sp_ratings', y)
    for s in sp:
        off = getattr(s, 'offense', None)
        def_ = getattr(s, 'defense', None)
        st = getattr(s, 'special_teams', None)
        cursor.execute('''
            INSERT INTO sp_ratings VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ''', (
            s.team, getattr(s, 'rating', None), getattr(s, 'ranking', None),
            getattr(off, 'rating', None) if off else None,
            getattr(off, 'ranking', None) if off else None,
            getattr(def_, 'rating', None) if def_ else None,
            getattr(def_, 'ranking', None) if def_ else None,
            getattr(st, 'rating', None) if st else None,
            y,
        ))
    conn.commit()
    print(f"  sp_ratings: {len(sp)}")


def backfill_ap(apis, y):
    """Final AP poll of the season (postseason snapshot if published, else the
    last regular-season week), with prev_rank from the poll before it — the
    same one-snapshot-per-season convention fetch_rankings.py uses for 2025."""
    rankings = apis['rankings'].get_rankings(year=y)
    ap_weeks = []
    for wk in rankings:
        for poll in wk.polls:
            if poll.poll == 'AP Top 25':
                stype = str(wk.season_type) if wk.season_type else ''
                sort_val = (0 if 'post' in stype.lower() else 1, -wk.week)
                ap_weeks.append((sort_val, wk.week, stype, poll.ranks))
    ap_weeks.sort(key=lambda x: x[0])
    if not ap_weeks:
        print(f"  ap_rankings: none found")
        return
    _, cur_week, cur_type, cur_ranks = ap_weeks[0]
    prev_map = {}
    if len(ap_weeks) > 1:
        for r in ap_weeks[1][3]:
            prev_map[r.school] = r.rank
    season_type = 'postseason' if 'post' in cur_type.lower() else 'regular'
    _refresh('ap_rankings', y)
    for r in cur_ranks:
        cursor.execute('''
            INSERT INTO ap_rankings
            (team, rank, points, first_place_votes, week, season, prev_rank, season_type)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ''', (r.school, r.rank, getattr(r, 'points', None),
              getattr(r, 'first_place_votes', None), cur_week, y,
              prev_map.get(r.school), season_type))
    conn.commit()
    print(f"  ap_rankings: {len(cur_ranks)} (week {cur_week} {season_type})")


def backfill_recruiting(apis, y):
    try:
        rec = apis['recruiting'].get_team_recruiting_rankings(year=y)
    except Exception as e:
        print(f"  team_recruiting: {type(e).__name__}: {str(e)[:80]}")
        return
    _refresh('team_recruiting', y, season_col='year')
    for r in rec:
        cursor.execute('INSERT INTO team_recruiting VALUES (%s,%s,%s,%s)',
                       (r.team, y, getattr(r, 'rank', None), getattr(r, 'points', None)))
    conn.commit()
    print(f"  team_recruiting: {len(rec)}")


def main():
    seasons = [int(a) for a in sys.argv[1:]] or list(range(2016, 2025))
    bad = [y for y in seasons if y in PROTECTED_SEASONS]
    if bad:
        raise SystemExit(f"Refusing to backfill protected season(s) {bad} — "
                         f"those are owned by the current-season fetch scripts.")

    with cfbd.ApiClient(configuration) as api_client:
        apis = {
            'games': cfbd.GamesApi(api_client),
            'stats': cfbd.StatsApi(api_client),
            'metrics': cfbd.MetricsApi(api_client),
            'players': cfbd.PlayersApi(api_client),
            'ratings': cfbd.RatingsApi(api_client),
            'rankings': cfbd.RankingsApi(api_client),
            'recruiting': cfbd.RecruitingApi(api_client),
        }
        for y in seasons:
            print(f"=== {y} ===")
            backfill_games(apis, y)
            backfill_player_stats(apis, y)
            backfill_player_ppa(apis, y)
            backfill_team_stats(apis, y)
            backfill_usage(apis, y)
            backfill_sp(apis, y)
            backfill_ap(apis, y)
            backfill_recruiting(apis, y)

    conn.close()
    print("\nBackfill complete.")


if __name__ == '__main__':
    main()


# Data changed — tell the live site to drop its in-memory page cache so the
# update is visible immediately instead of after the cache TTL.
try:
    from cache_notify import notify_cache_clear
    notify_cache_clear()
except Exception:
    pass
