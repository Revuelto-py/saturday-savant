"""Advance every returning player's class to the 2026 season.

CFBD's 2026 rosters are empty and its roster feed carries no redshirt field,
and our rosters table stores one frozen class per player (it does not progress
year over year). So the 2026 class is derived from each player's 2025 class
(rosters season=2025) and whether they recorded any 2025 game action:

  • A returning player with recorded 2025 action advances one class
    (Freshman -> Sophomore -> Junior -> Senior -> Graduate).
  • A 2025 TRUE FRESHMAN with no recorded 2025 action is treated as a redshirt:
    they hold "Freshman" and are flagged redshirt (shown sitewide as
    "Redshirt Freshman" / "RS Fr").

Redshirt detection is a heuristic and intentionally limited to freshmen. A
freshman offensive lineman or deep special-teamer who played but produced no
box-score stat is indistinguishable from a redshirt in our data, so some are
over-flagged — but that is where redshirts concentrate and where a false flag
is least damaging (an over-flagged upperclassman would read as clearly wrong).

Idempotent: the 2026 class is always computed from the frozen 2025 roster
class, never from the already-advanced players.year, so re-running is safe.
Writes players.year, players.redshirt, and rosters(season=2026).class_year.

Usage:  python3 advance_2026_classes.py
"""
import os
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=True)

conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cur = conn.cursor()

# Career redshirt flag (0/1). Idempotent.
cur.execute('ALTER TABLE players ADD COLUMN IF NOT EXISTS redshirt INTEGER DEFAULT 0')
conn.commit()

# Every 2026 player who has a real 2025 class (1-4), with whether they recorded
# any 2025 action — a season stat line or at least one game in the game log.
cur.execute('''
    SELECT p.id, r.class_year,
           (EXISTS (SELECT 1 FROM player_stats ps
                    WHERE ps.player_id = p.id::text AND ps.season = 2025)
            OR EXISTS (SELECT 1 FROM player_game_logs g
                       WHERE g.player_id = p.id AND g.season = 2025
                       AND COALESCE(json_array_length(g.log::json), 0) > 0)) AS played
    FROM players p
    JOIN rosters r ON r.player_id = p.id AND r.season = 2025
    WHERE p.active_2026 = 1 AND r.class_year ~ '^[1-4]$'
''')
rows = cur.fetchall()

updates = []          # (id, new_class, redshirt)
advanced = redshirted = 0
for pid, cls, played in rows:
    c = int(cls)
    if c == 1 and not played:
        new_class, rs = 1, 1          # redshirt freshman — hold the class
        redshirted += 1
    else:
        new_class, rs = min(c + 1, 5), 0   # cap at 5 = Graduate
        advanced += 1
    updates.append((pid, str(new_class), rs))

# Current snapshot: players.year + the redshirt flag.
execute_values(cur, '''
    UPDATE players p SET year = v.cls, redshirt = v.rs::int
    FROM (VALUES %s) AS v(id, cls, rs)
    WHERE p.id = v.id::int
''', updates, template='(%s,%s,%s)', page_size=1000)

# Next season's roster rows so the 2026 team roster shows the advanced class.
execute_values(cur, '''
    UPDATE rosters r SET class_year = v.cls
    FROM (VALUES %s) AS v(id, cls)
    WHERE r.player_id = v.id::int AND r.season = 2026
''', [(pid, nc) for pid, nc, _ in updates], template='(%s,%s)', page_size=1000)

conn.commit()
print(f"2026 classes set for {len(updates)} players: "
      f"{advanced} advanced +1, {redshirted} held as redshirt freshmen", flush=True)

# Post-advance class distribution sanity check
cur.execute("SELECT year, redshirt, COUNT(*) FROM players WHERE active_2026=1 "
            "AND year ~ '^[1-5]$' GROUP BY year, redshirt ORDER BY year, redshirt")
print("class/redshirt distribution:", cur.fetchall(), flush=True)
conn.close()


# Data changed — tell the live site to drop its in-memory page cache so the
# update is visible immediately instead of after the cache TTL.
try:
    from cache_notify import notify_cache_clear
    notify_cache_clear()
except Exception:
    pass
