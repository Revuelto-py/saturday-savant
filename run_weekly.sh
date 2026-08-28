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

echo "── [1/15] player box scores + PPA (fetch_data) ──"
$PY fetch_data.py
echo "── [2/15] team stats (fetch_team_stats) ──"
$PY fetch_team_stats.py
echo "── [3/15] advanced team stats (fetch_advanced) ──"
$PY fetch_advanced.py
echo "── [4/15] SP+ ratings (fetch_sp) ──"
$PY fetch_sp.py
echo "── [5/15] AP rankings (fetch_rankings) ──"
$PY fetch_rankings.py
echo "── [6/15] head coaches, current season (fetch_coaches) ──"
# Supplementary (team-page hero only) and CFBD publishes the new season late, so
# a failure/empty response must not abort the pipeline — keep going regardless.
$PY fetch_coaches.py || echo "  coach fetch failed — non-critical, continuing"
echo "── [7/15] team rosters, current season (fetch_2026_roster) ──"
# Rosters churn all season (injuries, dismissals, mid-year departures), and this
# is what removes a departed player: Trebor Pena sat on Penn State's roster
# after signing with Jacksonville because nothing refreshed it. Runs BEFORE the
# two steps that key off the roster — EA matching happens at ingest, and
# headshots are fetched per active player — so a newcomer picked up here gets a
# rating and a photo in the same run. Non-fatal, and it aborts internally
# rather than writing a partial roster if CFBD drops teams mid-fetch.
$PY fetch_2026_roster.py || echo "  roster fetch failed — keeping last week's roster, continuing"
echo "── [8/15] EA ratings, starter-model input (fetch_ea_ratings) ──"
# Internal-only signal for lineup/starter selection, never displayed. EA
# publishes roster updates through the season, so a stale table quietly means
# wrong starters. Non-fatal by design: it scrapes a third-party page, and the
# script refuses to overwrite on a short/blocked fetch (EA_MIN_ROWS), so the
# worst case is last week's ratings — not a broken pipeline.
$PY fetch_ea_ratings.py || echo "  EA ratings fetch failed — keeping previous ratings, continuing"
echo "── [9/15] player headshots, current roster (refresh_headshots) ──"
# Only the current roster: historical images change ~1% a year against ~47% for
# the roster at a season's photo drop, so the weekly pass sweeps 15k players
# rather than 44k. Compares ESPN against what's already in R2 (NOT a local
# mirror — this container has none), so it moves bytes only where a photo
# actually changed. Non-fatal: a CDN hiccup leaves last week's images, which is
# a stale photo, not a broken page.
$PY refresh_headshots.py --active-only || echo "  headshot refresh failed — keeping existing images, continuing"
echo "── [10/15] game summaries / drives (fetch_game_summaries) ──"
$PY fetch_game_summaries.py
echo "── [11/15] Savant ratings (compute_savant_ratings) ──"
$PY compute_savant_ratings.py --write   # --write persists; without it the script only dry-runs
echo "── [12/15] percentile peer pools (backfill_pools) ──"
$PY backfill_pools.py
echo "── [13/15] team-page + returning-production precompute (precompute) ──"
$PY precompute.py
echo "── [14/15] Vegas lines, active season (fetch_betting_lines) ──"
$PY fetch_betting_lines.py
echo "── [15/15] Savant Forecast: score last week + predict upcoming (predict_games) ──"
$PY predict_games.py

echo "weekly pipeline complete"
