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
        print(f'{season} coaches: refreshed {len(rows)} teams', flush=True)
    finally:
        main.release_db(conn)


if __name__ == '__main__':
    season = int(sys.argv[1]) if len(sys.argv) > 1 else current_cfb_season()
    print(f'Fetching head coaches for {season}…', flush=True)
    fetch_coaches(season)
