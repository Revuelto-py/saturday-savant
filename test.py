import requests
import cfbd
import os
from dotenv import load_dotenv

load_dotenv()

configuration = cfbd.Configuration(access_token=os.getenv("CFBD_API_KEY"))

with cfbd.ApiClient(configuration) as api_client:
    teams_api = cfbd.TeamsApi(api_client)
    roster = teams_api.get_roster(year=2025, team='Alabama')

hits = 0
misses = 0
for player in roster[:20]:
    url = f"https://a.espncdn.com/i/headshots/college-football/players/full/{player.id}.png"
    r = requests.get(url)
    if r.status_code == 200:
        hits += 1
        print(f"✓ {player.first_name} {player.last_name}")
    else:
        misses += 1
        print(f"✗ {player.first_name} {player.last_name}")

print(f"\nHits: {hits}, Misses: {misses}")