"""Phase 0 migration: make the schema multi-season.

Additive and idempotent — safe to run against the live database while the
current (season-unaware) code is still deployed:
  • adds a `season` column to the five tables that lacked one, backfilling
    every existing row to 2025 (the only season currently loaded);
  • creates the `rosters` table — per-(player, season) team/position/jersey —
    seeded from the current players snapshot (2025 = everyone, 2026 = the
    active_2026 flag) so the team page can read rosters per season;
  • adds season indexes for the new filter patterns.

Nothing is dropped or rewritten; existing queries keep working unchanged.
Historical seasons (2016–2024) are loaded later by the Phase 1a backfill.
"""
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=True)

conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cur = conn.cursor()

SEASON_TABLES = ['player_stats', 'player_ppa', 'team_stats', 'team_advanced', 'sp_ratings']

for t in SEASON_TABLES:
    cur.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = %s AND column_name = 'season'
    """, (t,))
    if cur.fetchone():
        print(f"{t}: season column already present")
        continue
    cur.execute(f'ALTER TABLE {t} ADD COLUMN season INTEGER')
    cur.execute(f'UPDATE {t} SET season = 2025 WHERE season IS NULL')
    conn.commit()
    cur.execute(f'SELECT COUNT(*) FROM {t} WHERE season = 2025')
    print(f"{t}: season added, {cur.fetchone()[0]} rows backfilled to 2025")

# rosters — one row per (player, season). The players table stays the identity
# record (name, headshot, NFL/draft status); rosters carries the per-season
# team/position/jersey/measurables that change year to year.
cur.execute("""
    CREATE TABLE IF NOT EXISTS rosters (
        player_id  INTEGER NOT NULL,
        season     INTEGER NOT NULL,
        team       TEXT,
        position   TEXT,
        jersey     INTEGER,
        height     INTEGER,
        weight     INTEGER,
        class_year TEXT,
        PRIMARY KEY (player_id, season)
    )
""")
conn.commit()

# Seed from the current snapshot so behavior is unchanged before the backfill:
# 2025 = the full players table (it IS the 2025 roster), 2026 = active_2026.
cur.execute('SELECT COUNT(*) FROM rosters')
if cur.fetchone()[0] == 0:
    cur.execute("""
        INSERT INTO rosters (player_id, season, team, position, jersey, height, weight, class_year)
        SELECT id, 2025, team, position, jersey, height, weight, year FROM players
        ON CONFLICT DO NOTHING
    """)
    cur.execute("""
        INSERT INTO rosters (player_id, season, team, position, jersey, height, weight, class_year)
        SELECT id, 2026, team, position, jersey, height, weight, year FROM players
        WHERE active_2026 = 1
        ON CONFLICT DO NOTHING
    """)
    conn.commit()
    cur.execute('SELECT season, COUNT(*) FROM rosters GROUP BY season ORDER BY season')
    print('rosters seeded:', cur.fetchall())
else:
    print('rosters: already populated, skipping seed')

for idx, ddl in [
    ('idx_player_stats_season', 'CREATE INDEX IF NOT EXISTS idx_player_stats_season ON player_stats(season)'),
    ('idx_player_ppa_season',   'CREATE INDEX IF NOT EXISTS idx_player_ppa_season ON player_ppa(season)'),
    ('idx_rosters_team_season', 'CREATE INDEX IF NOT EXISTS idx_rosters_team_season ON rosters(team, season)'),
    ('idx_rosters_season',      'CREATE INDEX IF NOT EXISTS idx_rosters_season ON rosters(season)'),
]:
    cur.execute(ddl)
print('indexes ensured')
conn.commit()
conn.close()
print('Phase 0 migration complete')
