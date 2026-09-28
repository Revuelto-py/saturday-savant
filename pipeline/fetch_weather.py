"""Per-game weather for the active season (CFBD weather feed) -> game_weather.

tools/fetch_forecast_extras.py backfilled 2016-2025 once; this keeps the
current season filled so game pages can show conditions. One CFBD call.

Usage:  python3 pipeline/fetch_weather.py [season]
"""
import os as _os, sys as _sys
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if ROOT not in _sys.path:
    _sys.path.insert(0, ROOT)
import os
import sys

import cfbd
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

from season_util import current_cfb_season

# No override: a DATABASE_URL already in the environment wins (see
# compute_savant_ratings.py for the incident this avoids).
load_dotenv(os.path.join(ROOT, '.env'))

season = next((int(a) for a in sys.argv[1:] if a.isdigit()), current_cfb_season())
cfg = cfbd.Configuration(access_token=os.getenv('CFBD_API_KEY'))
with cfbd.ApiClient(cfg) as api:
    w = cfbd.GamesApi(api).get_weather(year=season)

rows = [(x.id, season, x.week, getattr(x, 'venue_id', None),
         1 if getattr(x, 'game_indoors', False) else 0,
         x.temperature, x.dew_point, x.humidity, x.precipitation,
         getattr(x, 'snowfall', None), x.wind_speed,
         getattr(x, 'wind_direction', None), getattr(x, 'pressure', None),
         getattr(x, 'weather_condition', None))
        for x in w if x.id is not None]

conn = psycopg2.connect(os.getenv('DATABASE_URL'))
try:
    cur = conn.cursor()
    if rows:
        execute_values(cur, '''
            INSERT INTO game_weather (game_id, season, week, venue_id, game_indoors,
                temperature, dew_point, humidity, precipitation, snowfall,
                wind_speed, wind_direction, pressure, weather_condition) VALUES %s
            ON CONFLICT (game_id) DO UPDATE SET
                venue_id=EXCLUDED.venue_id, game_indoors=EXCLUDED.game_indoors,
                temperature=EXCLUDED.temperature, dew_point=EXCLUDED.dew_point,
                humidity=EXCLUDED.humidity, precipitation=EXCLUDED.precipitation,
                snowfall=EXCLUDED.snowfall, wind_speed=EXCLUDED.wind_speed,
                wind_direction=EXCLUDED.wind_direction, pressure=EXCLUDED.pressure,
                weather_condition=EXCLUDED.weather_condition''', rows)
    conn.commit()
finally:
    conn.close()
print(f"{season} weather: {len(rows)} games", flush=True)
