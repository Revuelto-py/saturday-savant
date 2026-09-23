"""Return deleted space to the filesystem after a large prune.

tools/prune_non_fbs_stats.py ends with VACUUM ANALYZE, which frees the space
for Postgres to reuse but leaves the files their original size — 290k deleted
rows stayed as ~70MB of free space inside player_stats rather than going back
to the disk Render bills for. VACUUM FULL rewrites each table at its live size.

It takes an ACCESS EXCLUSIVE lock for the length of the rewrite, so every page
that reads the table blocks until it finishes (seconds to a minute at this
size). Run it in a quiet window, never mid-slate. It also needs free disk equal
to the table being rewritten, since the rewrite is a copy.

Usage:  python3 tools/vacuum_full.py                        # report sizes only
        python3 tools/vacuum_full.py --apply                # rewrite the default tables
        python3 tools/vacuum_full.py --apply player_stats   # or named ones
"""

import os as _os, sys as _sys
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if ROOT not in _sys.path:
    _sys.path.insert(0, ROOT)

import os
import sys
import time

import psycopg2
from dotenv import load_dotenv

load_dotenv(_os.path.join(ROOT, '.env'), override=True)

DEFAULT_TABLES = ('player_stats', 'player_ppa', 'player_usage')

# Only tables this repo owns, spelled out — a table name goes into the SQL text
# (VACUUM takes no parameters), so it is never allowed to come from anywhere but
# this list.
ALLOWED = set(DEFAULT_TABLES) | {'player_game_logs', 'game_summaries', 'pool_store'}


def main():
    apply_ = '--apply' in sys.argv
    named = [a for a in sys.argv[1:] if not a.startswith('-')]
    tables = named or list(DEFAULT_TABLES)
    bad = [t for t in tables if t not in ALLOWED]
    if bad:
        sys.exit(f'refusing unknown table(s): {", ".join(bad)}')

    conn = psycopg2.connect(os.getenv('DATABASE_URL'))
    conn.set_isolation_level(0)          # VACUUM cannot run inside a transaction
    cur = conn.cursor()

    def size(t):
        cur.execute('SELECT pg_size_pretty(pg_total_relation_size(%s)), '
                    '       pg_total_relation_size(%s)', (t, t))
        return cur.fetchone()

    cur.execute('SELECT pg_size_pretty(pg_database_size(current_database()))')
    print(f'database: {cur.fetchone()[0]}')
    before = {t: size(t) for t in tables}
    for t in tables:
        print(f'  {t}: {before[t][0]}')

    if not apply_:
        print('\ndry run — re-run with --apply to rewrite these tables.')
        conn.close()
        return

    for t in tables:
        t0 = time.time()
        cur.execute(f'VACUUM FULL {t}')
        after = size(t)
        freed = (before[t][1] - after[1]) / 1e6
        print(f'  {t}: {before[t][0]} -> {after[0]}  ({freed:.0f} MB returned, '
              f'{time.time() - t0:.1f}s locked)', flush=True)

    cur.execute('SELECT pg_size_pretty(pg_database_size(current_database()))')
    print(f'database now: {cur.fetchone()[0]}')
    conn.close()


if __name__ == '__main__':
    main()
