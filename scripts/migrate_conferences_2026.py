"""
2026 FBS conference realignment migration.

Updates team conference assignments for the realignment that took effect
July 1, 2026 (Pac-12 rebuild from Mountain West/Sun Belt members, Mountain
West backfill from Conference USA/MAC, and two new football-only FBS
programs joining from FCS).

Run:
    cd /Users/diegotorres/coding/Projects/gridironIQ
    python scripts/migrate_conferences_2026.py

Connects via DATABASE_URL, runs every change in a single transaction
(commit at the end, rollback on any unexpected error), and prints a
confirmation of what changed. Teams that aren't found are reported as
warnings and skipped rather than crashing the run.

Note: teams.name has no UNIQUE/exclusion constraint in this database (only
teams.id is a primary key), so new teams are upserted by checking for an
existing row first rather than via ON CONFLICT.
"""
import os
import sys

import psycopg2
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv('DATABASE_URL')
CFBD_API_KEY = os.getenv('CFBD_API_KEY')
CFBD_YEAR = 2026

# name -> new conference. Matches the existing naming convention already used
# in the teams table (confirmed via `SELECT DISTINCT conference FROM teams`)
# — e.g. the MAC is stored as 'Mid-American', not 'MAC'.
CONFERENCE_UPDATES = {
    # Pac-12 additions (from Mountain West)
    'Boise State':      'Pac-12',
    'Colorado State':   'Pac-12',
    'Fresno State':     'Pac-12',
    'San Diego State':  'Pac-12',
    'Utah State':       'Pac-12',
    # Pac-12 addition (from Sun Belt)
    'Texas State':       'Pac-12',
    # Mountain West additions
    'UTEP':              'Mountain West',   # from Conference USA
    'Northern Illinois':  'Mountain West',   # from MAC
    # Conference USA departure (Louisiana Tech's move is uncertain per the
    # task, but it exists in this DB and CFBD's 2026 data already shows it
    # in Sun Belt, so it's applied here)
    'Louisiana Tech':    'Sun Belt',
}

# Hawai'i is already a full-time Mountain West member in this database (the
# team is stored with an apostrophe: "Hawai'i", not "Hawaii") — confirmed,
# not updated.
HAWAII_NAME = "Hawai'i"

# New FBS teams (football-only) moving up from FCS: (name, conference, abbreviation)
NEW_FBS_TEAMS = [
    ('Sacramento State',   'Mid-American',   'SAC'),   # from FCS Big Sky
    ('North Dakota State', 'Mountain West',  'NDSU'),  # from FCS MVFC
]

CONFIRM_NAMES = [
    'Boise State', 'Colorado State', 'Fresno State', 'San Diego State',
    'Utah State', 'Texas State', 'UTEP', 'Northern Illinois', HAWAII_NAME,
    'North Dakota State', 'Sacramento State', 'Louisiana Tech',
]

RIVALRY_CHECK_TEAMS = [
    'Boise State', 'Colorado State', 'Fresno State',
    'San Diego State', 'Utah State', 'Texas State',
]


def update_conferences(cursor):
    print("\n--- CHANGE 1: Updating conference assignments ---")
    for name, new_conf in CONFERENCE_UPDATES.items():
        cursor.execute('SELECT conference FROM teams WHERE name = %s', (name,))
        row = cursor.fetchone()
        if row is None:
            print(f"  WARNING: team '{name}' not found in teams table — skipped")
            continue
        old_conf = row[0]
        cursor.execute('UPDATE teams SET conference = %s WHERE name = %s', (new_conf, name))
        print(f"  {name}: {old_conf!r} -> {new_conf!r}")

    cursor.execute('SELECT conference FROM teams WHERE name = %s', (HAWAII_NAME,))
    row = cursor.fetchone()
    if row is None:
        print(f"  WARNING: '{HAWAII_NAME}' not found in teams table")
    elif row[0] == 'Mountain West':
        print(f"  {HAWAII_NAME}: already 'Mountain West' — no change needed")
    else:
        cursor.execute('UPDATE teams SET conference = %s WHERE name = %s', ('Mountain West', HAWAII_NAME))
        print(f"  {HAWAII_NAME}: {row[0]!r} -> 'Mountain West'")


def upsert_new_fbs_teams(cursor):
    print("\n--- CHANGE 1 (new teams): Inserting/updating new FBS programs ---")
    # teams.id has no sequence/default in this database (confirmed via
    # pg_get_serial_sequence — it's a plain NOT NULL integer primary key), so
    # new ids are assigned manually rather than relying on an autoincrement.
    cursor.execute('SELECT COALESCE(MAX(id), 0) FROM teams')
    next_id = cursor.fetchone()[0] + 1

    for name, conf, abbr in NEW_FBS_TEAMS:
        cursor.execute('SELECT id FROM teams WHERE name = %s', (name,))
        row = cursor.fetchone()
        if row:
            cursor.execute(
                'UPDATE teams SET conference = %s, abbreviation = %s WHERE name = %s',
                (conf, abbr, name)
            )
            print(f"  {name}: already existed (id={row[0]}), conference set to {conf!r}")
        else:
            cursor.execute(
                'INSERT INTO teams (id, name, conference, abbreviation) VALUES (%s, %s, %s, %s)',
                (next_id, name, conf, abbr)
            )
            print(f"  {name}: inserted new row (id={next_id}), conference={conf!r}")
            next_id += 1


def fetch_new_team_logos(cursor):
    print("\n--- CHANGE 2: Fetching CFBD logo/color data for new FBS teams ---")
    if not CFBD_API_KEY:
        print("  WARNING: CFBD_API_KEY not set in environment — skipping, logos left NULL")
        return

    cfbd_by_name = {}
    try:
        import cfbd
        configuration = cfbd.Configuration(access_token=CFBD_API_KEY)
        with cfbd.ApiClient(configuration) as api_client:
            teams_api = cfbd.TeamsApi(api_client)
            cfbd_teams = teams_api.get_teams(year=CFBD_YEAR)
        cfbd_by_name = {t.school: t for t in cfbd_teams}
    except Exception as e:
        print(f"  WARNING: CFBD API call failed ({e}) — logos left NULL for all new teams")

    for name, conf, abbr in NEW_FBS_TEAMS:
        t = cfbd_by_name.get(name)
        if not t:
            print(f"  WARNING: '{name}' not found in CFBD {CFBD_YEAR} teams data — logo/color left NULL")
            continue
        try:
            logos = getattr(t, 'logos', None) or []
            logo = logos[0] if len(logos) > 0 else None
            logo_dark = logos[1] if len(logos) > 1 else logo
            color = getattr(t, 'color', None)
            alt_color = getattr(t, 'alternate_color', None)
            cfbd_abbr = getattr(t, 'abbreviation', None) or abbr
            cursor.execute('''
                UPDATE teams SET logo = %s, logo_dark = %s, color = %s, alt_color = %s, abbreviation = %s
                WHERE name = %s
            ''', (logo, logo_dark, color, alt_color, cfbd_abbr, name))
            print(f"  {name}: logo/color populated from CFBD (abbreviation={cfbd_abbr}, color={color})")
        except Exception as e:
            print(f"  WARNING: failed to apply CFBD data for '{name}' ({e}) — logo/color left NULL")


def print_confirmation(cursor):
    print("\n--- Final conference assignments ---")
    cursor.execute('''
        SELECT name, conference FROM teams
        WHERE name = ANY(%s)
        ORDER BY conference, name
    ''', (CONFIRM_NAMES,))
    rows = cursor.fetchall()
    for name, conf in rows:
        print(f"  {name:<22} {conf}")
    missing = set(CONFIRM_NAMES) - {r[0] for r in rows}
    if missing:
        print(f"  (not found in teams table: {sorted(missing)})")


def check_rivalries(cursor):
    print("\n--- CHANGE 5: Rivalries referencing newly-realigned Pac-12 teams ---")
    cursor.execute('''
        SELECT team1, team2, rivalry_name FROM rivalries
        WHERE team1 = ANY(%s) OR team2 = ANY(%s)
        ORDER BY id
    ''', (RIVALRY_CHECK_TEAMS, RIVALRY_CHECK_TEAMS))
    rows = cursor.fetchall()
    for row in rows:
        print(f"  {row}")
    referencing_old_conf = [r for r in rows if r[2] and (
        'mountain west' in r[2].lower() or 'conference usa' in r[2].lower()
        or 'sun belt' in r[2].lower() or 'mid-american' in r[2].lower() or ' mac ' in f' {r[2].lower()} '
    )]
    print(f"  {len(rows)} rivalry rows found referencing these teams.")
    if referencing_old_conf:
        print("  NOTE: the following rivalry_name values mention a conference by name "
              "and may need manual review:")
        for r in referencing_old_conf:
            print(f"    {r}")
    else:
        print("  None of the rivalry_name text references a conference by name — no text changes needed.")


def main():
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL not set in environment.")
        sys.exit(1)

    conn = psycopg2.connect(dsn=DATABASE_URL)
    try:
        cursor = conn.cursor()
        print("=" * 72)
        print("2026 FBS CONFERENCE REALIGNMENT MIGRATION")
        print("=" * 72)

        update_conferences(cursor)
        upsert_new_fbs_teams(cursor)
        fetch_new_team_logos(cursor)

        conn.commit()
        print("\nTransaction committed.")

        print_confirmation(cursor)
        check_rivalries(cursor)

    except Exception as e:
        conn.rollback()
        print(f"\nERROR — transaction rolled back, no changes were saved: {e}")
        raise
    finally:
        conn.close()

    print("\nDone.")


if __name__ == '__main__':
    main()
