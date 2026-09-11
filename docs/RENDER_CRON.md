# Weekly data pipeline — Render Cron Job

The CFBD fetch scripts were run **manually** up to this point — there was no
scheduler in the repo (only the web `Procfile`). This sets up the one place that
runs the whole weekly chain automatically: fetch → derive → precompute, via
[`run_weekly.sh`](run_weekly.sh).

## What runs

`run_weekly.sh` runs, in order (`set -e` aborts on any failure):

1. `pipeline/fetch_data.py` — games (incl. scores/completion) + player box scores + PPA
2. `pipeline/apply_week_zero.py` — label Week 0 *(non-fatal)*
3. `pipeline/fetch_team_stats.py` — team stats
4. `pipeline/fetch_advanced.py` — advanced team stats
5. `pipeline/fetch_sp.py` — SP+ ratings
6. `pipeline/fetch_rankings.py` — AP rankings (every weekly poll, not just the final)
   — *also runs hourly on its own cron; see below*
7. `pipeline/fetch_coaches.py` — head coaches, current season *(non-fatal)*
8. `pipeline/fetch_2026_roster.py` — team rosters, current season *(non-fatal)*
9. `pipeline/fetch_ea_ratings.py` — EA ratings, starter-model input *(non-fatal)*
10. `pipeline/refresh_headshots.py --active-only` — player headshots *(non-fatal)*
11. `pipeline/fetch_game_summaries.py` — game summaries / drives
12. `pipeline/fetch_passing.py` — play-level passing: air yards / location / YAC *(non-fatal)*
13. `pipeline/compute_savant_ratings.py` — Savant ratings → `savant_ratings`
14. `pipeline/backfill_pools.py` — percentile peer pools → `pool_store`
15. `pipeline/precompute.py` — team-page + returning-production precompute → `pool_store`
16. `pipeline/fetch_betting_lines.py` — Vegas lines, active season
17. `pipeline/predict_games.py` — Savant Forecast: score last week, predict upcoming
18. `pipeline/apply_week_zero.py` — again, for the tables written since *(non-fatal)*

**Week 0 runs twice, and that is deliberate.** College football opens with a
handful of games the Saturday before the real opening weekend. **No upstream
source labels them:** CFBD returns them as week 1 and ignores a `week=0`
argument, so every season arrived with those games folded into week 1 — a team
that played both showed two "WK 1" rows on its schedule, and the duplication ran
through every player's game log. `pipeline/apply_week_zero.py` derives the split
from the calendar (`season_util.week_zero_dates`: sort the opening kickoff dates,
split at the first gap of 3+ days) and writes week 0 onto `games`, onto every
table that keeps its own copy of the week, and into the JSON in
`player_game_logs`.

- **Step 2** runs right after the only step that writes `games`, so everything
  derived below — Savant snapshots, precompute, forecasts — sees the corrected
  week.
- **Step 18** runs at the end because `betting_lines`, `passing_plays` and
  `game_predictions` are written *after* step 2, from CFBD, which calls a Week 0
  game week 1.

It is idempotent: it recomputes the same target set from weeks 0 **and** 1, and
skips the cache clear when nothing moved, so the ~50 weeks a year with no new
Week 0 cost nothing. `ap_rankings` is deliberately untouched — its `week` is a
*poll* week, and the preseason poll is week 1 by AP's own numbering.

Steps 14–15 **delete their stale `pool_store` keys before rebuilding**, so a
re-run refreshes against the newly-fetched tables instead of reading last week's
values back out.

**Steps 8–10 are ordered, not interchangeable.** EA ratings are matched to
players at ingest and headshots are fetched per active player, so both read the
roster written by step 7. Run them the other way round and a newcomer waits a
week for his rating and his photo.

Several steps are deliberately **non-fatal** (`|| echo`), so a third-party hiccup
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
- **`fetch_passing`** — feeds the air-yards / pass-location charts on the player,
  team and game pages. Nothing downstream in the chain reads `passing_plays`, so a
  failed fetch leaves last week's charts in place rather than aborting the ratings
  and precompute that follow it. Coverage is uneven by design of the source: 2025
  runs at 44% of attempts measured and is lopsided within the season, 2026 onward
  at ~98%, so the site gates every season-level chart on a per-player coverage
  floor rather than trusting that a row exists.

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
6. **Schedule (UTC):** `0 12 * * 0` — **Sundays 12:00 UTC (08:00 ET).**

   Moved off Mondays so the week's stats are derived the morning after the games
   rather than a day and a half later. The hour is set by the latest Saturday
   kickoff: in 2026 that is 23:59 ET (week 2), so the last game of a weekend ends
   around 03:30 ET Sunday and this leaves CFBD roughly four hours to post before
   the chain reads it. Do not move it earlier than ~10:00 UTC without checking
   that week's last kickoff.

   **The trade-off, stated:** games that kick off Sunday or Monday ET are not in
   that morning's run, so their derived stats (Savant ratings, percentile pools,
   team-page precompute) wait for the following Sunday. In 2026 that is **4 games
   out of 888** — 3 Sunday, 1 Monday (the Labor Day opener). Scores and
   completion are unaffected: the game-day cron below updates those every 10
   minutes regardless. If a future schedule puts real weight on Sunday games,
   revisit this rather than assuming it still holds.
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

## Second Cron Job — game-day scores (`pipeline/fetch_scores.py`)

The weekly chain runs once a week, so between Saturday kickoff and the next
scheduled run the `games` table would still say "Scheduled" with no score —
hours of stale results on the ticker, the /games grid and every game page.

`pipeline/fetch_scores.py` closes that window. It is deliberately NOT the weekly fetch:
one CFBD call, then `UPDATE` only the rows whose score or completion changed.
It never DELETEs or INSERTs, so unlike `pipeline/fetch_data.py` it cannot wipe a season
— the worst case is a no-op. Game pages need nothing further, because
`/game/<id>` already falls back to a live ESPN summary fetch (and stores it)
when a completed game has no stored summary.

1. Render Dashboard → **New +** → **Cron Job** (a second one; the weekly job stays).
2. Same repo/branch/runtime/build command as the weekly job.
3. **Command:** `python3 pipeline/fetch_scores.py`

   > **This path changed.** The script used to sit at the repo root, so an
   > existing cron job still says `python3 fetch_scores.py` and will start
   > failing on the next run. Update the Command field in the Render dashboard.
   > The weekly job is unaffected — it runs `bash run_weekly.sh`, which is still
   > at the root and now calls the moved scripts itself.
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

Manual run: `python3 pipeline/fetch_scores.py` (all weeks) or `--week 1` (smaller payload).

## Third Cron Job — AP rankings (`pipeline/fetch_rankings.py`)

Rankings are step 5 of the weekly chain, and for a while that was the only thing
that fetched them. It isn't enough, because **the AP poll's release day moves**:

- Sunday afternoon most weeks,
- **Tuesday** when week 1 runs through Labor Day (2026 week 1 ended with a
  Monday 19:30 ET game, so the first in-season poll landed on the Tuesday),
- January for the final poll.

A once-a-week job that happens to run before the poll drops leaves the site
showing the previous poll for another seven days. That is exactly what happened
in September 2026: the fetch was working and the stored rows matched CFBD
exactly — the poll simply hadn't been published yet when the chain last ran.

Running it hourly removes the guesswork, the same way the scores job runs always
rather than on a "game day" window.

1. Render Dashboard → **New +** → **Cron Job** (a third one).
2. Same repo/branch/runtime/build command as the others.
3. **Command:** `python3 pipeline/fetch_rankings.py`
4. **Schedule (UTC):** `0 * * * *` — hourly, all week.
5. **Environment variables:** `DATABASE_URL`, `CFBD_API_KEY`, `ADMIN_KEY`.

**CFBD is not the only source any more.** On 2026-09-08 the AP released its Week
2 poll at 07:00 UTC and CFBD still had only the preseason poll hours later, so
`fetch_rankings.py` now falls back to ESPN for any week CFBD has nothing for
(`espn_rankings.py`, the same idea as `espn_board.py` for scores). CFBD keeps
precedence on every week it does have.

One trap is baked into that module: ESPN's two APIs disagreed. The familiar
`site.api.espn.com/.../rankings` endpoint still returned "Preseason"; the poll
was only on the core API, `sports.core.api.espn.com/v2/.../weeks/2/rankings/1`,
which addresses each season/week/poll directly. Poll id 1 is the AP Top 25.
Teams come back as `$ref` URLs carrying an ESPN team id, resolved to our names
through the id already embedded in `teams.logo`. A week whose teams don't all
resolve is skipped rather than stored with a hole in it. `prev_rank` is always
derived by walking the merged CFBD+ESPN polls in order, never taken from ESPN's
own `previous` field, which disagrees with the stored preseason poll.

This means **rankings now self-correct even with no dashboard change**: the
Sunday chain runs `fetch_rankings.py` and will pick up a CFBD-lagged poll from
ESPN. The hourly job below just makes it minutes instead of up to a week.

**Why hourly is safe.** The script compares what CFBD returns against what is
stored and writes nothing when they match — one CFBD call and one `SELECT` for a
no-op run. It also **skips the cache clear unless a season actually changed**,
so the ~167 runs a week that find no new poll cost the site nothing; without that
guard an hourly job would keep every page permanently cold. It never deletes on
an empty CFBD response either, so an outage leaves the standing poll in place
rather than blanking the rankings page.

Manual run: `python3 pipeline/fetch_rankings.py` (active season) or
`python3 pipeline/fetch_rankings.py 2016 2025` to backfill a range.

## Fourth Cron Job — stats + percentiles (`pipeline/refresh_stats.py`)

Two gaps, closed by one job because they have to stay consistent with each
other.

**Totals.** `player_stats` holds each player's season totals, and step 1 of the
weekly chain was the only thing that rewrote it. A Thursday, Friday or Saturday
game left everyone who played carrying last week's numbers until Sunday — the
leaderboards, the player pages and the home leaders all read this table, so
scores were live while the totals under them were days old.

**Percentiles.** Fixing the totals alone would have bought a worse problem: a
player's raw numbers current while his standing against the field — and every
team percentile on the team page and the game preview — still moved on Sundays.
A current value beside a stale rank is harder to trust than two stale ones. So
the job refreshes the inputs **and** rebuilds the derived stores in one pass,
and clears the cache once, at the end.

What it touches, in dependency order:

| | | |
|---|---|---|
| `player_stats` | season totals | UPSERT, in-script |
| `player_ppa` | per-player EPA | UPSERT, in-script |
| `team_stats` | team advanced | `pipeline/fetch_team_stats.py` |
| `team_advanced` | team advanced (2nd set) | `pipeline/fetch_advanced.py --team-only` |
| `stats:*` / `ppa:*` | 15 player percentile pools | recomputed |
| `teampct:{season}` | team percentiles | recomputed |

**Deliberately still weekly:** Savant ratings (opponent-adjusted — the rating a
team carries into a week should not move under readers mid-slate), returning
production (a preseason figure), NFL talent (all-time), rosters, headshots, EA
ratings, game summaries.

1. Render Dashboard → **New +** → **Cron Job** (a fourth one).
2. Same repo/branch/runtime/build command as the others.
3. **Command:** `python3 pipeline/refresh_stats.py`
4. **Schedule (UTC):** `*/15 * * * *` — every 15 minutes, all week.

   Same reasoning as the scores job: the 2026 slate kicks off on every day of
   the week, so a "game day" window has to be re-reasoned whenever the schedule
   shifts and gets DST wrong twice a year. 15 rather than 10 because CFBD needs
   a few minutes after a final to post a box score.
5. **Environment variables:** `DATABASE_URL`, `CFBD_API_KEY`, `ADMIN_KEY`.

### Why running it this often is safe

- **UPSERT for the player tables, never DELETE-then-INSERT.** The `player_stats`
  key is `(player_id, season, team, category, stat_type)`, verified unique
  across all 1.2M stored rows. `team` is in the key because a player who
  changes teams mid-season legitimately carries one row per team — 112 such
  groups exist, and keying without it would collapse them silently. There is no
  window where the table is empty.
- **Every fetch refuses a short payload.** Under 80% of what is stored and the
  write is refused outright — here, and in both team scripts, which gained the
  same guard so they are safe to call between chains. A season's numbers only
  grow; one that shrinks is an upstream fault, not a correction.
- **The gate is cheap.** It compares the most recent completed kickoff against a
  marker in `pool_store`; with nothing new it exits in about a second having
  made no API call. That is what makes the ~160 idle runs a week free.
- **One cache clear, only on a real change.** The team scripts DELETE and
  re-INSERT unconditionally, so "the subprocess exited 0" says nothing about
  whether a number moved — the job fingerprints both tables before and after
  instead. (Round the sums: summing doubles in a different physical row order,
  which is exactly what a re-INSERT produces, moves the last digit and reported
  a change on every run.)

### The trap worth knowing

**A game ending and CFBD publishing its box score are minutes apart.** The first
version advanced the marker on every run that got past the gate — including the
run that found nothing because CFBD had not posted yet. The gate then closed
over its own miss and those stats would have waited for the *next* game.

So the marker only advances once something actually lands. To stop that
becoming an endless midweek poll there is a retry budget: `MAX_EMPTY_RETRIES=8`,
two hours at this cadence, comfortably longer than the gap between a final and
its box score. A retry that finds no new player rows also **stops before the
team stage**, since the same finished game feeds both — so a retry costs two
CFBD reads, not a full table rewrite.

The script creates its own unique index on first run (`IF NOT EXISTS`), so there
is no separate migration. `pipeline/fetch_data.py` was made conflict-safe at the
same time: it still DELETEs the season first, so a conflict should be
impossible, but a duplicate inside one CFBD payload would otherwise abort the
whole weekly chain now that the index exists.

Manual run: `python3 pipeline/refresh_stats.py` (active season), `--force` to
ignore the gate, or a year to target one season.

## Manual fallback

If the cron is ever paused, run the whole chain by hand from the project root:

```bash
bash run_weekly.sh
```

Or just the precompute step (after a manual fetch), for all or specific seasons:

```bash
python3 pipeline/precompute.py            # all loaded seasons
python3 pipeline/precompute.py 2024 2025  # specific seasons
```
