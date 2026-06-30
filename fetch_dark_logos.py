import cfbd
import psycopg2
import os
from dotenv import load_dotenv

load_dotenv()

configuration = cfbd.Configuration(access_token=os.getenv("CFBD_API_KEY"))
conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cursor = conn.cursor()

try:
    cursor.execute('ALTER TABLE teams ADD COLUMN logo_dark TEXT')
    conn.commit()
    print("Added logo_dark column")
except Exception:
    conn.rollback()
    print("logo_dark column already exists")

with cfbd.ApiClient(configuration) as api_client:
    teams_api = cfbd.TeamsApi(api_client)
    teams = teams_api.get_fbs_teams(year=2025)

updated = 0
dark_found = 0
for t in teams:
    logos = getattr(t, 'logos', []) or []
    logo_dark = logos[1] if len(logos) > 1 else (logos[0] if logos else None)
    logo_regular = logos[0] if logos else None

    if logo_dark and logo_dark != logo_regular:
        dark_found += 1

    cursor.execute('UPDATE teams SET logo_dark=%s WHERE name=%s', (logo_dark, t.school))
    updated += cursor.rowcount

conn.commit()
print(f"Updated {updated} teams, {dark_found} have distinct dark logos")

cursor.execute("SELECT name, logo, logo_dark FROM teams WHERE name IN ('Penn State','Ohio State','Alabama','Georgia') ORDER BY name")
for r in cursor.fetchall():
    print(f"{r[0]}:")
    print(f"  regular: {r[1]}")
    print(f"  dark:    {r[2]}")

conn.close()
