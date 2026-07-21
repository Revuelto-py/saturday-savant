"""Add + backfill games.neutral_site from CFBD (one-time, all seasons).

The games table never stored CFBD's neutral_site flag, but the Savant Forecast
home-field feature needs it (≈400 postseason games plus regular-season kickoff
classics are played on neutral fields — scoring them as home games would bias
the home-advantage coefficient). Adds the column idempotently and updates every
existing row by game id. Going forward the fetch scripts write the flag on
insert, so this is a one-time catch-up.

Usage:  python3 backfill_neutral_site.py            # 2016 .. current season
Budget: one CFBD call per season.
"""
import os

import cfbd
import psycopg2
from dotenv import load_dotenv

from season_util import current_cfb_season

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=True)

configuration = cfbd.Configuration(access_token=os.getenv("CFBD_API_KEY"))
conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cur = conn.cursor()

cur.execute('ALTER TABLE games ADD COLUMN IF NOT EXISTS neutral_site INTEGER DEFAULT 0')
conn.commit()

with cfbd.ApiClient(configuration) as api_client:
    games_api = cfbd.GamesApi(api_client)
    for season in range(2016, current_cfb_season() + 1):
        result = games_api.get_games(season)
        neutral_ids = [g.id for g in result if getattr(g, 'neutral_site', False)]
        if neutral_ids:
            cur.execute('UPDATE games SET neutral_site = 1 WHERE id = ANY(%s)', (neutral_ids,))
        conn.commit()
        print(f"{season}: {len(neutral_ids)} neutral-site games flagged "
              f"({cur.rowcount} matched rows)", flush=True)

conn.close()
print("neutral_site backfill complete")
