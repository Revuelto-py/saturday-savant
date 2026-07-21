"""Weekly precompute of the expensive team-page + returning-production data
into pool_store, run right after the CFBD fetch scripts refresh the tables.

These three are otherwise computed live per request and dominate a cold team
page (~5.7s in prod). Precomputing them per season / per team means no first
visitor pays that cost. The routes still self-heal (compute + store on a miss),
so this is a warm-up, not a hard dependency.

  returning:{season}   _returning_production_ranks   (all FBS, per season)
  teampct:{season}     _team_percentiles_all         (all teams, per season)
  nfltalent:{team}     _team_nfl_talent              (all-time, per team)

Because the routes read the store before recomputing, a plain re-run would read
the stale value straight back out. So each key is deleted first, forcing a fresh
compute against the newly-fetched tables. `.uncached()` skips the in-process
memoize layer too, so nothing short-circuits the rebuild.

Usage:  python3 precompute.py            # all loaded seasons
        python3 precompute.py 2024 2025  # specific seasons
"""
import os
import sys

os.environ.setdefault('POOL_BACKFILL', '1')
import main  # reuse the exact compute + store code paths


def _call(fn, *args):
    """Call a memoized compute function, bypassing the in-process cache."""
    return fn.uncached(*args) if hasattr(fn, 'uncached') else fn(*args)


def _fbs_teams():
    conn = main.get_db()
    try:
        cur = conn.cursor()
        cur.execute('SELECT name FROM teams WHERE conference IS NOT NULL '
                    'AND conference <> ALL(%s)', (list(main.FCS_CONFS),))
        return sorted(r[0] for r in cur.fetchall())
    finally:
        main.release_db(conn)


def run():
    seasons = sorted(int(a) for a in sys.argv[1:]) or sorted(main.get_available_seasons())

    # Per season: returning production + every team's percentiles.
    for season in seasons:
        main._pool_store_delete([f"returning:{season}", f"teampct:{season}"])
        _call(main._returning_production_ranks, season)
        _call(main._team_percentiles_all, season)
        print(f"{season}: returning production + team percentiles stored", flush=True)

    # All-time, per team: NFL talent.
    teams = _fbs_teams()
    main._pool_store_delete([f"nfltalent:{t}" for t in teams])
    for t in teams:
        _call(main._team_nfl_talent, t)
    print(f"nfl talent stored for {len(teams)} teams", flush=True)

    print("precompute complete", flush=True)


if __name__ == '__main__':
    run()


# Data changed — tell the live site to drop its in-memory page cache so the
# update is visible immediately instead of after the cache TTL.
try:
    from cache_notify import notify_cache_clear
    notify_cache_clear()
except Exception:
    pass
