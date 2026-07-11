import psycopg2
import cfbd
import os
from dotenv import load_dotenv

load_dotenv()

configuration = cfbd.Configuration(
    access_token=os.getenv("CFBD_API_KEY")
)

conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cursor = conn.cursor()

# Clear tables that get refreshed
cursor.execute('DELETE FROM games')
cursor.execute('DELETE FROM player_stats')
cursor.execute('DELETE FROM player_ppa')

with cfbd.ApiClient(configuration) as api_client:
    games_api = cfbd.GamesApi(api_client)
    result = games_api.get_games(2025)

    stats_api = cfbd.StatsApi(api_client)
    stats = stats_api.get_player_season_stats(year=2025, season_type='regular')

    metrics_api = cfbd.MetricsApi(api_client)
    ppa_data = metrics_api.get_predicted_points_added_by_player_season(year=2025)

# Save games
for game in result:
    if game.home_classification == 'fbs':
        cursor.execute('''
            INSERT INTO games (id, season, week, season_type, home_team, home_points, away_team, away_points, completed, start_date, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''', (game.id, game.season, game.week, str(game.season_type), game.home_team, game.home_points, game.away_team, game.away_points, game.completed, str(game.start_date), game.notes))

# Save player stats
for s in stats:
    cursor.execute('''
        INSERT INTO player_stats (player_id, player_name, team, conference, position, category, stat_type, stat)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ''', (s.player_id, s.player, s.team, s.conference, s.position, s.category, s.stat_type, s.stat))

# Save PPA
for p in ppa_data:
    cursor.execute('''
        INSERT INTO player_ppa (player_id, player_name, position, team, conference, avg_ppa_all, avg_ppa_pass, avg_ppa_rush, total_ppa)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ''', (p.id, p.name, p.position, p.team, p.conference,
          p.average_ppa.all, p.average_ppa.var_pass, p.average_ppa.rush, p.total_ppa.all))

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
