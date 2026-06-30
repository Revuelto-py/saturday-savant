import sqlite3
import cfbd
import requests
import os
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed

load_dotenv()

configuration = cfbd.Configuration(
    access_token=os.getenv("CFBD_API_KEY")
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
conn = sqlite3.connect(os.path.join(BASE_DIR, 'cfb_data.db'))
cursor = conn.cursor()

cursor.execute('''
    CREATE TABLE IF NOT EXISTS teams (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        conference TEXT,
        abbreviation TEXT,
        logo TEXT
    )
''')

cursor.execute('''
    CREATE TABLE IF NOT EXISTS players (
        id INTEGER PRIMARY KEY,
        first_name TEXT,
        last_name TEXT,
        team TEXT,
        position TEXT,
        jersey INTEGER,
        headshot TEXT
    )
''')

cursor.execute('''
    CREATE TABLE IF NOT EXISTS player_stats (
        player_id TEXT,
        player_name TEXT,
        team TEXT,
        conference TEXT,
        position TEXT,
        category TEXT,
        stat_type TEXT,
        stat REAL
    )
''')

cursor.execute('''
    CREATE TABLE IF NOT EXISTS games (
        id INTEGER PRIMARY KEY,
        season INTEGER,
        week INTEGER,
        season_type TEXT,
        home_team TEXT,
        home_points INTEGER,
        away_team TEXT,
        away_points INTEGER,
        completed INTEGER,
        start_date TEXT,
        notes TEXT
    )
''')

cursor.execute('''
    CREATE TABLE IF NOT EXISTS player_ppa (
        player_id TEXT,
        player_name TEXT,
        position TEXT,
        team TEXT,
        conference TEXT,
        avg_ppa_all REAL,
        avg_ppa_pass REAL,
        avg_ppa_rush REAL,
        total_ppa REAL
    )
''')

with cfbd.ApiClient(configuration) as api_client:
    teams_api = cfbd.TeamsApi(api_client)
    teams_info = teams_api.get_fbs_teams(year=2025)
    roster = teams_api.get_roster(year=2025)

fbs_team_names = {t.school for t in teams_info}
fbs_roster = [p for p in roster if p.team in fbs_team_names]

for t in teams_info:
    logo = t.logos[0] if t.logos else None
    cursor.execute('''
        INSERT OR REPLACE INTO teams (name, conference, abbreviation, logo)
        VALUES (?, ?, ?, ?)
    ''', (t.school, t.conference, t.abbreviation, logo))

for player in fbs_roster:
    cursor.execute('''
        INSERT OR REPLACE INTO players (id, first_name, last_name, team, position, jersey)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (player.id, player.first_name, player.last_name, player.team, player.position, player.jersey))

print(f"Teams saved: {len(teams_info)}")
print(f"FBS players saved: {len(fbs_roster)}")

headshot_dir = os.path.join(BASE_DIR, 'static', 'headshots')
os.makedirs(headshot_dir, exist_ok=True)

def download_headshot(player):
    path = os.path.join(headshot_dir, f"{player.id}.png")
    if os.path.exists(path):
        return player.id, 'skipped'
    url = f"https://a.espncdn.com/i/headshots/college-football/players/full/{player.id}.png"
    try:
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            with open(path, 'wb') as f:
                f.write(r.content)
            return player.id, 'saved'
        return player.id, 'failed'
    except:
        return player.id, 'failed'

print("Downloading headshots...")
saved = 0
skipped = 0
failed = 0
headshot_map = {}

with ThreadPoolExecutor(max_workers=20) as executor:
    futures = {executor.submit(download_headshot, p): p for p in fbs_roster}
    for i, future in enumerate(as_completed(futures)):
        player_id, status = future.result()
        if status == 'saved':
            saved += 1
            headshot_map[player_id] = f"/static/headshots/{player_id}.png"
        elif status == 'skipped':
            skipped += 1
            headshot_map[player_id] = f"/static/headshots/{player_id}.png"
        else:
            failed += 1
        if i % 200 == 0:
            print(f"  Progress: {i}/{len(fbs_roster)}")

for player_id, path in headshot_map.items():
    cursor.execute('UPDATE players SET headshot = ? WHERE id = ?', (path, player_id))

print(f"Headshots: {saved} saved, {skipped} already existed, {failed} not found")

conn.commit()
conn.close()
print("Setup complete")