"""Store the selection committee's weekly top 25 for 2016-2023 (cfp_rankings),
so the bracket can say how each field was built and /rankings has a CFP view
for every playoff season. pipeline/fetch_rankings.py keeps 2024 on current;
this fills the four-team years without touching their AP polls.

  - Regular-season committee weeks as CFBD numbers them.
  - 2020's selection-day ranking is CFBD's postseason entry (its title games came
    after the last regular week); it is stored as the week after that one.
  - CFBD's final 2023 ranking lists Florida State and Georgia both 5th; the
    committee had Florida State 5th and Georgia 6th.

Dry run by default; --write replaces cfp_rankings for 2016-2023 only.
    python3 backfill/backfill_cfp_rankings.py [--write]
"""

# This script lives one directory below the repo root; ROOT points back at it so
# .env and the shared modules resolve the same as before.
import os as _os, sys as _sys
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if ROOT not in _sys.path:
    _sys.path.insert(0, ROOT)
import os
import sys

import cfbd
import psycopg2
from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT, '.env'), override=True)

SEASONS = range(2016, 2024)
SELECTION_FROM_POSTSEASON = {2020}
FIXES = {(2023, 'Georgia'): 6}
WRITE = '--write' in sys.argv

api = cfbd.RankingsApi(cfbd.ApiClient(cfbd.Configuration(access_token=os.getenv('CFBD_API_KEY'))))
conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cur = conn.cursor()
rows = []
for season in SEASONS:
    reg, post = {}, None
    for w in api.get_rankings(year=season):
        for p in w.polls:
            if p.poll != 'Playoff Committee Rankings':
                continue
            ranks = {r.school: r.rank for r in p.ranks}
            if 'post' in str(w.season_type).lower():
                post = ranks
            else:
                reg[w.week] = ranks
    if season in SELECTION_FROM_POSTSEASON and post:
        reg[max(reg) + 1] = post
    final = reg[max(reg)]
    for (s, team), rank in FIXES.items():
        if s == season and team in final:
            final[team] = rank
    # The final ranking's top four are the seeded field; say so if they ever disagree.
    cur.execute('SELECT team FROM cfp_seeds WHERE season = %s ORDER BY seed', (season,))
    seeds = [r[0] for r in cur.fetchall()]
    top4 = [t for t, _ in sorted(final.items(), key=lambda kv: kv[1])][:4]
    flag = '' if top4 == seeds else f'  (seeds stored: {seeds} - run backfill/backfill_cfp.py {season})'
    print(f'{season}: weeks {sorted(reg)}, final top four {top4}{flag}')
    rows += [(season, wk, t, r) for wk, rk in reg.items() for t, r in rk.items()]

print(f'{len(rows)} rows')
if WRITE:
    cur.execute('DELETE FROM cfp_rankings WHERE season BETWEEN %s AND %s', (SEASONS[0], SEASONS[-1]))
    cur.executemany('INSERT INTO cfp_rankings (season, week, team, rank) VALUES (%s, %s, %s, %s)', rows)
    conn.commit()
    print('written')
    try:
        from lib.cache_notify import notify_cache_clear
        notify_cache_clear()
    except Exception:
        pass
conn.close()
