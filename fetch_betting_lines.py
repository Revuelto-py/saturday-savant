"""Fetch Vegas betting lines from CFBD into the betting_lines table.

One row per game: the closing spread (home perspective — negative means the
home team is favored), over/under, and moneylines where present. When a game
has lines from several sportsbooks, one is chosen by a fixed provider
preference so the stored line is deterministic.

The lines serve two jobs:
  • the Vegas-closing-line baseline in the Savant Forecast evaluation — the
    honesty benchmark every model result is compared against;
  • (Phase 2) optional display alongside forecasts.

Usage:  python3 fetch_betting_lines.py              # active season only
        python3 fetch_betting_lines.py 2016 2025    # backfill a season range

Budget: one CFBD call per season.
"""
import os
import sys

import cfbd
import psycopg2
from dotenv import load_dotenv

from season_util import current_cfb_season

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=True)

PROVIDER_PREF = ['consensus', 'Bovada', 'ESPN Bet', 'DraftKings', 'William Hill (New Jersey)']


def pick_line(lines):
    """Choose one sportsbook's line by preference order; fall back to the
    first that actually carries a spread."""
    by_provider = {(l.provider or ''): l for l in lines if l.spread is not None}
    if not by_provider:
        return None
    for p in PROVIDER_PREF:
        if p in by_provider:
            return by_provider[p]
    return next(iter(by_provider.values()))


def main():
    if len(sys.argv) >= 3:
        seasons = range(int(sys.argv[1]), int(sys.argv[2]) + 1)
    elif len(sys.argv) == 2:
        seasons = [int(sys.argv[1])]
    else:
        seasons = [current_cfb_season()]

    configuration = cfbd.Configuration(access_token=os.getenv("CFBD_API_KEY"))
    conn = psycopg2.connect(os.getenv('DATABASE_URL'))
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS betting_lines (
            game_id        BIGINT PRIMARY KEY,
            season         INTEGER NOT NULL,
            week           INTEGER,
            provider       TEXT,
            spread         REAL,     -- home perspective; negative = home favored
            over_under     REAL,
            home_moneyline INTEGER,
            away_moneyline INTEGER,
            updated_at     TIMESTAMPTZ DEFAULT now()
        )
    ''')
    conn.commit()

    with cfbd.ApiClient(configuration) as api_client:
        betting = cfbd.BettingApi(api_client)
        for season in seasons:
            games = betting.get_lines(year=season)
            saved = 0
            for g in games:
                line = pick_line(g.lines or [])
                if line is None:
                    continue
                cur.execute('''
                    INSERT INTO betting_lines
                        (game_id, season, week, provider, spread, over_under,
                         home_moneyline, away_moneyline, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (game_id) DO UPDATE SET
                        provider = EXCLUDED.provider, spread = EXCLUDED.spread,
                        over_under = EXCLUDED.over_under,
                        home_moneyline = EXCLUDED.home_moneyline,
                        away_moneyline = EXCLUDED.away_moneyline,
                        updated_at = now()
                ''', (g.id, season, g.week, line.provider,
                      float(line.spread) if line.spread is not None else None,
                      float(line.over_under) if line.over_under is not None else None,
                      line.home_moneyline, line.away_moneyline))
                saved += 1
            conn.commit()
            print(f"{season}: lines stored for {saved} games", flush=True)
    conn.close()
    print("betting lines fetch complete")


if __name__ == '__main__':
    main()
