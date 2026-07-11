"""Phase 1a: backfill historical rosters (2016-2024) into the rosters table.

Usage:
    python3 backfill_rosters.py                 # all of 2016-2024
    python3 backfill_rosters.py 2019 2020       # specific seasons

Two CFBD calls per season: get_fbs_teams(year) for that year's FBS membership
(realignment-correct), and get_roster(year) for every player. Only FBS players
with real (positive) ESPN ids are stored — CFBD marks pre-2019 players it
couldn't match to ESPN with synthetic negative ids; those have no stats, no
headshot, and no player page, so they are skipped and counted.

Writes:
  • rosters — one row per (player, season): team/position/jersey/measurables.
    Upserts, so re-running refreshes in place. 2025/2026 are refused (seeded
    from the players snapshot in Phase 0 / owned by fetch_2026_roster.py).
  • players — identity rows (name; headshot filled by the Phase 1b mirror) for
    ids we've never seen, inserted with active_2026=0 and the historical team.
    Existing players rows are NEVER updated — the current snapshot (team,
    active flag, NFL/draft status) stays authoritative.

The players table gains a primary key on id here (idempotent) — it never had
one, and the identity upsert needs it.
"""
import sys
import cfbd
import psycopg2
from psycopg2.extras import execute_values
import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=True)

PROTECTED_SEASONS = (2025, 2026)

configuration = cfbd.Configuration(access_token=os.getenv("CFBD_API_KEY"))
conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cursor = conn.cursor()

# players.id was never constrained (SQLite-era table). Verified unique; the
# identity insert below needs ON CONFLICT (id).
cursor.execute('''
    SELECT 1 FROM information_schema.table_constraints
    WHERE table_name='players' AND constraint_type='PRIMARY KEY'
''')
if not cursor.fetchone():
    cursor.execute('ALTER TABLE players ADD PRIMARY KEY (id)')
    conn.commit()
    print('players: primary key added on id')


def clean_class_year(val, season):
    """CFBD's roster `year` is the class year (1-4) in most seasons, but some
    older rows carry the season itself (e.g. 2016). Store only plausible
    class years; anything else becomes NULL."""
    if val is None:
        return None
    try:
        v = int(val)
    except (TypeError, ValueError):
        return None
    return str(v) if 1 <= v <= 6 else None


def backfill_year(teams_api, y):
    fbs = {t.school for t in teams_api.get_fbs_teams(year=y)}
    roster = teams_api.get_roster(year=y)

    roster_rows = []
    identity_rows = []
    synthetic = 0
    seen = set()
    for p in roster:
        if p.team not in fbs:
            continue
        try:
            pid = int(p.id)
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            synthetic += 1
            continue
        if pid in seen:   # a handful of players appear on two rosters mid-year
            continue
        seen.add(pid)
        cy = clean_class_year(getattr(p, 'year', None), y)
        roster_rows.append((pid, y, p.team, p.position, p.jersey, p.height, p.weight, cy))
        identity_rows.append((pid, p.first_name, p.last_name, p.team, p.position,
                              p.jersey, p.height, p.weight, cy))

    execute_values(cursor, '''
        INSERT INTO rosters (player_id, season, team, position, jersey,
                             height, weight, class_year)
        VALUES %s
        ON CONFLICT (player_id, season) DO UPDATE SET
            team=EXCLUDED.team, position=EXCLUDED.position,
            jersey=EXCLUDED.jersey, height=EXCLUDED.height,
            weight=EXCLUDED.weight, class_year=EXCLUDED.class_year
    ''', roster_rows, page_size=1000)

    # Identity rows for players we've never seen — historical attributes,
    # never overwriting the current snapshot (DO NOTHING keeps existing rows).
    cursor.execute('SELECT COUNT(*) FROM players')
    before = cursor.fetchone()[0]
    execute_values(cursor, '''
        INSERT INTO players (id, first_name, last_name, team, position, jersey,
                             height, weight, year, active_2026)
        VALUES %s
        ON CONFLICT (id) DO NOTHING
    ''', [ir + (0,) for ir in identity_rows],
        page_size=1000)
    cursor.execute('SELECT COUNT(*) FROM players')
    new_players = cursor.fetchone()[0] - before

    conn.commit()
    print(f"{y}: rosters={len(roster_rows)}  new_identity_rows={new_players}  synthetic_skipped={synthetic}", flush=True)


def main():
    seasons = [int(a) for a in sys.argv[1:]] or list(range(2016, 2025))
    bad = [y for y in seasons if y in PROTECTED_SEASONS]
    if bad:
        raise SystemExit(f"Refusing protected season(s) {bad}.")
    with cfbd.ApiClient(configuration) as api_client:
        teams_api = cfbd.TeamsApi(api_client)
        for y in seasons:
            backfill_year(teams_api, y)
    cursor.execute('SELECT season, COUNT(*) FROM rosters GROUP BY season ORDER BY season')
    print('\nrosters by season:', cursor.fetchall())
    conn.close()


if __name__ == '__main__':
    main()


# Data changed — tell the live site to drop its in-memory page cache so the
# update is visible immediately instead of after the cache TTL.
try:
    from cache_notify import notify_cache_clear
    notify_cache_clear()
except Exception:
    pass
