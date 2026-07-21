#!/usr/bin/env bash
#
# Weekly in-season data pipeline for Saturday Savant.
#
# Fetches are NOT otherwise automated — this is the single ordered chain that
# refreshes every table CFBD updates after Saturday's games, then rebuilds the
# derived stores (savant ratings, percentile pools, team-page precomputes) so
# no visitor pays a cold live computation.
#
# Run order matters: derived data depends on the fetched tables, and precompute
# depends on all of it. `set -e` aborts the chain if any step fails, leaving the
# previous week's stores intact rather than half-refreshed.
#
# Cron: run weekly after CFBD has posted the week's data (see RENDER_CRON.md).
# Manual fallback:  bash run_weekly.sh
#
# NOTE: roster / transfer / NFL-status / offseason scripts are event-driven, not
# weekly, so they are intentionally NOT in this chain — run them by hand during
# the transfer-portal windows, signing day, and the post-draft NFL update.

set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

echo "── [1/11] player box scores + PPA (fetch_data) ──"
$PY fetch_data.py
echo "── [2/11] team stats (fetch_team_stats) ──"
$PY fetch_team_stats.py
echo "── [3/11] advanced team stats (fetch_advanced) ──"
$PY fetch_advanced.py
echo "── [4/11] SP+ ratings (fetch_sp) ──"
$PY fetch_sp.py
echo "── [5/11] AP rankings (fetch_rankings) ──"
$PY fetch_rankings.py
echo "── [6/11] game summaries / drives (fetch_game_summaries) ──"
$PY fetch_game_summaries.py
echo "── [7/11] Savant ratings (compute_savant_ratings) ──"
$PY compute_savant_ratings.py --write   # --write persists; without it the script only dry-runs
echo "── [8/11] percentile peer pools (backfill_pools) ──"
$PY backfill_pools.py
echo "── [9/11] team-page + returning-production precompute (precompute) ──"
$PY precompute.py
echo "── [10/11] Vegas lines, active season (fetch_betting_lines) ──"
$PY fetch_betting_lines.py
echo "── [11/11] Savant Forecast: score last week + predict upcoming (predict_games) ──"
$PY predict_games.py

echo "weekly pipeline complete"
