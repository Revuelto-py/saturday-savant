"""Refresh game scores + completion for the active season — and nothing else.

Why this exists separately from pipeline/fetch_data.py: that script is the weekly
DESTRUCTIVE refresh (it DELETEs the season's games / player_stats / player_ppa
and re-inserts them), which is correct once a week but far too heavy — and too
dangerous — to run on a game-day cadence. The site's `games` table is what
drives the scoreboard ticker, the /games grid and each game page's state, so
between Saturday kickoff and the Monday pipeline every result would otherwise
sit at "Scheduled" with no score for ~30 hours.

This does the narrow thing instead: two CFBD reads, then UPDATE the rows whose
score or completion actually changed. It never DELETEs and never INSERTs, so it
cannot wipe a season the way the 2026-07-21 incident did — the worst case is a
no-op. Safe to run every few minutes on game days.

Two reads, because /games alone is not enough: it only populates home_points /
away_points once a game is FINAL. Verified 2026-08-29 during UNC-TCU — every
week-1 game carrying points had completed=True, and the in-progress game
carried None. So a game sat at "Scheduled" with no score for its entire four
hours. /scoreboard is the live endpoint and does carry an in-progress score,
so it supplies scores for games under way; /games stays the sole authority on
whether a game is COMPLETE, so a game can never be marked final early.

A game with points but completed=0 is therefore "in progress", which is how
the site renders a live state without needing to store a clock.

Game pages need no extra step: /game/<id> already falls back to a live ESPN
summary fetch (and stores it) when a completed game has no stored summary, so a
game flips to its played layout as soon as this marks it complete.

Usage:  python3 pipeline/fetch_scores.py              # active season, all weeks
        python3 pipeline/fetch_scores.py --week 1     # one week (smaller payload)
"""

# This script lives one directory below the repo root; ROOT points back at it so
# .env, the model artifacts and the shared modules resolve the same as before.
import os as _os, sys as _sys
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if ROOT not in _sys.path:
    _sys.path.insert(0, ROOT)
import os
import sys

import cfbd
import psycopg2
from dotenv import load_dotenv

from season_util import current_cfb_season

# No override: an exported DATABASE_URL wins, matching run_weekly.sh's chain.
load_dotenv(os.path.join(ROOT, '.env'))

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
        # Live board for games under way. Non-fatal: if it fails we still apply
        # the finals below, which is the behaviour this script had before.
        try:
            board = games_api.get_scoreboard()
        except Exception as exc:
            print(f'scoreboard unavailable ({exc}) — finals only', flush=True)
            board = []

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
            # /games reports points=None for anything not yet final — including
            # a game under way. Writing that back would null out the score the
            # board pass below just wrote, so leave those rows alone entirely.
            # Without this the two passes fight: a run whose scoreboard call
            # fails, or an older build of this script, blanks a live score.
            if g.home_points is None and g.away_points is None and not g.completed:
                continue
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

        # Live pass. The board carries BOTH the running score and the moment a
        # game ends, and it learns of the ending well before /games does — which
        # is why completion is taken from here rather than waiting for the
        # finals pass. A game sitting at "in progress" for an hour after it
        # finished is a worse failure than the theoretical risk of trusting the
        # board's own completed flag. Rows are matched by id, so a board entry
        # for a game we do not carry is a no-op.
        live = 0
        finals = 0
        for g in board:
            status = getattr(getattr(g, 'status', None), 'value', getattr(g, 'status', None))
            if status not in ('in_progress', 'completed'):
                continue
            hp = getattr(getattr(g, 'home_team', None), 'points', None)
            ap = getattr(getattr(g, 'away_team', None), 'points', None)
            if hp is None or ap is None:
                continue
            done = 1 if status == 'completed' else 0
            cur.execute("""
                UPDATE games
                   SET home_points = %s, away_points = %s, completed = %s
                 WHERE id = %s
                   AND completed = 0
                   AND (home_points IS DISTINCT FROM %s
                     OR away_points IS DISTINCT FROM %s
                     OR completed    IS DISTINCT FROM %s)
            """, (hp, ap, done, g.id, hp, ap, done))
            if cur.rowcount:
                live += 1
                finals += done
        changed += live
        conn.commit()

        cur.execute('SELECT COUNT(*) FROM games WHERE season = %s AND completed = 1',
                    (SEASON,))
        done = cur.fetchone()[0]
        scope = f'week {WEEK}' if WEEK else 'all weeks'
        print(f'{SEASON} {scope}: {len(games)} games checked, {changed} updated '
              f'({live} live, {finals} just finished) '
              f'({done} completed this season)', flush=True)
    finally:
        conn.close()

    # Only poke the live cache when something actually changed — this runs on a
    # tight loop during game days and a no-op run should cost the site nothing.
    if changed:
        try:
            from cache_notify import notify_cache_clear
            # A game FINISHING ripples much wider than a score moving: team
            # records, its own page's layout, standings. That is rare enough
            # (tens of times a week) to justify the full clear. A score merely
            # moving is frequent and narrow, so it stays scoped.
            notify_cache_clear(scope=None if finals else 'scores')
        except Exception:
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
