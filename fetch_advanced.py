import cfbd
import psycopg2
import os
from dotenv import load_dotenv

load_dotenv()

configuration = cfbd.Configuration(access_token=os.getenv("CFBD_API_KEY"))
conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cursor = conn.cursor()

with cfbd.ApiClient(configuration) as api_client:
    stats_api      = cfbd.StatsApi(api_client)
    players_api    = cfbd.PlayersApi(api_client)
    recruiting_api = cfbd.RecruitingApi(api_client)
    ratings_api    = cfbd.RatingsApi(api_client)

    # ── 1. TEAM ADVANCED STATS ────────────────────────────────────────────────
    print("Fetching team advanced stats...")
    try:
        adv = stats_api.get_advanced_season_stats(year=2025, exclude_garbage_time=True)
        cursor.execute('DELETE FROM team_advanced')
        saved = 0
        for s in adv:
            o = s.offense
            d = s.defense
            if not o and not d:
                continue

            off_havoc = getattr(o, 'havoc', None)         if o else None
            def_havoc = getattr(d, 'havoc', None)         if d else None
            off_fp    = getattr(o, 'field_position', None) if o else None
            def_fp    = getattr(d, 'field_position', None) if d else None
            off_pd    = getattr(o, 'passing_downs', None)  if o else None
            off_sd    = getattr(o, 'standard_downs', None) if o else None

            cursor.execute('''
                INSERT INTO team_advanced VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                )
            ''', (
                s.team,
                getattr(o, 'ppa', None)                    if o else None,
                getattr(o, 'success_rate', None)            if o else None,
                getattr(o, 'explosiveness', None)           if o else None,
                getattr(off_pd, 'ppa', None)                if off_pd else None,
                getattr(off_sd, 'ppa', None)                if off_sd else None,
                getattr(off_fp, 'average_start', None)      if off_fp else None,
                getattr(o, 'total_opportunies', None)       if o else None,
                getattr(o, 'points_per_opportunity', None)  if o else None,
                getattr(o, 'total_ppa', None)               if o else None,
                getattr(d, 'ppa', None)                    if d else None,
                getattr(d, 'success_rate', None)            if d else None,
                getattr(d, 'explosiveness', None)           if d else None,
                getattr(def_havoc, 'total', None)           if def_havoc else None,
                getattr(def_havoc, 'front_seven', None)     if def_havoc else None,
                getattr(def_havoc, 'db', None)              if def_havoc else None,
                getattr(def_fp, 'average_start', None)      if def_fp else None,
                getattr(d, 'total_opportunies', None)       if d else None,
                getattr(d, 'points_per_opportunity', None)  if d else None,
            ))
            saved += 1

        conn.commit()
        print(f"  Saved {saved} team advanced records")
    except Exception as e:
        conn.rollback()
        print(f"  Error: {e}")
        import traceback; traceback.print_exc()

    # ── 2. PLAYER USAGE STATS ─────────────────────────────────────────────────
    print("\nFetching player usage stats...")
    try:
        usage = players_api.get_player_usage(year=2025)
        saved = 0
        for u in usage:
            pid = getattr(u, 'id', None)
            ud  = getattr(u, 'usage', None)
            if not ud:
                continue
            cursor.execute('''
                INSERT INTO player_usage VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (player_id) DO UPDATE SET
                    player_name=EXCLUDED.player_name, team=EXCLUDED.team,
                    position=EXCLUDED.position, season=EXCLUDED.season,
                    overall=EXCLUDED.overall, pass=EXCLUDED.pass,
                    rush=EXCLUDED.rush, first_down=EXCLUDED.first_down,
                    second_down=EXCLUDED.second_down, third_down=EXCLUDED.third_down,
                    standard_downs=EXCLUDED.standard_downs,
                    passing_downs=EXCLUDED.passing_downs
            ''', (
                pid, u.name, u.team, u.position, 2025,
                getattr(ud, 'overall', None),
                getattr(ud, 'var_pass', None),
                getattr(ud, 'rush', None),
                getattr(ud, 'first_down', None),
                getattr(ud, 'second_down', None),
                getattr(ud, 'third_down', None),
                getattr(ud, 'standard_downs', None),
                getattr(ud, 'passing_downs', None),
            ))
            saved += 1
        conn.commit()
        print(f"  Saved {saved} player usage records")

        cursor.execute('''
            SELECT player_name, team, position, overall
            FROM player_usage ORDER BY overall DESC LIMIT 5
        ''')
        for r in cursor.fetchall():
            print(f"    {r[0]} ({r[2]}, {r[1]}): {r[3]:.3f} overall usage")
    except Exception as e:
        conn.rollback()
        print(f"  Error: {e}")
        import traceback; traceback.print_exc()

    # ── 3. RECRUITING RANKINGS (multi-year) ───────────────────────────────────
    print("\nFetching recruiting rankings...")
    try:
        for yr in [2022, 2023, 2024, 2025, 2026]:
            try:
                teams_rec = recruiting_api.get_team_recruiting_rankings(year=yr)
                cursor.execute('DELETE FROM team_recruiting WHERE year=%s', (yr,))
                for r in teams_rec:
                    cursor.execute('''
                        INSERT INTO team_recruiting VALUES (%s, %s, %s, %s)
                    ''', (r.team, yr, getattr(r, 'rank', None), getattr(r, 'points', None)))
                conn.commit()
                print(f"  Recruiting {yr}: {len(teams_rec)} teams")
            except Exception as e:
                conn.rollback()
                print(f"  Recruiting {yr} error: {e}")
    except Exception as e:
        print(f"  Error: {e}")

    # ── 4. HISTORICAL SP+ (5 years) ───────────────────────────────────────────
    print("\nFetching historical SP+...")
    try:
        for yr in [2021, 2022, 2023, 2024, 2025]:
            try:
                sp = ratings_api.get_sp(year=yr)
                cursor.execute('DELETE FROM sp_historical WHERE year=%s', (yr,))
                for s in sp:
                    off  = getattr(s, 'offense', None)
                    def_ = getattr(s, 'defense', None)
                    st   = getattr(s, 'special_teams', None)
                    cursor.execute('''
                        INSERT INTO sp_historical VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ''', (
                        s.team, yr,
                        getattr(s, 'rating', None),    getattr(s, 'ranking', None),
                        getattr(off, 'rating', None)   if off  else None,
                        getattr(off, 'ranking', None)  if off  else None,
                        getattr(def_, 'rating', None)  if def_ else None,
                        getattr(def_, 'ranking', None) if def_ else None,
                        getattr(st, 'rating', None)    if st   else None,
                    ))
                conn.commit()
                print(f"  SP+ {yr}: {len(sp)} teams")
            except Exception as e:
                conn.rollback()
                print(f"  SP+ {yr} error: {e}")
    except Exception as e:
        print(f"  Error: {e}")

conn.close()
print("\nDone!")
