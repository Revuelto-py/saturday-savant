"""Self-check: a score-only cache clear drops home and /games and keeps the rest,
and every clear rebuilds the warm pages in the background.

Home and /games are cached with query_string=True, so their keys are the path
plus a hash, and a clear that deleted 'view/<path>' matched nothing: on a
Saturday the home page sat on its last render until a game finished.
python3 tools/check_cache_scope.py (main connects its DB pool on import, so
.env must be present; renders the warm pages, so it takes a minute).
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402

main._RL_MAX = 0
time.sleep(5)
while main._rewarm_lock.locked():
    time.sleep(1)
c = main.app.test_client()
for p in ('/', '/games', '/teams'):
    c.get(p)
with main.app.test_request_context('/'):
    home, games = main.home.make_cache_key(), None
with main.app.test_request_context('/games'):
    games = main.games_hub.make_cache_key()
assert main.cache.get(home) is not None and main.cache.get(games) is not None, 'home and /games cached'
main._purge_pages('scores')
assert main.cache.get(home) is None and main.cache.get(games) is None, 'a score clear drops home and /games'
assert main.cache.get('view//teams') is not None, 'and leaves pages a score cannot change'
with main.app.test_request_context('/'):
    main._purge_local(None)
time.sleep(0.5)
while main._rewarm_lock.locked():
    time.sleep(1)
assert main.cache.get(home) is not None, 'a clear rebuilds the warm pages without a visitor'
# The score cron sends its clear as python-requests with ?scope=scores; the bot
# guard on query-string URLs must let /admin/ through (it shed it with a 429).
main._epoch_bump = lambda scope: 1
r = c.get('/admin/clear-cache?scope=scores', headers={'User-Agent': 'python-requests/2.32.3',
                                                      'X-Admin-Key': os.getenv('ADMIN_KEY', '')})
assert r.status_code == 200, f'the score cron reaches the scoped clear ({r.status_code})'
assert c.get('/games?season=2025', headers={'User-Agent': 'python-requests/2.32.3'}).status_code == 429, \
    'bots on query-string pages are still shed'
print('cache scope: ok')
