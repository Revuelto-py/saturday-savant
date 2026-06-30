import cfbd
import sqlite3
import os
from dotenv import load_dotenv

load_dotenv()

configuration = cfbd.Configuration(access_token=os.getenv("CFBD_API_KEY"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
conn = sqlite3.connect(os.path.join(BASE_DIR, 'cfb_data.db'))
cursor = conn.cursor()

with cfbd.ApiClient(configuration) as api_client:
    teams_api = cfbd.TeamsApi(api_client)
    teams_info = teams_api.get_fbs_teams(year=2025)

updated = 0
for t in teams_info:
    color = getattr(t, 'color', None)
    alt_color = getattr(t, 'alternate_color', None)
    cursor.execute('UPDATE teams SET color=?, alt_color=? WHERE name=?',
                   (color, alt_color, t.school))
    updated += 1

conn.commit()
conn.close()
print(f"Updated {updated} teams")

# Verify a few
conn = sqlite3.connect(os.path.join(BASE_DIR, 'cfb_data.db'))
cursor = conn.cursor()
cursor.execute("SELECT name, color, alt_color FROM teams WHERE name IN ('Alabama', 'Florida', 'Penn State') ")
for row in cursor.fetchall():
    print(row)
conn.close()