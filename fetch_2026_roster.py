import cfbd
import psycopg2
import os
import time
from dotenv import load_dotenv

load_dotenv()

configuration = cfbd.Configuration(access_token=os.getenv("CFBD_API_KEY"))
conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cursor = conn.cursor()

try:
    cursor.execute('ALTER TABLE players ADD COLUMN active_2026 INTEGER DEFAULT 0')
    conn.commit()
    print("Added active_2026 column")
except Exception:
    conn.rollback()
    print("active_2026 column already exists")

try:
    cursor.execute('ALTER TABLE players ADD COLUMN draft_status TEXT')
    conn.commit()
    print("Added draft_status column")
except Exception:
    conn.rollback()
    print("draft_status column already exists")

with cfbd.ApiClient(configuration) as api_client:
    teams_api = cfbd.TeamsApi(api_client)

    print("Probing 2026 roster availability...")
    probe = teams_api.get_roster(team='Alabama', year=2026)
    print(f"  Alabama 2026 roster: {len(probe)} players")

    # ── Fetch every roster BEFORE touching the database ─────────────────────
    # This used to reset active_2026 to 0 up front and fill it in as each team
    # came back. When CFBD 502s partway through (it does), that left the flag
    # mostly empty, which then tripped the <30% fallback below and marked the
    # ENTIRE players table active. Collect first, write once: a bad CFBD day now
    # ends with last week's roster intact instead of a corrupted one.
    roster_ok, roster_failed, all_players = 0, [], []
    if len(probe) > 0:
        fbs_teams = teams_api.get_fbs_teams(year=2026)
        print(f"Fetching 2026 rosters for {len(fbs_teams)} teams...")
        for i, t in enumerate(fbs_teams):
            roster = None
            for attempt in range(4):          # CFBD 502s under load; back off
                try:
                    roster = teams_api.get_roster(team=t.school, year=2026)
                    break
                except Exception as e:
                    if attempt == 3:
                        print(f"  giving up on {t.school}: {str(e)[:70]}")
                    else:
                        time.sleep(2 ** attempt)
            if roster is None:
                roster_failed.append(t.school)
                continue
            roster_ok += 1
            all_players.extend((p, t.school) for p in roster)
            if i % 20 == 0:
                print(f"  {i+1}/{len(fbs_teams)} teams done...")
            time.sleep(0.3)

        print(f"rosters fetched: {roster_ok} ok, {len(roster_failed)} failed")
        # A partial roster is worse than none: it would silently drop every
        # player on the teams that failed.
        if roster_failed:
            print(f"  ABORTING roster update — incomplete ({', '.join(roster_failed[:8])}"
                  f"{' …' if len(roster_failed) > 8 else ''}). Existing roster left untouched.")
            conn.rollback()
            raise SystemExit(1)

    updated = inserted = not_matched = 0
    if all_players:
        seen = []
        for p, school in all_players:
            pid = int(p.id) if str(getattr(p, 'id', '') or '').isdigit() else None
            if pid is None:
                not_matched += 1
                continue
            # Insert on the CFBD athlete id rather than only flagging names we
            # already hold. The old loop UPDATEd by name+team and dropped anyone
            # it couldn't find, which silently discarded every true freshman: a
            # first-year player has no prior CFBD stats, so no players row to
            # update, and therefore no headshot, no EA rating match and no shot
            # at being named a starter. The id is the same athlete id
            # player_stats, ea_ratings and the headshot mirror all key on.
            cursor.execute('''
                INSERT INTO players
                    (id, first_name, last_name, team, position, jersey,
                     height, weight, year, active_2026)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1)
                ON CONFLICT (id) DO UPDATE SET
                    active_2026 = 1,
                    team     = EXCLUDED.team,
                    position = COALESCE(EXCLUDED.position, players.position),
                    jersey   = COALESCE(EXCLUDED.jersey,   players.jersey),
                    height   = COALESCE(EXCLUDED.height,   players.height),
                    weight   = COALESCE(EXCLUDED.weight,   players.weight),
                    year     = COALESCE(EXCLUDED.year,     players.year)
            ''', (pid, p.first_name, p.last_name, school, p.position,
                  p.jersey, p.height, p.weight,
                  str(p.year) if p.year is not None else None))
            seen.append(pid)
            updated += 1
        # Everyone not on a 2026 roster goes inactive — done in the same
        # transaction as the marking, so the flag is never briefly empty.
        cursor.execute('UPDATE players SET active_2026 = 0 WHERE NOT (id = ANY(%s))', (seen,))
        conn.commit()
        print(f"2026 roster: {updated:,} players marked active across {roster_ok} teams"
              f"{f', {not_matched} without a usable id' if not_matched else ''}")
    else:
        print("2026 rosters not yet published — leaving active_2026 as-is")

    print("\nFetching NFL draft data...")
    try:
        draft_api = cfbd.DraftApi(api_client)
        for yr in [2025, 2026]:
            try:
                picks = draft_api.get_draft_picks(year=yr)
                marked = 0
                for pick in picks:
                    name = getattr(pick, 'name', '') or ''
                    if not name:
                        continue
                    parts = name.strip().split(' ', 1)
                    if len(parts) == 2:
                        first, last = parts
                        status = (f"Drafted {yr} (Rd {getattr(pick,'round',None)}, "
                                  f"Pk {getattr(pick,'pick',None)}) - {getattr(pick,'nfl_team',None)}")
                        cursor.execute('''
                            UPDATE players SET draft_status=%s
                            WHERE first_name=%s AND last_name=%s
                        ''', (status, first, last))
                        marked += cursor.rowcount
                conn.commit()
                print(f"  Draft {yr}: {len(picks)} picks, {marked} matched in DB")
            except Exception as e:
                print(f"  Draft {yr} error: {e}")
    except Exception as e:
        print(f"Draft API error: {e}")

# Fallback
cursor.execute('SELECT COUNT(*) FROM players WHERE active_2026=1')
active_count = cursor.fetchone()[0]
cursor.execute('SELECT COUNT(*) FROM players')
total_count = cursor.fetchone()[0]
print(f"\nActive 2026 before fallback: {active_count} / {total_count}")

# The fallback exists for the offseason window when CFBD hasn't published next
# season's rosters at all. It must NOT fire when the team loop DID run — a
# transient CFBD outage once left the flag at 12% and this marked the entire
# 54k-row table active, wiping the distinction between a current roster and
# every player in history.
if not all_players and active_count < total_count * 0.3:
    print("Applying fallback: active = everyone except confirmed draft picks")
    cursor.execute('UPDATE players SET active_2026 = 1 WHERE draft_status IS NULL')
    cursor.execute('UPDATE players SET active_2026 = 0 WHERE draft_status IS NOT NULL')
    conn.commit()
    cursor.execute('SELECT COUNT(*) FROM players WHERE active_2026=0')
    inactive = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM players WHERE active_2026=1')
    active_final = cursor.fetchone()[0]
    print(f"  active_2026=1: {active_final}  |  active_2026=0 (drafted): {inactive}")

print("\nSpot checks:")
for name in [('Drew', 'Allar'), ('Rocco', 'Becht'), ('Carson', 'Beck'), ('Nico', 'Iamaleava')]:
    cursor.execute(
        "SELECT first_name, last_name, team, active_2026, draft_status FROM players WHERE first_name=%s AND last_name=%s",
        name
    )
    rows = cursor.fetchall()
    for r in rows:
        print(f"  {r}")
    if not rows:
        print(f"  {name[0]} {name[1]}: not found")

conn.close()
print("\nDone!")


# Data changed — tell the live site to drop its in-memory page cache so the
# update is visible immediately instead of after the cache TTL.
try:
    from cache_notify import notify_cache_clear
    notify_cache_clear()
except Exception:
    pass
