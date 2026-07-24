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

echo "── [1/12] player box scores + PPA (fetch_data) ──"
$PY fetch_data.py
echo "── [2/12] team stats (fetch_team_stats) ──"
$PY fetch_team_stats.py
echo "── [3/12] advanced team stats (fetch_advanced) ──"
$PY fetch_advanced.py
echo "── [4/12] SP+ ratings (fetch_sp) ──"
$PY fetch_sp.py
echo "── [5/12] AP rankings (fetch_rankings) ──"
$PY fetch_rankings.py
echo "── [6/12] head coaches, current season (fetch_coaches) ──"
# Supplementary (team-page hero only) and CFBD publishes the new season late, so
# a failure/empty response must not abort the pipeline — keep going regardless.
$PY fetch_coaches.py || echo "  coach fetch failed — non-critical, continuing"
echo "── [7/12] game summaries / drives (fetch_game_summaries) ──"
$PY fetch_game_summaries.py
echo "── [8/12] Savant ratings (compute_savant_ratings) ──"
$PY compute_savant_ratings.py --write   # --write persists; without it the script only dry-runs
echo "── [9/12] percentile peer pools (backfill_pools) ──"
$PY backfill_pools.py
echo "── [10/12] team-page + returning-production precompute (precompute) ──"
$PY precompute.py
echo "── [11/12] Vegas lines, active season (fetch_betting_lines) ──"
$PY fetch_betting_lines.py
echo "── [12/12] Savant Forecast: score last week + predict upcoming (predict_games) ──"
$PY predict_games.py

echo "weekly pipeline complete"
