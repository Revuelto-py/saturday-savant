import cfbd
import sqlite3
import os
from dotenv import load_dotenv

load_dotenv()

configuration = cfbd.Configuration(access_token=os.getenv("CFBD_API_KEY"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
conn = sqlite3.connect(os.path.join(BASE_DIR, 'cfb_data.db'))
cursor = conn.cursor()

try:
    cursor.execute('ALTER TABLE players ADD COLUMN height INTEGER')
    cursor.execute('ALTER TABLE players ADD COLUMN weight INTEGER')
    cursor.execute('ALTER TABLE players ADD COLUMN year TEXT')
    conn.commit()
    print("Columns added")
except Exception as e:
    print("Columns exist:", e)

with cfbd.ApiClient(configuration) as api_client:
    teams_api = cfbd.TeamsApi(api_client)
    teams_info = teams_api.get_fbs_teams(year=2025)
    fbs_names = {t.school for t in teams_info}
    roster = teams_api.get_roster(year=2025)
    fbs_roster = [p for p in roster if p.team in fbs_names]

updated = 0
for p in fbs_roster:
    height = getattr(p, 'height', None)
    weight = getattr(p, 'weight', None)
    year = getattr(p, 'year', None)
    cursor.execute('UPDATE players SET height=?, weight=?, year=? WHERE id=?',
                   (height, weight, str(year) if year else None, p.id))
    updated += 1

conn.commit()
conn.close()
print(f"Updated {updated} players")

# Verify
conn = sqlite3.connect(os.path.join(BASE_DIR, 'cfb_data.db'))
cursor = conn.cursor()
cursor.execute("SELECT first_name, last_name, position, height, weight, year FROM players WHERE team='Penn State' LIMIT 5")
for r in cursor.fetchall():
    print(r)
conn.close()