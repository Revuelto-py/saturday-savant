"""One-time fetch of candidate Savant Forecast feature sources.

Populates four small tables used only by the model-exploration pipeline:
  venues        — stadium coordinates/elevation/dome (travel distance, altitude)
  game_weather  — per-game temperature/wind/precipitation (CFBD weather feed)
  team_talent   — 247-composite roster talent per team-season
  coaches       — head coach per team-season (first-year-coach flag)

Also backfills betting_lines.spread_open so opening-vs-closing line movement
can be evaluated as a feature (the closing line itself stays reserved as the
evaluation baseline).

Budget: ~1 + 10 + 10 + 10 + 10 = ~41 CFBD calls, one time.
Usage:  python3 fetch_forecast_extras.py [first_season] [last_season]
"""
import os
import sys

import cfbd
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=True)

FIRST = int(sys.argv[1]) if len(sys.argv) > 2 else 2016
LAST = int(sys.argv[2]) if len(sys.argv) > 2 else 2025

cfg = cfbd.Configuration(access_token=os.getenv("CFBD_API_KEY"))
conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cur = conn.cursor()

cur.execute('''
    CREATE TABLE IF NOT EXISTS venues (
        id INTEGER PRIMARY KEY, name TEXT, city TEXT, state TEXT,
        latitude REAL, longitude REAL, elevation REAL,
        dome INTEGER, capacity INTEGER, timezone TEXT)''')
cur.execute('''
    CREATE TABLE IF NOT EXISTS game_weather (
        game_id BIGINT PRIMARY KEY, season INTEGER, week INTEGER,
        venue_id INTEGER, game_indoors INTEGER,
        temperature REAL, dew_point REAL, humidity REAL,
        precipitation REAL, snowfall REAL, wind_speed REAL,
        wind_direction REAL, pressure REAL, weather_condition TEXT)''')
cur.execute('''
    CREATE TABLE IF NOT EXISTS team_talent (
        team TEXT, season INTEGER, talent REAL, PRIMARY KEY (team, season))''')
cur.execute('''
    CREATE TABLE IF NOT EXISTS coaches (
        team TEXT, season INTEGER, coach TEXT, hire_date TEXT,
        PRIMARY KEY (team, season))''')
cur.execute('ALTER TABLE betting_lines ADD COLUMN IF NOT EXISTS spread_open REAL')
cur.execute('ALTER TABLE betting_lines ADD COLUMN IF NOT EXISTS over_under_open REAL')
conn.commit()

PROVIDER_PREF = ['consensus', 'Bovada', 'ESPN Bet', 'DraftKings', 'William Hill (New Jersey)']

with cfbd.ApiClient(cfg) as api:
    # ── venues (single call, all-time) ──────────────────────────────────────
    vs = cfbd.VenuesApi(api).get_venues()
    execute_values(cur, '''
        INSERT INTO venues (id, name, city, state, latitude, longitude,
                            elevation, dome, capacity, timezone) VALUES %s
        ON CONFLICT (id) DO UPDATE SET
            latitude=EXCLUDED.latitude, longitude=EXCLUDED.longitude,
            elevation=EXCLUDED.elevation, dome=EXCLUDED.dome''',
        [(v.id, v.name, v.city, v.state,
          float(v.latitude) if v.latitude is not None else None,
          float(v.longitude) if v.longitude is not None else None,
          float(v.elevation) if getattr(v, 'elevation', None) is not None else None,
          1 if getattr(v, 'dome', False) else 0,
          getattr(v, 'capacity', None), getattr(v, 'timezone', None))
         for v in vs if v.id is not None])
    conn.commit()
    print(f"venues: {len(vs)}", flush=True)

    games_api, teams_api, coaches_api, betting = (
        cfbd.GamesApi(api), cfbd.TeamsApi(api), cfbd.CoachesApi(api), cfbd.BettingApi(api))

    for season in range(FIRST, LAST + 1):
        # ── weather ─────────────────────────────────────────────────────────
        try:
            w = games_api.get_weather(year=season)
            execute_values(cur, '''
                INSERT INTO game_weather (game_id, season, week, venue_id, game_indoors,
                    temperature, dew_point, humidity, precipitation, snowfall,
                    wind_speed, wind_direction, pressure, weather_condition) VALUES %s
                ON CONFLICT (game_id) DO UPDATE SET
                    temperature=EXCLUDED.temperature, wind_speed=EXCLUDED.wind_speed,
                    precipitation=EXCLUDED.precipitation, game_indoors=EXCLUDED.game_indoors''',
                [(x.id, season, x.week, getattr(x, 'venue_id', None),
                  1 if getattr(x, 'game_indoors', False) else 0,
                  x.temperature, x.dew_point, x.humidity, x.precipitation,
                  getattr(x, 'snowfall', None), x.wind_speed,
                  getattr(x, 'wind_direction', None), getattr(x, 'pressure', None),
                  getattr(x, 'weather_condition', None))
                 for x in w if x.id is not None])
            conn.commit()
            print(f"{season} weather: {len(w)}", flush=True)
        except Exception as e:
            conn.rollback(); print(f"{season} weather ERR: {type(e).__name__}: {str(e)[:90]}", flush=True)

        # ── talent ──────────────────────────────────────────────────────────
        try:
            t = teams_api.get_talent(year=season)
            execute_values(cur, '''
                INSERT INTO team_talent (team, season, talent) VALUES %s
                ON CONFLICT (team, season) DO UPDATE SET talent=EXCLUDED.talent''',
                list({(x.team, season): (x.team, season, float(x.talent))
                      for x in t if x.talent is not None}.values()))
            conn.commit()
            print(f"{season} talent: {len(t)}", flush=True)
        except Exception as e:
            conn.rollback(); print(f"{season} talent ERR: {str(e)[:80]}", flush=True)

        # ── coaches (head coach per team-season) ────────────────────────────
        try:
            cs = coaches_api.get_coaches(year=season)
            # A team can have >1 coach in a season (mid-season change); keep the
            # last listed so the (team, season) key stays unique within the batch.
            byteam = {}
            for c in cs:
                name = f"{c.first_name or ''} {c.last_name or ''}".strip()
                for cs_ in (c.seasons or []):
                    if cs_.year == season and cs_.school:
                        byteam[cs_.school] = (cs_.school, season, name,
                                              str(getattr(c, 'hire_date', '') or ''))
            rows = list(byteam.values())
            execute_values(cur, '''
                INSERT INTO coaches (team, season, coach, hire_date) VALUES %s
                ON CONFLICT (team, season) DO UPDATE SET coach=EXCLUDED.coach''', rows)
            conn.commit()
            print(f"{season} coaches: {len(rows)}", flush=True)
        except Exception as e:
            conn.rollback(); print(f"{season} coaches ERR: {str(e)[:80]}", flush=True)

        # ── opening lines ───────────────────────────────────────────────────
        try:
            L = betting.get_lines(year=season)
            n = 0
            for g in L:
                by = {(l.provider or ''): l for l in (g.lines or [])
                      if getattr(l, 'spread_open', None) is not None}
                if not by:
                    continue
                ln = next((by[p] for p in PROVIDER_PREF if p in by), next(iter(by.values())))
                cur.execute('''UPDATE betting_lines SET spread_open=%s, over_under_open=%s
                               WHERE game_id=%s''',
                            (float(ln.spread_open),
                             float(ln.over_under_open) if getattr(ln, 'over_under_open', None) is not None else None,
                             g.id))
                n += cur.rowcount
            conn.commit()
            print(f"{season} opening lines: {n}", flush=True)
        except Exception as e:
            conn.rollback(); print(f"{season} lines ERR: {str(e)[:80]}", flush=True)

conn.close()
print("forecast extras fetch complete")
