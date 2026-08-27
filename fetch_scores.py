"""Refresh game scores + completion for the active season — and nothing else.

Why this exists separately from fetch_data.py: that script is the weekly
DESTRUCTIVE refresh (it DELETEs the season's games / player_stats / player_ppa
and re-inserts them), which is correct once a week but far too heavy — and too
dangerous — to run on a game-day cadence. The site's `games` table is what
drives the scoreboard ticker, the /games grid and each game page's state, so
between Saturday kickoff and the Monday pipeline every result would otherwise
sit at "Scheduled" with no score for ~30 hours.

This does the narrow thing instead: one CFBD call, then UPDATE the rows whose
score or completion actually changed. It never DELETEs and never INSERTs, so it
cannot wipe a season the way the 2026-07-21 incident did — the worst case is a
no-op. Safe to run every few minutes on game days.

Game pages need no extra step: /game/<id> already falls back to a live ESPN
summary fetch (and stores it) when a completed game has no stored summary, so a
game flips to its played layout as soon as this marks it complete.

Usage:  python3 fetch_scores.py              # active season, all weeks
        python3 fetch_scores.py --week 1     # one week (smaller payload)
"""
import os
import sys

import cfbd
import psycopg2
from dotenv import load_dotenv

from season_util import current_cfb_season

# No override: an exported DATABASE_URL wins, matching run_weekly.sh's chain.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

SEASON = current_cfb_season()
WEEK = None
if '--week' in sys.argv:
    WEEK = int(sys.argv[sys.argv.index('--week') + 1])

# A season's schedule is ~800-900 games. A response far below that means CFBD
# returned something partial/broken; with UPDATE-only writes that is harmless,
# but bail loudly rather than silently doing a fraction of the work.
MIN_GAMES = 100


def main():
    key = os.getenv('CFBD_API_KEY')
    if not key:
        print('CFBD_API_KEY not set — cannot fetch scores', flush=True)
        return 1

    with cfbd.ApiClient(cfbd.Configuration(access_token=key)) as api:
        games_api = cfbd.GamesApi(api)
        games = games_api.get_games(SEASON, week=WEEK) if WEEK \
            else games_api.get_games(SEASON)

    if not WEEK and len(games) < MIN_GAMES:
        print(f'CFBD returned only {len(games)} games for {SEASON} '
              f'(expected >= {MIN_GAMES}) — not applying', flush=True)
        return 1

    conn = psycopg2.connect(os.getenv('DATABASE_URL'))
    try:
        cur = conn.cursor()
        # Only rows whose score/completion actually moved: IS DISTINCT FROM
        # treats NULL correctly, so an unplayed game stays untouched and the
        # changed-count below is a real "what happened since last run".
        changed = 0
        for g in games:
            cur.execute('''
                UPDATE games
                   SET home_points = %s, away_points = %s, completed = %s
                 WHERE id = %s
                   AND (home_points IS DISTINCT FROM %s
                     OR away_points IS DISTINCT FROM %s
                     OR completed    IS DISTINCT FROM %s)
            ''', (g.home_points, g.away_points, 1 if g.completed else 0, g.id,
                  g.home_points, g.away_points, 1 if g.completed else 0))
            changed += cur.rowcount
        conn.commit()

        cur.execute('SELECT COUNT(*) FROM games WHERE season = %s AND completed = 1',
                    (SEASON,))
        done = cur.fetchone()[0]
        scope = f'week {WEEK}' if WEEK else 'all weeks'
        print(f'{SEASON} {scope}: {len(games)} games checked, {changed} updated '
              f'({done} completed this season)', flush=True)
    finally:
        conn.close()

    # Only poke the live cache when something actually changed — this runs on a
    # tight loop during game days and a no-op run should cost the site nothing.
    if changed:
        try:
            from cache_notify import notify_cache_clear
            notify_cache_clear()
        except Exception:
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
