import cfbd
import sqlite3
import os
import time
from dotenv import load_dotenv

load_dotenv()

configuration = cfbd.Configuration(access_token=os.getenv("CFBD_API_KEY"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
conn = sqlite3.connect(os.path.join(BASE_DIR, 'cfb_data.db'))
cursor = conn.cursor()

try:
    cursor.execute('ALTER TABLE players ADD COLUMN active_2026 INTEGER DEFAULT 0')
    print("Added active_2026 column")
except:
    print("active_2026 column already exists")
try:
    cursor.execute('ALTER TABLE players ADD COLUMN draft_status TEXT')
    print("Added draft_status column")
except:
    print("draft_status column already exists")
conn.commit()

with cfbd.ApiClient(configuration) as api_client:
    teams_api = cfbd.TeamsApi(api_client)

    # Probe 2026 roster availability with a single team before looping 138 teams
    print("Probing 2026 roster availability...")
    probe = teams_api.get_roster(team='Alabama', year=2026)
    print(f"  Alabama 2026 roster: {len(probe)} players")

    cursor.execute('UPDATE players SET active_2026 = 0')
    conn.commit()
    updated = 0

    if len(probe) > 0:
        # 2026 data is available — loop all teams with rate-limit protection
        fbs_teams = teams_api.get_fbs_teams(year=2026)
        print(f"Fetching 2026 rosters for {len(fbs_teams)} teams...")
        not_matched = 0
        for i, t in enumerate(fbs_teams):
            try:
                roster = teams_api.get_roster(team=t.school, year=2026)
                for p in roster:
                    cursor.execute('''
                        UPDATE players SET active_2026 = 1
                        WHERE first_name=? AND last_name=? AND team=?
                    ''', (p.first_name, p.last_name, t.school))
                    if cursor.rowcount > 0:
                        updated += cursor.rowcount
                    else:
                        not_matched += 1
                if i % 20 == 0:
                    conn.commit()
                    print(f"  {i+1}/{len(fbs_teams)} teams done...")
                time.sleep(0.15)
            except Exception as e:
                print(f"  Error {t.school}: {e}")
        conn.commit()
        print(f"Marked {updated} active for 2026, {not_matched} not in DB")
    else:
        print("2026 rosters not yet published — skipping team loop, going straight to draft data + fallback")

    # Draft data — marks confirmed NFL entrants
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
                            UPDATE players SET draft_status=?
                            WHERE first_name=? AND last_name=?
                        ''', (status, first, last))
                        marked += cursor.rowcount
                conn.commit()
                print(f"  Draft {yr}: {len(picks)} picks, {marked} matched in DB")
            except Exception as e:
                print(f"  Draft {yr} error: {e}")
    except Exception as e:
        print(f"Draft API error: {e}")

# Fallback: if 2026 rosters weren't available, mark everyone active except draft picks
cursor.execute('SELECT COUNT(*) FROM players WHERE active_2026=1')
active_count = cursor.fetchone()[0]
cursor.execute('SELECT COUNT(*) FROM players')
total_count = cursor.fetchone()[0]
print(f"\nActive 2026 before fallback: {active_count} / {total_count}")

if active_count < total_count * 0.3:
    print("Applying fallback: active = everyone except confirmed draft picks")
    cursor.execute('UPDATE players SET active_2026 = 1 WHERE draft_status IS NULL')
    cursor.execute('UPDATE players SET active_2026 = 0 WHERE draft_status IS NOT NULL')
    conn.commit()
    cursor.execute('SELECT COUNT(*) FROM players WHERE active_2026=1')
    cursor.execute('SELECT COUNT(*) FROM players WHERE active_2026=0')
    inactive = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM players WHERE active_2026=1')
    active_final = cursor.fetchone()[0]
    print(f"  active_2026=1: {active_final}  |  active_2026=0 (drafted): {inactive}")

# Spot checks
print("\nSpot checks:")
for name in [('Drew', 'Allar'), ('Rocco', 'Becht'), ('Carson', 'Beck'), ('Nico', 'Iamaleava')]:
    cursor.execute(
        "SELECT first_name, last_name, team, active_2026, draft_status FROM players WHERE first_name=? AND last_name=?",
        name
    )
    rows = cursor.fetchall()
    for r in rows:
        print(f"  {r}")
    if not rows:
        print(f"  {name[0]} {name[1]}: not found")

conn.close()
print("\nDone!")
