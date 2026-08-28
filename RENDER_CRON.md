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
7. `fetch_2026_roster.py` — team rosters, current season *(non-fatal)*
8. `fetch_ea_ratings.py` — EA ratings, starter-model input *(non-fatal)*
9. `refresh_headshots.py --active-only` — player headshots *(non-fatal)*
10. `fetch_game_summaries.py` — game summaries / drives
11. `compute_savant_ratings.py` — Savant ratings → `savant_ratings`
12. `backfill_pools.py` — percentile peer pools → `pool_store`
13. `precompute.py` — team-page + returning-production precompute → `pool_store`
14. `fetch_betting_lines.py` — Vegas lines, active season
15. `predict_games.py` — Savant Forecast: score last week, predict upcoming

Steps 12–13 **delete their stale `pool_store` keys before rebuilding**, so a
re-run refreshes against the newly-fetched tables instead of reading last week's
values back out.

**Steps 7–9 are ordered, not interchangeable.** EA ratings are matched to
players at ingest and headshots are fetched per active player, so both read the
roster written by step 7. Run them the other way round and a newcomer waits a
week for his rating and his photo.

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
- **`fetch_2026_roster`** — rosters churn all season (injuries, dismissals,
  mid-year departures) and this is what removes a departed player: Trebor Pena
  sat on Penn State's roster after signing with Jacksonville because nothing
  refreshed it. It writes BOTH `players.active_2026` and the `rosters` table —
  the team page's Roster tab reads the latter, and the two silently diverging is
  what caused that bug. It fetches every team before writing anything and aborts
  rather than storing a partial roster, so a CFBD outage leaves last week's
  intact. Despite the filename it follows `current_cfb_season()`.
- **`refresh_headshots`** — ESPN publishes new photos through the season, so a
  file that only ever gets backfilled goes stale (47% of the roster's images
  changed at the 2026 preseason drop). `--active-only` sweeps the ~15k current
  roster rather than all 44k, because historical images move ~1% a year. It
  compares against **what is already in R2**, not a local mirror — the cron
  container has no `static/headshots/`, and a local baseline would make every
  player look new and re-push the whole 43k bucket weekly. A failure leaves last
  week's photos: a stale image, not a broken page.

  This is the step that needs the **R2 variables** below; without them it is the
  one thing in the chain that cannot run.

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
   - `ADMIN_KEY` — lets the run clear the live page cache when it finishes.
     Without it the data lands in Postgres but the site serves cached pages
     until the TTL expires.
   - `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
     `R2_BUCKET_NAME`, `R2_PUBLIC_URL` — the headshot bucket (step 8). Copy the
     values from your local `.env`; note they are stored there as `KEY = value`
     with spaces, which `python-dotenv` reads fine but a plain shell `grep` does
     not.

## Why the stores survive deploys

The precomputed data lives in Postgres (`pool_store`, `savant_ratings`, …), not
in the web service's in-process `SimpleCache`. A deploy or `/admin/clear-cache`
wipes only the in-memory page cache; the precomputed stores persist and refresh
**only on this cron schedule** (or self-heal on a cache miss).

## Second Cron Job — game-day scores (`fetch_scores.py`)

The weekly chain runs Mondays, so between Saturday kickoff and Monday 10:00 UTC
the `games` table still says "Scheduled" with no score — about **30 hours** of
stale results on the ticker, the /games grid and every game page.

`fetch_scores.py` closes that window. It is deliberately NOT the weekly fetch:
one CFBD call, then `UPDATE` only the rows whose score or completion changed.
It never DELETEs or INSERTs, so unlike `fetch_data.py` it cannot wipe a season
— the worst case is a no-op. Game pages need nothing further, because
`/game/<id>` already falls back to a live ESPN summary fetch (and stores it)
when a completed game has no stored summary.

1. Render Dashboard → **New +** → **Cron Job** (a second one; the weekly job stays).
2. Same repo/branch/runtime/build command as the weekly job.
3. **Command:** `python3 fetch_scores.py`
4. **Schedule (UTC):** `*/10 * * * *` — every 10 minutes, all week.

   Do **not** narrow this to "game days". The 2026 slate kicks off on every day
   of the week (Sat 720, Sun 70, Fri 45, Wed 24, Thu 22, Tue 6, Mon 1 — Tuesday
   and Wednesday are November MACtion, Monday is Labor Day), and kickoffs land in
   every UTC hour except 06:00-14:00. A day-or-hour window has to be re-reasoned
   every time the schedule shifts, and gets DST wrong twice a year; running
   always has no gaps to get wrong.

   The 10-minute floor is the part that matters: a run that changes nothing skips
   the cache clear entirely (so an off-hours no-op costs the site nothing), while
   one that does change something clears the whole page cache — a tighter loop
   would keep every page permanently cold.
5. **Environment variables:** `DATABASE_URL`, `CFBD_API_KEY`, `ADMIN_KEY`
   (`ADMIN_KEY` is what lets the run clear the live page cache — without it the
   scores land in Postgres but the site keeps serving cached pages until the TTL
   expires).

Manual run: `python3 fetch_scores.py` (all weeks) or `--week 1` (smaller payload).

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
