"""Refresh head-coach assignments for the current ingest season from CFBD.

Background: the `coaches` table (team, season, coach, hire_date) was populated
for 2016–2025 by the one-off `fetch_forecast_extras.py` backfill (the Savant
Forecast exploration phase) and was NEVER added to the weekly cron. So the
current season's coaching carousel — offseason hires like a coach moving schools
— never landed, and the team-page hero fell back to the prior season's coach.

This script fetches the CURRENT ingest season (`season_util.current_cfb_season()`,
date-derived and auto-advancing) each week, so 2026 and every future season stay
current on their own. Idempotent per season: when CFBD returns data it does a
clean refresh (delete the season's rows, reinsert), so a coach who left is
removed and the new hire appears with a fresh tenure.

IMPORTANT — CFBD data lag: CFBD's coaches endpoint is record-based. It has NO
rows for an upcoming season until that season is under way (get_coaches(year=Y)
returns coaches with a Y season record). So an early-offseason run can legitimately
return 0 rows — that is CFBD not having published the new season yet, not a bug.
On a 0-row response this script leaves any existing rows untouched (it never
wipes good data on an empty fetch) and exits cleanly.

Connection pooling: main.get_db() / release_db() with try/finally, per convention.

Run:  python3 fetch_coaches.py [season]      # season defaults to the current one
"""
import os
import sys

os.environ.setdefault('POOL_BACKFILL', '1')

import cfbd
from dotenv import load_dotenv
from psycopg2.extras import execute_values

load_dotenv()

import main
from season_util import current_cfb_season

# ── Manual current-season hire overrides ────────────────────────────────────
# CFBD's coaches endpoint is record-based and has nothing for an upcoming season
# (see the module docstring), so it can't reflect the offseason carousel until
# the season is under way. Until CFBD publishes it, these confirmed head-coach
# hires — taken from a published coaching-changes roundup — are layered on top of
# CFBD so the team-page hero is current. Keyed to the DB's EXACT team names
# (verified against the teams table; note "Cal" is stored "California"). They fill
# ONLY teams CFBD hasn't published — once CFBD provides a team's coach it wins —
# so this whole block becomes a no-op and can be trimmed as CFBD catches up.
COACH_OVERRIDES = {
    2026: {
        'Michigan': 'Kyle Whittingham',      'Missouri State': 'Casey Woods',
        'Washington State': 'Kirby Moore',    'Coastal Carolina': 'Ryan Beard',
        'Southern Miss': 'Blake Anderson',    'Toledo': 'Mike Jacobs',
        'Tulane': 'Will Hall',                'UConn': 'Jason Candle',
        'Memphis': 'Charles Huff',            'Penn State': 'Matt Campbell',
        'California': 'Tosh Lupoi',           'James Madison': 'Billy Napier',
        'UAB': 'Alex Mortensen',              'South Florida': 'Brian Hartline',
        'North Texas': 'Neal Brown',          'Kentucky': 'Will Stein',
        'Michigan State': 'Pat Fitzgerald',   'UCLA': 'Bob Chesney',
        'Ole Miss': 'Pete Golding',           'LSU': 'Lane Kiffin',
        'Florida': 'Jon Sumrall',             'Auburn': 'Alex Golesh',
        'Arkansas': 'Ryan Silverfield',       'Stanford': 'Tavita Pritchard',
        'Oregon State': 'JaMarcus Shephard',  'Colorado State': 'Jim Mora',
        'Oklahoma State': 'Eric Morris',      'Virginia Tech': 'James Franklin',
        'Kent State': 'Mark Carney',
    },
}
# Placeholder hire date for an override: an offseason (>= Sept) date so the hero's
# hire_date-derived tenure reads "1st season" in the hire year (correct — every
# override is a coach's first year at that team). Only the derived tenure is shown,
# never the raw date, so a placeholder is safe; CFBD's real date replaces it later.
OVERRIDE_HIRE_DATE = '2025-12-01 00:00:00+00:00'


def fetch_coaches(season):
    key = os.getenv('CFBD_API_KEY')
    if not key:
        print('CFBD_API_KEY not set — cannot fetch coaches', flush=True)
        return

    with cfbd.ApiClient(cfbd.Configuration(access_token=key)) as api:
        coaches = cfbd.CoachesApi(api).get_coaches(year=season)

    # One row per team for the season. A team can list >1 coach in a year
    # (a mid-season change); keep the last listed so the (team, season) key is
    # unique within the batch — the same rule the original backfill used.
    by_team = {}
    for c in coaches:
        name = f"{c.first_name or ''} {c.last_name or ''}".strip()
        for s in (c.seasons or []):
            if s.year == season and s.school:
                by_team[s.school] = (s.school, season, name,
                                     str(getattr(c, 'hire_date', '') or ''))

    # Layer manual overrides on top, but only for teams CFBD hasn't published —
    # CFBD is authoritative wherever it has data.
    n_override = 0
    for team, coach in COACH_OVERRIDES.get(season, {}).items():
        if team not in by_team:
            by_team[team] = (team, season, coach, OVERRIDE_HIRE_DATE)
            n_override += 1

    rows = list(by_team.values())

    conn = main.get_db()
    try:
        cur = conn.cursor()
        cur.execute('''CREATE TABLE IF NOT EXISTS coaches (
            team TEXT, season INTEGER, coach TEXT, hire_date TEXT,
            PRIMARY KEY (team, season))''')

        if not rows:
            # Empty CFBD response (upcoming season not yet published, or a blip):
            # never wipe existing rows on nothing — just report and leave as-is.
            cur.execute('SELECT COUNT(*) FROM coaches WHERE season=%s', (season,))
            existing = cur.fetchone()[0]
            conn.commit()
            print(f'{season} coaches: CFBD returned 0 rows — CFBD has no data for '
                  f'this season yet; leaving {existing} existing row(s) untouched',
                  flush=True)
            return

        # Clean refresh so a departed coach is removed and hires appear fresh.
        cur.execute('DELETE FROM coaches WHERE season=%s', (season,))
        execute_values(cur, '''
            INSERT INTO coaches (team, season, coach, hire_date) VALUES %s
            ON CONFLICT (team, season) DO UPDATE SET
                coach=EXCLUDED.coach, hire_date=EXCLUDED.hire_date''', rows)
        conn.commit()
        via = f' ({len(rows) - n_override} from CFBD, {n_override} from manual overrides)' if n_override else ''
        print(f'{season} coaches: refreshed {len(rows)} teams{via}', flush=True)
    finally:
        main.release_db(conn)


if __name__ == '__main__':
    season = int(sys.argv[1]) if len(sys.argv) > 1 else current_cfb_season()
    print(f'Fetching head coaches for {season}…', flush=True)
    fetch_coaches(season)
