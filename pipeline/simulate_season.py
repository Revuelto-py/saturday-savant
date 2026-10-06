"""Season simulator — weekly step (end of the cron chain, after predict_games).

Plays the rest of the active season out (season_sim.simulate) and stores the
result in pool_store as simulator:{season}, which /simulator reads. Needs the
fresh Savant Forecast predictions, so it runs after predict_games.

Usage:  python3 pipeline/simulate_season.py          # active season
        python3 pipeline/simulate_season.py 2026
"""
import os as _os, sys as _sys
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if ROOT not in _sys.path:
    _sys.path.insert(0, ROOT)
import datetime
import sys
import time

_os.environ.setdefault('POOL_BACKFILL', '1')
import main
import season_sim
from season_util import current_cfb_season

N = 10000

season = int(sys.argv[1]) if len(sys.argv) > 1 else current_cfb_season()
conn = main.get_db()
try:
    data = season_sim.load(conn.cursor(), season, main._compute_conference_standings)
finally:
    main.release_db(conn)
if not data['games']:
    print(f'{season}: no remaining forecast games — nothing to simulate (kept the stored result)')
    sys.exit(0)
t0 = time.time()
result = season_sim.simulate(data, n=N)
if result is None:
    sys.exit(f'{season}: not enough teams to simulate')
result.update(season=season, games=len(data['games']),
              ran_at=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='minutes'))
main._pool_store_put(f'simulator:{season}', season, result)
print(f'{season}: simulated {N:,} seasons over {len(data["games"])} games in {time.time() - t0:.0f}s')
for r in result['teams'][:12]:
    print(f"  {r['team']:<18}{r['cfp']:>7.1%}{r['bye']:>7.1%}{r['title']:>7.1%}")
try:
    from cache_notify import notify_cache_clear
    notify_cache_clear()
except Exception:
    pass
