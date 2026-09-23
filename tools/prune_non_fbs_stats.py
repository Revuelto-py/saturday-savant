"""Delete stored stats for players outside FBS.

CFBD's season endpoints return every division in one payload and the ingest
stored all of it, while every page that reads these tables filters to FBS. The
result was 290k player_stats rows — a quarter of the table, half of the 2026
season — for programs the site does not cover and players it has no page for.

The rule matches divisions.keep_stat_row exactly, so this deletes precisely
what the ingest now declines to write:

    delete a row when the team is not FBS AND the player has no page here

The second half is what keeps career logs whole. 685 players on 2026 FBS
rosters have FCS seasons behind them (Abraham Williams' three years at Weber
State, say) and those rows are content — they show on his page. They stay.

Idempotent: a second run finds nothing. Dry run by default.

Usage:  python3 tools/prune_non_fbs_stats.py            # report only
        python3 tools/prune_non_fbs_stats.py --apply    # delete
"""

import os as _os, sys as _sys
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if ROOT not in _sys.path:
    _sys.path.insert(0, ROOT)

import os
import sys

import psycopg2
from dotenv import load_dotenv

from divisions import FCS_CONFS

load_dotenv(_os.path.join(ROOT, '.env'), override=True)

APPLY = '--apply' in sys.argv

# (table, team column, player-id column). The id casts differ per table —
# player_stats keys on TEXT, the other two on integers — so each one is spelled
# out rather than assumed.
TABLES = [
    ('player_stats', 'team', 'player_id'),
    ('player_ppa',   'team', 'player_id'),
    ('player_usage', 'team', 'player_id'),
]

# A row is non-FBS when its team is in an FCS conference OR has no teams row at
# all (the D-II opponents that appear in box scores). The NOT EXISTS is the
# "has a page here" half of divisions.keep_stat_row.
WHERE = """
    WHERE NOT EXISTS (
              SELECT 1 FROM teams t
               WHERE t.name = x.{team} AND t.conference IS NOT NULL
                 AND t.conference NOT IN %s)
      AND NOT EXISTS (
              SELECT 1 FROM players pl WHERE pl.id::text = x.{pid}::text)
"""


def main():
    conn = psycopg2.connect(os.getenv('DATABASE_URL'))
    cur = conn.cursor()
    total = 0
    for table, team, pid in TABLES:
        where = WHERE.format(team=team, pid=pid)
        cur.execute(f'SELECT count(*) FROM {table} x {where}', (FCS_CONFS,))
        n = cur.fetchone()[0]
        cur.execute(f'SELECT count(*) FROM {table}')
        have = cur.fetchone()[0]
        print(f'{table}: {n} of {have} rows are non-FBS with no page here', flush=True)
        if APPLY and n:
            cur.execute(f'DELETE FROM {table} x {where}', (FCS_CONFS,))
            conn.commit()
            print(f'  deleted {cur.rowcount}', flush=True)
        total += n

    if APPLY and total:
        # Returns the space to Postgres for reuse and refreshes the planner's
        # statistics, which now describe a much smaller table. It does NOT
        # shrink the files on disk — that needs VACUUM FULL, which takes an
        # exclusive lock and belongs in a maintenance window, not here.
        conn.set_isolation_level(0)
        for table, _, _ in TABLES:
            print(f'vacuum analyze {table}', flush=True)
            cur.execute(f'VACUUM (ANALYZE) {table}')
        conn.set_isolation_level(1)
    elif not APPLY:
        print(f'\ndry run — {total} rows would be deleted. Re-run with --apply.')
    conn.close()


if __name__ == '__main__':
    main()
