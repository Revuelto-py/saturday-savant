# This script lives one directory below the repo root; ROOT points back at it so
# .env, the model artifacts and the shared modules resolve the same as before.
import os as _os, sys as _sys
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if ROOT not in _sys.path:
    _sys.path.insert(0, ROOT)
import cfbd
import sqlite3
import os
from dotenv import load_dotenv

load_dotenv(_os.path.join(ROOT, '.env'))

configuration = cfbd.Configuration(access_token=os.getenv("CFBD_API_KEY"))
BASE_DIR = ROOT
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