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
# Cron: Sundays 12:00 UTC (08:00 ET) — see docs/RENDER_CRON.md. Saturday's
# latest kickoff is 23:59 ET, so the last game of a weekend ends around
# 03:30 ET Sunday; this leaves CFBD roughly four hours to post the data and
# still refreshes the site about a day earlier than the old Monday slot.
#
# AP rankings are step 5 here, but they ALSO have their own hourly cron.
# The poll's release day moves (Sunday most weeks, Tuesday when week 1 runs
# through Labor Day, January for the final), so this chain must not be the
# only thing that can pick one up.
#
# Manual fallback:  bash run_weekly.sh
#
# NOTE: roster / transfer / NFL-status / offseason scripts are event-driven, not
# weekly, so they are intentionally NOT in this chain — run them by hand during
# the transfer-portal windows, signing day, and the post-draft NFL update.

set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

echo "── [1/19] player box scores + PPA (fetch_data) ──"
$PY pipeline/fetch_data.py
# Week 0 is derived, not fetched: CFBD returns those games as week 1 and ignores
# a week=0 argument. This runs immediately after the only step that writes
# `games`, so everything derived below (Savant snapshots, precompute, forecasts)
# sees the corrected week. It runs again at the end for the tables written later.
echo "── [2/19] label week 0 (apply_week_zero) ──"
$PY pipeline/apply_week_zero.py || echo "  week 0 pass failed — weeks unchanged, continuing"
# Player game logs. main.py caches these per player-season and only rebuilds one
# when the cached copy predates the newest completed kickoff — refreshing them
# here in bulk (~17 CFBD calls for the whole player base) keeps that rebuild out
# of the request path, where it is ~13 blocking ESPN calls in a web worker.
# Runs after the week-0 pass because it reads `games.week` for each entry.
echo "── [3/19] player game logs, active season (backfill_game_logs) ──"
$PY backfill/backfill_game_logs.py --current || echo "  game-log refresh failed — logs rebuild lazily, continuing"
echo "── [4/19] team stats (fetch_team_stats) ──"
$PY pipeline/fetch_team_stats.py
echo "── [5/19] advanced team stats (fetch_advanced) ──"
$PY pipeline/fetch_advanced.py
echo "── [6/19] SP+ ratings (fetch_sp) ──"
$PY pipeline/fetch_sp.py
echo "── [7/19] AP rankings (fetch_rankings) ──"
$PY pipeline/fetch_rankings.py
echo "── [8/19] head coaches, current season (fetch_coaches) ──"
# Supplementary (team-page hero only) and CFBD publishes the new season late, so
# a failure/empty response must not abort the pipeline — keep going regardless.
$PY pipeline/fetch_coaches.py || echo "  coach fetch failed — non-critical, continuing"
echo "── [9/19] team rosters, current season (fetch_2026_roster) ──"
# Rosters churn all season (injuries, dismissals, mid-year departures), and this
# is what removes a departed player: Trebor Pena sat on Penn State's roster
# after signing with Jacksonville because nothing refreshed it. Runs BEFORE the
# two steps that key off the roster — EA matching happens at ingest, and
# headshots are fetched per active player — so a newcomer picked up here gets a
# rating and a photo in the same run. Non-fatal, and it aborts internally
# rather than writing a partial roster if CFBD drops teams mid-fetch.
$PY pipeline/fetch_2026_roster.py || echo "  roster fetch failed — keeping last week's roster, continuing"
echo "── [10/19] EA ratings, starter-model input (fetch_ea_ratings) ──"
# Internal-only signal for lineup/starter selection, never displayed. EA
# publishes roster updates through the season, so a stale table quietly means
# wrong starters. Non-fatal by design: it scrapes a third-party page, and the
# script refuses to overwrite on a short/blocked fetch (EA_MIN_ROWS), so the
# worst case is last week's ratings — not a broken pipeline.
$PY pipeline/fetch_ea_ratings.py || echo "  EA ratings fetch failed — keeping previous ratings, continuing"
echo "── [11/19] player headshots, current roster (refresh_headshots) ──"
# Only the current roster: historical images change ~1% a year against ~47% for
# the roster at a season's photo drop, so the weekly pass sweeps 15k players
# rather than 44k. Compares ESPN against what's already in R2 (NOT a local
# mirror — this container has none), so it moves bytes only where a photo
# actually changed. Non-fatal: a CDN hiccup leaves last week's images, which is
# a stale photo, not a broken page.
$PY pipeline/refresh_headshots.py --active-only || echo "  headshot refresh failed — keeping existing images, continuing"
echo "── [12/19] game summaries / drives (fetch_game_summaries) ──"
$PY pipeline/fetch_game_summaries.py
# Play-level passing (air yards / pass location / YAC). Sits after the box-score
# fetch because it only carries games already marked complete.
# Non-fatal: these feed additive charts that nothing downstream reads, so a CFBD
# hiccup here must not abort the ratings and precompute below.
echo "── [13/19] play-level passing: air yards / location / YAC (fetch_passing) ──"
$PY pipeline/fetch_passing.py || echo "  (passing fetch failed — charts keep last week's data)"
echo "── [14/19] Savant ratings (compute_savant_ratings) ──"
$PY pipeline/compute_savant_ratings.py --write   # --write persists; without it the script only dry-runs
echo "── [15/19] percentile peer pools (backfill_pools) ──"
$PY pipeline/backfill_pools.py
echo "── [16/19] team-page + returning-production precompute (precompute) ──"
$PY pipeline/precompute.py
echo "── [17/19] Vegas lines, active season (fetch_betting_lines) ──"
$PY pipeline/fetch_betting_lines.py
echo "── [18/19] Savant Forecast: score last week + predict upcoming (predict_games) ──"
$PY pipeline/predict_games.py
# Second pass. betting_lines, passing plays and predictions keep their own copy
# of the week and are written above from CFBD, which calls a Week 0 game week 1
# — so they need relabelling after those steps, not before.
echo "── [19/19] label week 0 in the tables written since (apply_week_zero) ──"
$PY pipeline/apply_week_zero.py || echo "  week 0 pass failed — weeks unchanged, continuing"

echo "weekly pipeline complete"
