#!/usr/bin/env python3
"""Recompute HFA_RATIO for pipeline/compute_savant_ratings.py.

The Savant model applies home-field as a FIXED constant rather than measuring
it from the season in progress, because the raw home/away points-per-drive
split has no control for opponent quality and September schedules are
deliberately unbalanced — see the comment on HFA_RATIO for the numbers.

This prints the measured full-season ratio for every completed season, and
their mean, which is where the constant comes from. Run it after a season ends
and update the constant if the mean has drifted.

    python3 tools/savant_hfa.py [first_season] [last_season]
"""
import contextlib
import io
import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# The pipeline module pokes the live site's cache on import in some versions;
# stub it so a read-only analysis can never reach out.
_stub = types.ModuleType('cache_notify')
_stub.notify_cache_clear = lambda *a, **k: None
sys.modules.setdefault('cache_notify', _stub)

import psycopg2
from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT, '.env'))

FIRST = int(sys.argv[1]) if len(sys.argv) > 1 else 2019
LAST = int(sys.argv[2]) if len(sys.argv) > 2 else 2025


def season_ratio(cur, season):
    sys.argv = ['compute_savant_ratings.py', str(season)]
    spec = importlib.util.spec_from_file_location(
        f'_svr{season}', os.path.join(ROOT, 'pipeline', 'compute_savant_ratings.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cur.execute('SELECT COUNT(*) FROM game_summaries s JOIN games g ON g.id = s.game_id '
                'WHERE g.season = %s', (season,))
    have_summaries = cur.fetchone()[0] >= 50
    with contextlib.redirect_stdout(io.StringIO()):
        if have_summaries:
            games, _fbs, ratio = mod.load_game_samples(cur)
        else:
            games, _fbs, ratio = mod.load_game_samples_cfbd(cur, season)
    return len(games), ratio


def main():
    conn = psycopg2.connect(dsn=os.getenv('DATABASE_URL'))
    try:
        cur = conn.cursor()
        print(f"{'season':>7} {'games':>6} {'ratio':>8} {'applied':>8}")
        ratios = []
        for season in range(FIRST, LAST + 1):
            n, ratio = season_ratio(cur, season)
            if n < 200:                      # not a complete season
                print(f"{season:>7} {n:>6} {'—':>8} {'(partial, skipped)':>8}")
                continue
            ratios.append(ratio)
            print(f"{season:>7} {n:>6} {ratio:>8.4f} {ratio ** 0.5:>8.4f}")
        if ratios:
            mean = sum(ratios) / len(ratios)
            print(f"\nmean of {len(ratios)} complete seasons: {mean:.4f}")
            print(f"set HFA_RATIO = {round(mean, 2)} in pipeline/compute_savant_ratings.py")
    finally:
        conn.close()


if __name__ == '__main__':
    main()
