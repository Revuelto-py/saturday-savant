# Saturday Savant

College football analytics website built with Flask, SQLite, and the CFBD API.

## Setup
1. `pip install -r requirements.txt`
2. Create a `.env` file with `CFBD_API_KEY=your_key_here`
3. Run the `fetch_*.py` scripts to populate `cfb_data.db`
4. `python main.py`

## Features
Team pages, player pages, game detail pages with box scores and win probability, 
AP rankings, leaderboards, transfer portal, rivalries.
