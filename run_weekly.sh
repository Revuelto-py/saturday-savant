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
# Cron: run weekly after CFBD has posted the week's data (see docs/RENDER_CRON.md).
# Manual fallback:  bash run_weekly.sh
#
# NOTE: roster / transfer / NFL-status / offseason scripts are event-driven, not
# weekly, so they are intentionally NOT in this chain — run them by hand during
# the transfer-portal windows, signing day, and the post-draft NFL update.

set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

echo "── [1/16] player box scores + PPA (fetch_data) ──"
$PY pipeline/fetch_data.py
echo "── [2/16] team stats (fetch_team_stats) ──"
$PY pipeline/fetch_team_stats.py
echo "── [3/16] advanced team stats (fetch_advanced) ──"
$PY pipeline/fetch_advanced.py
echo "── [4/16] SP+ ratings (fetch_sp) ──"
$PY pipeline/fetch_sp.py
echo "── [5/16] AP rankings (fetch_rankings) ──"
$PY pipeline/fetch_rankings.py
echo "── [6/16] head coaches, current season (fetch_coaches) ──"
# Supplementary (team-page hero only) and CFBD publishes the new season late, so
# a failure/empty response must not abort the pipeline — keep going regardless.
$PY pipeline/fetch_coaches.py || echo "  coach fetch failed — non-critical, continuing"
echo "── [7/16] team rosters, current season (fetch_2026_roster) ──"
# Rosters churn all season (injuries, dismissals, mid-year departures), and this
# is what removes a departed player: Trebor Pena sat on Penn State's roster
# after signing with Jacksonville because nothing refreshed it. Runs BEFORE the
# two steps that key off the roster — EA matching happens at ingest, and
# headshots are fetched per active player — so a newcomer picked up here gets a
# rating and a photo in the same run. Non-fatal, and it aborts internally
# rather than writing a partial roster if CFBD drops teams mid-fetch.
$PY pipeline/fetch_2026_roster.py || echo "  roster fetch failed — keeping last week's roster, continuing"
echo "── [8/16] EA ratings, starter-model input (fetch_ea_ratings) ──"
# Internal-only signal for lineup/starter selection, never displayed. EA
# publishes roster updates through the season, so a stale table quietly means
# wrong starters. Non-fatal by design: it scrapes a third-party page, and the
# script refuses to overwrite on a short/blocked fetch (EA_MIN_ROWS), so the
# worst case is last week's ratings — not a broken pipeline.
$PY pipeline/fetch_ea_ratings.py || echo "  EA ratings fetch failed — keeping previous ratings, continuing"
echo "── [9/16] player headshots, current roster (refresh_headshots) ──"
# Only the current roster: historical images change ~1% a year against ~47% for
# the roster at a season's photo drop, so the weekly pass sweeps 15k players
# rather than 44k. Compares ESPN against what's already in R2 (NOT a local
# mirror — this container has none), so it moves bytes only where a photo
# actually changed. Non-fatal: a CDN hiccup leaves last week's images, which is
# a stale photo, not a broken page.
$PY pipeline/refresh_headshots.py --active-only || echo "  headshot refresh failed — keeping existing images, continuing"
echo "── [10/16] game summaries / drives (fetch_game_summaries) ──"
$PY pipeline/fetch_game_summaries.py
# Play-level passing (air yards / pass location / YAC). Sits after the box-score
# fetch because it only carries games already marked complete.
# Non-fatal: these feed additive charts that nothing downstream reads, so a CFBD
# hiccup here must not abort the ratings and precompute below.
echo "── [11/16] play-level passing: air yards / location / YAC (fetch_passing) ──"
$PY pipeline/fetch_passing.py || echo "  (passing fetch failed — charts keep last week's data)"
echo "── [12/16] Savant ratings (compute_savant_ratings) ──"
$PY pipeline/compute_savant_ratings.py --write   # --write persists; without it the script only dry-runs
echo "── [13/16] percentile peer pools (backfill_pools) ──"
$PY pipeline/backfill_pools.py
echo "── [14/16] team-page + returning-production precompute (precompute) ──"
$PY pipeline/precompute.py
echo "── [15/16] Vegas lines, active season (fetch_betting_lines) ──"
$PY pipeline/fetch_betting_lines.py
echo "── [16/16] Savant Forecast: score last week + predict upcoming (predict_games) ──"
$PY pipeline/predict_games.py

echo "weekly pipeline complete"
