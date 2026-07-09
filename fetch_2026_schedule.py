"""Ingest the 2026 FBS season schedule into the games table.

Follows the same conventions as fetch_data.py (CFBD access_token config,
direct psycopg2 connection), but is scoped to 2026 and non-destructive: it
only touches season=2026 rows, so the completed 2025 season is left intact.

2026 games are (this far out) unplayed — no scores, completed=false. They are
stored with NULL points and completed=0; the game page, /games hub, and team
schedules render these as scheduled kickoffs (date/time) rather than finals.

Re-runnable: rows are upserted on the primary key, so running it again after
CFBD updates/adds 2026 games refreshes the schedule without duplicating.

Conference realignment is inherently reflected: games store only team names
and every page joins to the (already-realigned) teams table for conference,
logo, and color — so a team's 2026 conference follows from teams, not here.
"""
import cfbd
import psycopg2
import os
from dotenv import load_dotenv

# Load .env by absolute path so the script works regardless of the cwd it's
# launched from (a plain load_dotenv() can miss it depending on where python
# is invoked).
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=True)

SEASON = 2026

configuration = cfbd.Configuration(access_token=os.getenv("CFBD_API_KEY"))
conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cursor = conn.cursor()

# Kickoff times for future games are often not set yet (about half the 2026
# slate this far out). Track CFBD's start_time_tbd flag so schedules/pages can
# show the date with a "TBD" time instead of a bogus midnight kickoff.
try:
    cursor.execute('ALTER TABLE games ADD COLUMN start_time_tbd INTEGER DEFAULT 0')
    conn.commit()
    print("Added start_time_tbd column")
except Exception:
    conn.rollback()

all_games = []
with cfbd.ApiClient(configuration) as api_client:
    games_api = cfbd.GamesApi(api_client)
    # Bowls/playoff aren't scheduled this early, but ask for both so a later
    # re-fetch picks the postseason up automatically once CFBD publishes it.
    for season_type in ('regular', 'postseason'):
        try:
            result = games_api.get_games(SEASON, season_type=season_type)
            all_games.extend(result)
            print(f"{SEASON} {season_type}: {len(result)} games returned")
        except Exception as e:
            print(f"{SEASON} {season_type}: fetch failed — {type(e).__name__}: {str(e)[:120]}")

# Only FBS home games, matching how fetch_data.py scopes the 2025 season.
# home_classification is a DivisionClassification enum whose value is 'fbs';
# it compares equal to the string, so keep the same comparison fetch_data uses.
fbs_games = [g for g in all_games if g.home_classification == 'fbs']
print(f"FBS games (home_classification=fbs): {len(fbs_games)} of {len(all_games)}")

# Non-destructive refresh: clear only this season's rows, then insert.
cursor.execute('DELETE FROM games WHERE season = %s', (SEASON,))

inserted = 0
completed_count = 0
for game in fbs_games:
    is_completed = 1 if getattr(game, 'completed', False) else 0
    completed_count += is_completed
    time_tbd = 1 if getattr(game, 'start_time_tbd', False) else 0
    cursor.execute('''
        INSERT INTO games (id, season, week, season_type, home_team, home_points,
                           away_team, away_points, completed, start_date, notes, start_time_tbd)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            season = EXCLUDED.season, week = EXCLUDED.week,
            season_type = EXCLUDED.season_type,
            home_team = EXCLUDED.home_team, home_points = EXCLUDED.home_points,
            away_team = EXCLUDED.away_team, away_points = EXCLUDED.away_points,
            completed = EXCLUDED.completed, start_date = EXCLUDED.start_date,
            notes = EXCLUDED.notes, start_time_tbd = EXCLUDED.start_time_tbd
    ''', (game.id, game.season, game.week, str(game.season_type),
          game.home_team, game.home_points, game.away_team, game.away_points,
          is_completed, str(game.start_date) if game.start_date else None, game.notes, time_tbd))
    inserted += 1

conn.commit()

# Flag any 2026 team names that aren't in the teams table — these would render
# without a logo/conference and usually signal a name mismatch or a brand-new
# program the teams table hasn't been updated for yet.
cursor.execute('''
    SELECT DISTINCT t FROM (
        SELECT home_team AS t FROM games WHERE season = %s
        UNION SELECT away_team AS t FROM games WHERE season = %s
    ) s
    WHERE t NOT IN (SELECT name FROM teams)
    ORDER BY t
''', (SEASON, SEASON))
missing = [r[0] for r in cursor.fetchall()]

print(f"\n2026 games stored: {inserted} (completed={completed_count}, scheduled={inserted - completed_count})")
if missing:
    print(f"⚠ {len(missing)} team name(s) in 2026 schedule not found in teams table "
          f"(will render without logo/conference): {missing}")
else:
    print("All 2026 schedule team names resolve to the teams table ✓")

conn.close()
print("2026 schedule ingest complete")
