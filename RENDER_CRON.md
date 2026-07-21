# Weekly data pipeline — Render Cron Job

The CFBD fetch scripts were run **manually** up to this point — there was no
scheduler in the repo (only the web `Procfile`). This sets up the one place that
runs the whole weekly chain automatically: fetch → derive → precompute, via
[`run_weekly.sh`](run_weekly.sh).

## What runs

`run_weekly.sh` runs, in order (`set -e` aborts on any failure):

1. `fetch_data.py` — player box scores + PPA
2. `fetch_team_stats.py` — team stats
3. `fetch_advanced.py` — advanced team stats
4. `fetch_sp.py` — SP+ ratings
5. `fetch_rankings.py` — AP rankings
6. `fetch_game_summaries.py` — game summaries / drives
7. `compute_savant_ratings.py` — Savant ratings → `savant_ratings`
8. `backfill_pools.py` — percentile peer pools → `pool_store`
9. `precompute.py` — team-page + returning-production precompute → `pool_store`

Steps 8–9 **delete their stale `pool_store` keys before rebuilding**, so a
re-run refreshes against the newly-fetched tables instead of reading last week's
values back out.

Roster / transfer / NFL-status / offseason scripts are event-driven, not weekly
— run them by hand during the portal windows, signing day, and the post-draft
NFL update. They are intentionally excluded from the chain.

## Create the Render Cron Job

Render Cron Jobs are a **separate service type** (~$1/mo minimum — consistent
with the $1/mo CFBD tier). The Starter web service does not run cron itself.

1. Render Dashboard → **New +** → **Cron Job**.
2. Connect this repository, branch `main`.
3. **Runtime:** Python 3.
4. **Build Command:** `pip install -r requirements.txt`
5. **Command:** `bash run_weekly.sh`
6. **Schedule (UTC):** `0 10 * * 1` — Mondays 10:00 UTC (~5–6am ET), safely
   after Sunday's late games and CFBD ingestion. Adjust if CFBD lags.
7. **Environment variables** — set the same two the web service uses:
   - `DATABASE_URL` — the Render Postgres connection string (shared with the
     web service, so the precomputed `pool_store` rows are the ones the site
     reads).
   - `CFBD_API_KEY` — the CFBD API token.

## Why the stores survive deploys

The precomputed data lives in Postgres (`pool_store`, `savant_ratings`, …), not
in the web service's in-process `SimpleCache`. A deploy or `/admin/clear-cache`
wipes only the in-memory page cache; the precomputed stores persist and refresh
**only on this cron schedule** (or self-heal on a cache miss).

## Manual fallback

If the cron is ever paused, run the whole chain by hand from the project root:

```bash
bash run_weekly.sh
```

Or just the precompute step (after a manual fetch), for all or specific seasons:

```bash
python3 precompute.py            # all loaded seasons
python3 precompute.py 2024 2025  # specific seasons
```
