import psycopg2
import cfbd
import os
from dotenv import load_dotenv
from season_util import current_cfb_season

load_dotenv()

SEASON = current_cfb_season()

configuration = cfbd.Configuration(
    access_token=os.getenv("CFBD_API_KEY")
)

conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cursor = conn.cursor()

# Refresh ONLY the active season — these tables are multi-season (history is
# loaded by backfill_history.py and tagged by `season`), so an unscoped DELETE
# would wipe every prior year. Scope every delete to SEASON.
cursor.execute('DELETE FROM games WHERE season = %s', (SEASON,))
cursor.execute('DELETE FROM player_stats WHERE season = %s', (SEASON,))
cursor.execute('DELETE FROM player_ppa WHERE season = %s', (SEASON,))

with cfbd.ApiClient(configuration) as api_client:
    games_api = cfbd.GamesApi(api_client)
    result = games_api.get_games(SEASON)

    stats_api = cfbd.StatsApi(api_client)
    stats = stats_api.get_player_season_stats(year=SEASON, season_type='regular')

    metrics_api = cfbd.MetricsApi(api_client)
    ppa_data = metrics_api.get_predicted_points_added_by_player_season(year=SEASON)

# Save games
for game in result:
    if game.home_classification == 'fbs':
        cursor.execute('''
            INSERT INTO games (id, season, week, season_type, home_team, home_points, away_team, away_points, completed, start_date, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''', (game.id, game.season, game.week, str(game.season_type), game.home_team, game.home_points, game.away_team, game.away_points, 1 if game.completed else 0, str(game.start_date), game.notes))

# Save player stats
for s in stats:
    cursor.execute('''
        INSERT INTO player_stats (player_id, player_name, team, conference, position, category, stat_type, stat, season)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ''', (s.player_id, s.player, s.team, s.conference, s.position, s.category, s.stat_type, s.stat, SEASON))

# Save PPA
for p in ppa_data:
    cursor.execute('''
        INSERT INTO player_ppa (player_id, player_name, position, team, conference, avg_ppa_all, avg_ppa_pass, avg_ppa_rush, total_ppa, season)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (player_id, season) DO NOTHING
    ''', (p.id, p.name, p.position, p.team, p.conference,
          p.average_ppa.all, p.average_ppa.var_pass, p.average_ppa.rush, p.total_ppa.all, SEASON))

print(f"Games saved: {len(result)}")
print(f"Stats saved: {len(stats)}")
print(f"PPA saved: {len(ppa_data)}")

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
