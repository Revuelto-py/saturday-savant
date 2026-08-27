# Weekly data pipeline — Render Cron Job

The CFBD fetch scripts were run **manually** up to this point — there was no
scheduler in the repo (only the web `Procfile`). This sets up the one place that
runs the whole weekly chain automatically: fetch → derive → precompute, via
[`run_weekly.sh`](run_weekly.sh).

## What runs

`run_weekly.sh` runs, in order (`set -e` aborts on any failure):

1. `fetch_data.py` — games (incl. scores/completion) + player box scores + PPA
2. `fetch_team_stats.py` — team stats
3. `fetch_advanced.py` — advanced team stats
4. `fetch_sp.py` — SP+ ratings
5. `fetch_rankings.py` — AP rankings (every weekly poll, not just the final)
6. `fetch_coaches.py` — head coaches, current season *(non-fatal)*
7. `fetch_ea_ratings.py` — EA ratings, starter-model input *(non-fatal)*
8. `fetch_game_summaries.py` — game summaries / drives
9. `compute_savant_ratings.py` — Savant ratings → `savant_ratings`
10. `backfill_pools.py` — percentile peer pools → `pool_store`
11. `precompute.py` — team-page + returning-production precompute → `pool_store`
12. `fetch_betting_lines.py` — Vegas lines, active season
13. `predict_games.py` — Savant Forecast: score last week, predict upcoming

Steps 10–11 **delete their stale `pool_store` keys before rebuilding**, so a
re-run refreshes against the newly-fetched tables instead of reading last week's
values back out.

Two steps are deliberately **non-fatal** (`|| echo`), so a third-party hiccup
can't abort the chain and leave the week half-refreshed:

- **`fetch_coaches`** — CFBD publishes a new season's coaching records late, so
  an empty response is expected in the preseason. It never wipes existing rows
  on an empty fetch.
- **`fetch_ea_ratings`** — an internal-only signal for lineup/starter selection,
  never displayed. It scrapes a third-party page, so it refuses to overwrite the
  table when a fetch comes back short or blocked (`EA_MIN_ROWS`), leaving last
  week's ratings in place rather than silently dropping the starter model back
  to production-only scoring.

### Season rollover

Every fetch script derives its season from `season_util.current_cfb_season()`
(date-driven, rolls over in February), and the two precompute steps key off the
seasons that actually have stats loaded. So the first run after a new season's
opening weekend picks the new year up on its own — no edit needed. The site's
*display* default is separate and only advances once the season has real stats,
which is why the preseason shows last season while the pipeline already ingests
the new one.

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
