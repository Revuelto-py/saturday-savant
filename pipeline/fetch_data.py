# This script lives one directory below the repo root; ROOT points back at it so
# .env, the model artifacts and the shared modules resolve the same as before.
import os as _os, sys as _sys
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if ROOT not in _sys.path:
    _sys.path.insert(0, ROOT)
import psycopg2
import cfbd
import os
from dotenv import load_dotenv
from cfbd_retry import call_with_retry
from divisions import fbs_team_names, keep_stat_row, tracked_player_ids
from season_util import current_cfb_season

load_dotenv(_os.path.join(ROOT, '.env'))

SEASON = current_cfb_season()

configuration = cfbd.Configuration(
    access_token=os.getenv("CFBD_API_KEY")
)

conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cursor = conn.cursor()

# Refresh ONLY the active season — these tables are multi-season (history is
# loaded by backfill/backfill_history.py and tagged by `season`), so an unscoped DELETE
# would wipe every prior year. Scope every delete to SEASON.
cursor.execute('DELETE FROM games WHERE season = %s', (SEASON,))
cursor.execute('DELETE FROM player_stats WHERE season = %s', (SEASON,))
cursor.execute('DELETE FROM player_ppa WHERE season = %s', (SEASON,))

with cfbd.ApiClient(configuration) as api_client:
    games_api = cfbd.GamesApi(api_client)
    # Every read retries a transient CFBD 5xx rather than aborting the weekly
    # chain (run_weekly.sh runs under `set -e`). All three happen before the
    # first DELETE below, so a fetch that does fail still leaves last week's
    # tables intact.
    result = call_with_retry('games', games_api.get_games, SEASON)

    stats_api = cfbd.StatsApi(api_client)
    # 'both' = regular + postseason combined, so bowl/CFP production counts
    # toward season totals (e.g. a sack in the CFP shows in the season sack
    # total). CFBD returns one combined row per player/stat for 'both'.
    stats = call_with_retry('player season stats',
                            stats_api.get_player_season_stats,
                            year=SEASON, season_type='both')

    metrics_api = cfbd.MetricsApi(api_client)
    ppa_data = call_with_retry(
        'player ppa', metrics_api.get_predicted_points_added_by_player_season,
        year=SEASON)

# Save games
for game in result:
    if game.home_classification == 'fbs':
        cursor.execute('''
            INSERT INTO games (id, season, week, season_type, home_team, home_points, away_team, away_points, completed, start_date, notes, neutral_site)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''', (game.id, game.season, game.week, str(game.season_type), game.home_team, game.home_points, game.away_team, game.away_points, 1 if game.completed else 0, str(game.start_date), game.notes, 1 if getattr(game, 'neutral_site', False) else 0))

# CFBD returns every division in one payload. Store the FBS half (plus anyone
# who already has a page here) — see divisions.py. Before this, half of the
# season's player_stats rows were FCS and D-II players no page can reach.
FBS_NAMES = fbs_team_names(cursor)
TRACKED = tracked_player_ids(cursor)

# Save player stats
stats_saved = 0
for s in stats:
    if not keep_stat_row(s.team, s.player_id, FBS_NAMES, TRACKED):
        continue
    cursor.execute('''
        INSERT INTO player_stats (player_id, player_name, team, conference, position, category, stat_type, stat, season)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (player_id, season, team, category, stat_type) DO UPDATE
           SET stat = EXCLUDED.stat, player_name = EXCLUDED.player_name,
               conference = EXCLUDED.conference, position = EXCLUDED.position
    ''', (s.player_id, s.player, s.team, s.conference, s.position, s.category, s.stat_type, s.stat, SEASON))
    stats_saved += 1

# Save PPA
ppa_saved = 0
for p in ppa_data:
    if not keep_stat_row(p.team, p.id, FBS_NAMES, TRACKED):
        continue
    cursor.execute('''
        INSERT INTO player_ppa (player_id, player_name, position, team, conference, avg_ppa_all, avg_ppa_pass, avg_ppa_rush, total_ppa, season)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (player_id, season) DO NOTHING
    ''', (p.id, p.name, p.position, p.team, p.conference,
          p.average_ppa.all, p.average_ppa.var_pass, p.average_ppa.rush, p.total_ppa.all, SEASON))
    ppa_saved += 1

print(f"Games saved: {len(result)}")
print(f"Stats saved: {stats_saved} FBS (of {len(stats)} read)")
print(f"PPA saved: {ppa_saved} FBS (of {len(ppa_data)} read)")

conn.commit()
conn.close()

print("Data updated")


# Data changed — tell the live site to drop its in-memory page cache so the
# update is visible immediately instead of after the cache TTL.
try:
    from cache_notify import notify_cache_clear
    notify_cache_clear()
except Exception:
    pass
