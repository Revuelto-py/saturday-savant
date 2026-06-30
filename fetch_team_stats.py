import cfbd
import sqlite3
import os
from dotenv import load_dotenv

load_dotenv()

configuration = cfbd.Configuration(access_token=os.getenv("CFBD_API_KEY"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
conn = sqlite3.connect(os.path.join(BASE_DIR, 'cfb_data.db'))
cursor = conn.cursor()

cursor.execute('''
    CREATE TABLE IF NOT EXISTS team_stats (
        team TEXT PRIMARY KEY,
        -- Offense basic
        off_plays INTEGER,
        off_drives INTEGER,
        off_ppa REAL,
        off_total_ppa REAL,
        off_success_rate REAL,
        off_explosiveness REAL,
        off_power_success REAL,
        off_stuff_rate REAL,
        off_line_yards REAL,
        off_open_field_yards REAL,
        off_second_level_yards REAL,
        off_rushing_plays_ppa REAL,
        off_passing_plays_ppa REAL,
        off_rushing_success_rate REAL,
        off_passing_success_rate REAL,
        off_rushing_explosiveness REAL,
        off_passing_explosiveness REAL,
        -- Defense basic
        def_plays INTEGER,
        def_drives INTEGER,
        def_ppa REAL,
        def_total_ppa REAL,
        def_success_rate REAL,
        def_explosiveness REAL,
        def_power_success REAL,
        def_stuff_rate REAL,
        def_line_yards REAL,
        def_open_field_yards REAL,
        def_second_level_yards REAL,
        def_rushing_plays_ppa REAL,
        def_passing_plays_ppa REAL,
        def_rushing_success_rate REAL,
        def_passing_success_rate REAL,
        def_rushing_explosiveness REAL,
        def_passing_explosiveness REAL
    )
''')

with cfbd.ApiClient(configuration) as api_client:
    stats_api = cfbd.StatsApi(api_client)
    advanced = stats_api.get_advanced_season_stats(year=2025, exclude_garbage_time=True)
saved = 0
for s in advanced:
    try:
        o = s.offense
        d = s.defense
        cursor.execute('''
            INSERT OR REPLACE INTO team_stats VALUES (
                ?,
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
        ''', (
            s.team,
            getattr(o,'plays',None), getattr(o,'drives',None),
            getattr(o,'ppa',None), getattr(o,'total_ppa',None),
            getattr(o,'success_rate',None), getattr(o,'explosiveness',None),
            getattr(o,'power_success',None), getattr(o,'stuff_rate',None),
            getattr(o,'line_yards',None), getattr(o,'open_field_yards',None),
            getattr(o,'second_level_yards',None),
            getattr(o,'rushing_plays',None) and getattr(o.rushing_plays,'ppa',None),
            getattr(o,'passing_plays',None) and getattr(o.passing_plays,'ppa',None),
            getattr(o,'rushing_plays',None) and getattr(o.rushing_plays,'success_rate',None),
            getattr(o,'passing_plays',None) and getattr(o.passing_plays,'success_rate',None),
            getattr(o,'rushing_plays',None) and getattr(o.rushing_plays,'explosiveness',None),
            getattr(o,'passing_plays',None) and getattr(o.passing_plays,'explosiveness',None),

            getattr(d,'plays',None), getattr(d,'drives',None),
            getattr(d,'ppa',None), getattr(d,'total_ppa',None),
            getattr(d,'success_rate',None), getattr(d,'explosiveness',None),
            getattr(d,'power_success',None), getattr(d,'stuff_rate',None),
            getattr(d,'line_yards',None), getattr(d,'open_field_yards',None),
            getattr(d,'second_level_yards',None),
            getattr(d,'rushing_plays',None) and getattr(d.rushing_plays,'ppa',None),
            getattr(d,'passing_plays',None) and getattr(d.passing_plays,'ppa',None),
            getattr(d,'rushing_plays',None) and getattr(d.rushing_plays,'success_rate',None),
            getattr(d,'passing_plays',None) and getattr(d.passing_plays,'success_rate',None),
            getattr(d,'rushing_plays',None) and getattr(d.rushing_plays,'explosiveness',None),
            getattr(d,'passing_plays',None) and getattr(d.passing_plays,'explosiveness',None),
        ))
        saved += 1
    except Exception as e:
        print(f"Error on {s.team}: {e}")

conn.commit()
conn.close()
print(f"Saved {saved} teams")

# Verify
conn = sqlite3.connect(os.path.join(BASE_DIR, 'cfb_data.db'))
cursor = conn.cursor()
cursor.execute("SELECT team, off_ppa, def_ppa, off_success_rate, def_success_rate FROM team_stats WHERE team IN ('Penn State','Alabama','Georgia')")
for r in cursor.fetchall():
    print(r)
conn.close()