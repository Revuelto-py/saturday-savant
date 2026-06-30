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
    CREATE TABLE IF NOT EXISTS transfers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        first_name TEXT,
        last_name TEXT,
        position TEXT,
        origin TEXT,
        destination TEXT,
        transfer_date TEXT,
        rating REAL,
        stars INTEGER,
        eligibility TEXT,
        year INTEGER
    )
''')
conn.commit()

with cfbd.ApiClient(configuration) as api_client:
    players_api = cfbd.PlayersApi(api_client)

    for yr in [2024, 2025, 2026]:
        try:
            transfers = players_api.get_transfer_portal(year=yr)
            print(f"Year {yr}: {len(transfers)} transfers")

            cursor.execute('DELETE FROM transfers WHERE year=?', (yr,))

            saved = 0
            for t in transfers:
                # transfer_date is a datetime object
                td = getattr(t, 'transfer_date', None)
                td_str = td.isoformat() if td else None

                # eligibility is an enum
                elig = getattr(t, 'eligibility', None)
                elig_str = elig.value if hasattr(elig, 'value') else str(elig) if elig else None

                cursor.execute('''
                    INSERT INTO transfers
                    (first_name, last_name, position, origin, destination,
                     transfer_date, rating, stars, eligibility, year)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                ''', (
                    getattr(t, 'first_name', None),
                    getattr(t, 'last_name',  None),
                    getattr(t, 'position',   None),
                    getattr(t, 'origin',     None),
                    getattr(t, 'destination',None),
                    td_str,
                    getattr(t, 'rating',     None),
                    getattr(t, 'stars',      None),
                    elig_str,
                    yr,
                ))
                saved += 1

            conn.commit()
            print(f"  Saved {saved} transfers for {yr}")

            # Preview top rated
            cursor.execute('''
                SELECT first_name, last_name, position, origin, destination, stars, rating
                FROM transfers WHERE year=? AND destination IS NOT NULL
                ORDER BY rating DESC NULLS LAST LIMIT 10
            ''', (yr,))
            for r in cursor.fetchall():
                stars_str = ('★' * int(r[5])) if r[5] else '—'
                rating_str = f"{r[6]:.4f}" if r[6] else '—'
                print(f"  {stars_str} [{rating_str}] {r[0]} {r[1]} {r[2]}: {r[3]} → {r[4]}")

        except Exception as e:
            print(f"Year {yr} error: {e}")
            import traceback; traceback.print_exc()

# Update player teams from 2025 AND 2026 transfers (2026 applied last so it wins)
print("\nUpdating player teams from 2025 AND 2026 transfers...")
cursor.execute('''
    SELECT first_name, last_name, destination FROM transfers
    WHERE year IN (2025, 2026) AND destination IS NOT NULL AND destination != ''
    ORDER BY year ASC
''')
transfers_2025 = cursor.fetchall()

updated = 0
not_found = 0
for first, last, dest in transfers_2025:
    cursor.execute('''
        SELECT id, team FROM players
        WHERE first_name=? AND last_name=?
        LIMIT 1
    ''', (first, last))
    player = cursor.fetchone()

    if player and player[1] != dest:
        cursor.execute('SELECT name FROM teams WHERE name=?', (dest,))
        team_exists = cursor.fetchone()

        if team_exists:
            cursor.execute('UPDATE players SET team=? WHERE id=?', (dest, player[0]))
            updated += 1
            print(f"  Updated {first} {last}: {player[1]} → {dest}")
        else:
            cursor.execute("SELECT name FROM teams WHERE name LIKE ?", (f'%{dest[:6]}%',))
            fuzzy = cursor.fetchone()
            if fuzzy:
                cursor.execute('UPDATE players SET team=? WHERE id=?', (fuzzy[0], player[0]))
                updated += 1
                print(f"  Updated {first} {last}: {player[1]} → {fuzzy[0]} (fuzzy for '{dest}')")
    elif not player:
        not_found += 1

conn.commit()
print(f"\nUpdated {updated} players, {not_found} not found in DB")

conn.close()
print("Done!")
