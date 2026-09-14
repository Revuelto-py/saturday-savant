#!/usr/bin/env python3
"""Rebuild every season's Savant ratings AND weekly snapshots under the current
calibration, in ascending season order.

WHY ORDER MATTERS. Since 2026-09-13 each team's Bayesian prior is anchored to
its own preseason expectation — last season's Savant rating, regressed toward
the mean (PRIOR_REGRESS). Season N therefore reads season N-1's stored ratings,
so the seasons form a chain and must be rebuilt oldest-first. Rebuilding 2025
alone would leave it anchored to an old-calibration 2024; rebuilding newest
first would anchor every season to values about to be overwritten.

The earliest season has no predecessor and falls back to the national average,
exactly as load_prior_targets() documents.

WHAT IT WRITES, per season:
  • savant_ratings  — the season's final ratings (all games, postseason included)
  • savant_weekly   — one snapshot per regular-season week, plus the week-20
                      postseason sentinel, so the team page's Trends chart is
                      built the same way as the headline rating above it.

Games are loaded from the database once per season and every week is computed
from that one sample in memory; the alternative (invoking the pipeline script
once per week) re-reads ~900 game summaries 16 times per season.

    python3 backfill/backfill_savant_history.py            # every season with games
    python3 backfill/backfill_savant_history.py 2016 2025  # a range
    python3 backfill/backfill_savant_history.py --dry-run  # compute, report, write nothing
"""
import contextlib
import io
import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# The pipeline module clears the live page cache when it writes; this script
# does its own single notify at the end rather than one per season.
_stub = types.ModuleType('cache_notify')
_stub.notify_cache_clear = lambda *a, **k: None
sys.modules['cache_notify'] = _stub

import psycopg2
from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT, '.env'))

DRY = '--dry-run' in sys.argv
_nums = [int(a) for a in sys.argv[1:] if a.isdigit()]
FIRST = _nums[0] if _nums else None
LAST = _nums[1] if len(_nums) > 1 else FIRST

POSTSEASON_WEEK = 20     # sentinel used by write_snapshot for a complete season


def load_module(season):
    """A fresh copy of the pipeline module bound to `season`.

    Both write_table() and write_snapshot() read the module-level SEASON, so a
    multi-season run has to rebind it rather than pass it in."""
    argv = sys.argv
    sys.argv = ['compute_savant_ratings.py', str(season)]
    try:
        spec = importlib.util.spec_from_file_location(
            '_svr_backfill', os.path.join(ROOT, 'pipeline', 'compute_savant_ratings.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.argv = argv


def season_games(mod, cur, season):
    cur.execute('SELECT COUNT(*) FROM game_summaries s JOIN games g ON g.id = s.game_id '
                'WHERE g.season = %s', (season,))
    have_summaries = cur.fetchone()[0] >= 50
    with contextlib.redirect_stdout(io.StringIO()):
        if have_summaries:
            games, _fbs, measured = mod.load_game_samples(cur)
        else:
            games, _fbs, measured = mod.load_game_samples_cfbd(cur, season)
    return games, measured


def rebuild(mod, cur, season, games, priors):
    """Final ratings + every weekly snapshot for one season. Returns a summary."""
    regular = [g for g in games if g['order'][0] == 0]
    has_post = any(g['order'][0] == 1 for g in games)
    weeks = sorted({g['order'][1] for g in regular})

    with contextlib.redirect_stdout(io.StringIO()):
        final = mod.compute_ratings(games, mod.HFA_RATIO, priors)
    if not DRY:
        mod.write_table(cur, final)
        # A finished season's last snapshot is the postseason sentinel; an
        # in-progress one is labelled by the last week actually played.
        mod.write_snapshot(cur, final,
                           POSTSEASON_WEEK if has_post else (max(weeks) if weeks else 0))

    written = 0
    skipped = []
    for w in weeks:
        sub = [g for g in regular if g['order'][1] <= w]
        # Below the pipeline's own floor the ratings would be pure prior; the
        # live script exits rather than write those, so neither do we.
        if len(sub) < mod.MIN_GAMES:
            skipped.append(w)
            continue
        with contextlib.redirect_stdout(io.StringIO()):
            weekly = mod.compute_ratings(sub, mod.HFA_RATIO, priors)
        if not DRY:
            mod.write_snapshot(cur, weekly, w)
        written += 1
    return final, written, skipped, has_post


def main():
    conn = psycopg2.connect(dsn=os.getenv('DATABASE_URL'))
    try:
        cur = conn.cursor()
        cur.execute('SELECT DISTINCT season FROM games WHERE completed = 1 ORDER BY season')
        seasons = [r[0] for r in cur.fetchall()]
        if FIRST is not None:
            seasons = [s for s in seasons if FIRST <= s <= (LAST or FIRST)]
        if not seasons:
            print('no seasons to rebuild')
            return
        print(f"rebuilding {seasons[0]}-{seasons[-1]} in ascending order"
              + (" (DRY RUN — no writes)" if DRY else ""))
        print(f"{'season':>7} {'games':>6} {'measured':>9} {'priors':>7} "
              f"{'weeks':>6} {'top rated':<20} {'net':>7}")

        for season in seasons:
            mod = load_module(season)
            games, measured = season_games(mod, cur, season)
            if len(games) < mod.MIN_GAMES:
                print(f"{season:>7} {len(games):>6}  — below MIN_GAMES, skipped")
                continue
            with contextlib.redirect_stdout(io.StringIO()):
                priors = mod.load_prior_targets(cur, season)
            final, n_weeks, skipped, has_post = rebuild(mod, cur, season, games, priors)
            if not DRY:
                conn.commit()      # each season is complete before the next reads it
            best = min(final, key=lambda t: final[t]['net_ranking'])
            note = f" (weeks {skipped} below MIN_GAMES)" if skipped else ""
            print(f"{season:>7} {len(games):>6} {measured:>9.3f} {len(priors):>7} "
                  f"{n_weeks:>6} {best:<20} {final[best]['net_rating']:>7}{note}")

        if not DRY:
            # Drop the stub installed at import so the real notifier loads, and
            # poke the site once for the whole rebuild rather than per season.
            print('\ndone — clearing the live page cache')
            sys.modules.pop('cache_notify', None)
            try:
                from cache_notify import notify_cache_clear
                notify_cache_clear()
            except Exception as exc:
                print(f'cache clear failed ({exc.__class__.__name__}) — '
                      f'rows are written; the site will catch up on its TTL')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
