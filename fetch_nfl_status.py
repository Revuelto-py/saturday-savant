import re
import cfbd
import psycopg2
import requests
import os
from dotenv import load_dotenv
load_dotenv()

configuration = cfbd.Configuration(access_token=os.getenv("CFBD_API_KEY"))
conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cursor = conn.cursor()

# CFBD's draft-pick nflTeamId uses the same numbering as ESPN's NFL team ids
# (e.g. 33=Ravens, 34=Texans), so pull ESPN's team list once and key off id
# rather than team name — CFBD's nfl_team field is just a city ("Los Angeles",
# "New York") which is ambiguous between two franchises for 4 of the 32 teams.
r = requests.get(
    'https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams',
    params={'limit': 40}, timeout=15,
)
NFL_TEAMS = {}
for t in r.json()['sports'][0]['leagues'][0]['teams']:
    team = t['team']
    NFL_TEAMS[int(team['id'])] = {
        'name': team['displayName'],
        'logo': next((l['href'] for l in team.get('logos', [])), ''),
    }
print(f"Loaded {len(NFL_TEAMS)} NFL teams")

with cfbd.ApiClient(configuration) as api_client:
    draft_api = cfbd.DraftApi(api_client)

    # Most recent two draft classes relative to today (2026-06-30) are the
    # 2025 and 2026 drafts — players who left college after the 2024/2025 seasons.
    for yr in [2025, 2026]:
        try:
            picks = draft_api.get_draft_picks(year=yr)
            print(f"NFL Draft {yr}: {len(picks)} picks")

            for pick in picks:
                name = (pick.name or '').strip()
                if not name:
                    continue
                parts = name.split(' ', 1)
                if len(parts) != 2:
                    continue
                first, last = parts

                team_info = NFL_TEAMS.get(pick.nfl_team_id, {})
                team_name = team_info.get('name') or pick.nfl_team or ''
                logo = team_info.get('logo', '')

                cursor.execute('''
                    SELECT id FROM players
                    WHERE first_name ILIKE %s AND last_name ILIKE %s
                    LIMIT 1
                ''', (first, last))
                player = cursor.fetchone()

                if player:
                    cursor.execute('''
                        UPDATE players SET
                            nfl_status = 'drafted',
                            nfl_team = %s,
                            nfl_team_logo = %s,
                            draft_year = %s,
                            draft_round = %s,
                            draft_pick = %s,
                            active_2026 = 0
                        WHERE id = %s
                    ''', (team_name, logo, yr, pick.round, pick.pick, player[0]))
                    print(f"  Drafted: {first} {last} -> {team_name} (Rd {pick.round}, Pk {pick.pick})")

            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"  Draft {yr} error: {e}")
            import traceback; traceback.print_exc()

# UDFA signings: ESPN's transactions feed has no per-athlete field (despite what
# you'd expect) — names only exist inside a free-text "description" sentence like
# "Signed OLBs Niles King and Nadame Tucker ... to rookie contracts." Rather than
# parsing names out of that sentence, check which of our own undrafted players'
# full names appear (on a word boundary) inside rookie/UDFA signing transactions.
print("\nFetching UDFA signings from ESPN...")
cursor.execute('''
    SELECT id, first_name, last_name FROM players
    WHERE active_2026 = 1 AND (nfl_status IS NULL OR nfl_status = '')
''')
candidates = cursor.fetchall()
print(f"{len(candidates)} undrafted candidates to check against transactions")

udfa_count = 0
try:
    page = 1
    page_count = 1
    while page <= page_count:
        r = requests.get(
            'https://site.api.espn.com/apis/site/v2/sports/football/nfl/transactions',
            params={'limit': 500, 'page': page},
            timeout=15,
        )
        data = r.json()
        page_count = data.get('pageCount', 1)
        transactions = data.get('transactions', [])
        if page == 1:
            print(f"Found {data.get('count', len(transactions))} total transactions across {page_count} page(s)")

        for txn in transactions:
            desc = txn.get('description', '') or ''
            desc_lower = desc.lower()
            if 'rookie contract' not in desc_lower and 'udfa contract' not in desc_lower:
                continue

            team = txn.get('team', {}) or {}
            team_name = team.get('displayName', '')
            logo = next((l['href'] for l in team.get('logos', []) if 'dark' not in l.get('rel', [])), '')

            for pid, first, last in candidates:
                full_name = f"{first} {last}"
                if re.search(r'\b' + re.escape(full_name) + r'\b', desc):
                    cursor.execute('''
                        UPDATE players SET
                            nfl_status = 'udfa',
                            nfl_team = %s,
                            nfl_team_logo = %s,
                            active_2026 = 0
                        WHERE id = %s AND (nfl_status IS NULL OR nfl_status = '')
                    ''', (team_name, logo, pid))
                    if cursor.rowcount > 0:
                        udfa_count += 1
                        print(f"  UDFA: {full_name} -> {team_name}")

        page += 1

    conn.commit()
    print(f"Updated {udfa_count} UDFA signings")

except Exception as e:
    conn.rollback()
    print(f"ESPN UDFA error: {e}")
    import traceback; traceback.print_exc()

# Mark remaining seniors/grad players who fell off the 2026 roster and got
# neither drafted nor signed as UDFAs as simply done with college football.
cursor.execute('''
    UPDATE players SET
        nfl_status = 'graduated'
    WHERE year IN ('4', '5', 'Sr', 'Gr')
    AND active_2026 = 0
    AND (nfl_status IS NULL OR nfl_status = '')
''')
print(f"\nMarked {cursor.rowcount} players as graduated")
conn.commit()

# Verify Nadame Tucker specifically
cursor.execute('''
    SELECT first_name, last_name, nfl_status, nfl_team, draft_round, draft_pick, active_2026
    FROM players WHERE last_name ILIKE 'Tucker' AND first_name ILIKE '%Nada%'
''')
print("\nNadame Tucker check:")
for row in cursor.fetchall():
    print(f"  {row}")

conn.close()
print("\nDone!")


# Data changed — tell the live site to drop its in-memory page cache so the
# update is visible immediately instead of after the cache TTL.
try:
    from cache_notify import notify_cache_clear
    notify_cache_clear()
except Exception:
    pass
