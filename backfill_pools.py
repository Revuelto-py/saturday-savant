"""Precompute every percentile peer pool into pool_store.

The player page runs 8-12 pool aggregations (~400ms each when cold); this
persists all of them — 15 (kind, category, positions) combos x every loaded
season — so no first visitor ever pays the computation. The pool functions in
main.py read pool_store before recomputing, and write back on a miss, so this
is a warm-up, not a hard dependency.

Usage:  python3 backfill_pools.py            # all seasons
        python3 backfill_pools.py 2019 2024  # specific seasons
"""
import os
import sys

os.environ.setdefault('POOL_BACKFILL', '1')
import main  # reuse the exact pool computation + store code paths

# Every (category, positions) tuple the player page and compare tool use —
# mirrors _pos_groups / COMPARE_PEER_POSITIONS / the wide-front-seven pools.
QB  = ('QB',)
RB  = ('RB', 'HB', 'FB')
WRT = ('WR', 'TE')
DL  = ('DE', 'DT', 'NT', 'DL', 'EDGE')
LB  = ('LB', 'ILB', 'OLB', 'MLB')
DB  = ('CB', 'S', 'SS', 'FS', 'SAF', 'DB')
FRONT7 = ('DE', 'DT', 'NT', 'DL', 'EDGE', 'LB', 'ILB', 'OLB', 'MLB')
WIDE_SKILL = ('WR', 'TE', 'RB', 'HB', 'FB')

STAT_POOLS = [
    ('passing',   QB),
    ('rushing',   RB),
    ('rushing',   QB),
    ('receiving', WRT),
    ('receiving', WIDE_SKILL),
    ('defensive', DL),
    ('defensive', LB),
    ('defensive', DB),
    ('defensive', FRONT7),
]
PPA_POOLS = [QB, RB, WRT, DL, LB, DB]


def run():
    seasons = [int(a) for a in sys.argv[1:]] or main.get_available_seasons()
    for season in sorted(seasons):
        # Force a fresh rebuild: drop the stored pools first, else the compute
        # functions read the (stale) value straight back out of pool_store and
        # never recompute against the freshly-fetched player_stats/player_ppa.
        stale = [f"stats:{cat}:{','.join(pos)}:{season}" for cat, pos in STAT_POOLS]
        stale += [f"ppa:{','.join(pos)}:{season}" for pos in PPA_POOLS]
        main._pool_store_delete(stale)
        n = 0
        for category, positions in STAT_POOLS:
            key = f"stats:{category}:{','.join(positions)}:{season}"
            pool = main._stats_pool_cached.uncached(category, positions, season) \
                if hasattr(main._stats_pool_cached, 'uncached') \
                else main._stats_pool_cached(category, positions, season)
            n += 1
        for positions in PPA_POOLS:
            pool = main._ppa_pool_cached.uncached(positions, season) \
                if hasattr(main._ppa_pool_cached, 'uncached') \
                else main._ppa_pool_cached(positions, season)
            n += 1
        print(f"{season}: {n} pools computed + stored", flush=True)
    print("pool backfill complete", flush=True)


if __name__ == '__main__':
    run()


# Data changed — tell the live site to drop its in-memory page cache so the
# update is visible immediately instead of after the cache TTL.
try:
    from cache_notify import notify_cache_clear
    notify_cache_clear()
except Exception:
    pass
