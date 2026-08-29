"""Tell the live site to drop its in-memory page cache after a data update.

Deploys don't need this — Flask-Caching's SimpleCache is in-process, so every
deploy restart starts with an empty cache by definition (verifiable via the
X-Boot response header). The stale-cache problem only exists in the OTHER
direction: fetch/backfill scripts update the shared database while the running
process keeps serving cached pages for up to six hours. Every data-writing
script calls notify_cache_clear() at the end so that window closes itself.

The ADMIN_KEY is sent via the X-Admin-Key header (never the URL, so it stays
out of access logs) and is read from the same .env that holds it for the
endpoint. Never raises — a failed notification just means the cache expires on
its normal TTL.
"""
import os

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=True)

SITE_URL = os.getenv('SITE_URL', 'https://saturdaysavant.com').rstrip('/')


def notify_cache_clear(scope=None):
    """Drop the live site's page cache.

    scope='scores' clears only the pages a score changes (home, /games, the
    week views). A score has no bearing on leaderboards, team precompute or
    player pages, so the score cron uses it to avoid making every goal cost a
    site-wide recompute. The weekly chain passes no scope and clears
    everything, which is right — it rewrites nearly every table.
    """
    key = os.getenv('ADMIN_KEY')
    if not key:
        print('cache_notify: no ADMIN_KEY in environment — skipped', flush=True)
        return False
    try:
        r = requests.get(f'{SITE_URL}/admin/clear-cache',
                         params={'scope': scope} if scope else None,
                         headers={'X-Admin-Key': key}, timeout=15)
        print(f'cache_notify: {SITE_URL} -> HTTP {r.status_code}', flush=True)
        return r.ok
    except Exception as e:
        print(f'cache_notify: failed ({type(e).__name__}) — cache will expire on TTL', flush=True)
        return False


if __name__ == '__main__':
    notify_cache_clear()
