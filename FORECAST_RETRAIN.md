# Savant Forecast — retrain plan & experiment log

The production model (`forecast_model.json`, v1, 16 features) is trained
**locally only** by `train_forecast.py`; sklearn never deploys. Serving is a dot
product in `predict_games.py`.

## When to retrain

After each season completes (January, once bowls/CFP are final and the weekly
cron has ingested them). Retraining rolls the season splits forward, e.g. after
2026: train 2017–2024, validate 2025, test 2026.

```bash
python3 train_forecast.py          # prints the full protocol report
git add forecast_model.json && git commit   # ship only if it clears the bars
```

Ship bars (unchanged): must beat the **prior-SP+** and **Elo-alone** baselines on
the test season, stay calibrated within ~2 points per probability bucket, and
remain below the Vegas closing line (anything above it means a bug, not a
breakthrough).

## Standing practice for any new feature

These are requirements, not suggestions. Two experiments have already been
rejected on them, and one near-miss was caused by skipping the third.

1. **Verify the feature's data before trusting its result.** Print the non-zero
   rate, mean, std, and min/max of every candidate column, and check the row
   coverage of any join it depends on, *before* reading its evaluation. A
   feature that silently resolves to all zeros will produce a clean-looking
   "reject" that means nothing.
   > This is not hypothetical: the 2026-07 sweep initially evaluated rolling
   > turnover margin as a column of zeros, because ESPN box scores store mascot
   > display names ("Maine Black Bears") that never join to the games table
   > ("Maine") — 0 of 17,262 rows matched. It surfaced only as a 0/0 McNemar
   > disagreement count. Once fixed, that feature turned out to be the single
   > best candidate in the entire sweep. The bug would have buried the most
   > promising finding under a false rejection.
2. **Judge on multi-season walk-forward + McNemar, never a single test season.**
   The as-of-week Savant feature looked like +0.005 on 2025 alone and was
   −0.0006 across seven walk-forward seasons (p=0.857).
3. **Bar to ship:** > +0.002 walk-forward weighted accuracy **and** p < 0.05,
   without degrading Brier or calibration. Also check whether the new
   coefficient merely cannibalizes an existing one (as-of-week Savant pulled
   `elo_diff` from +0.67 to +0.41 — it was re-expressing Elo, not adding to it).
4. **Test candidates one at a time first, then in combination.** Combined
   effects differ, and on ~8.6k games more features is a real overfitting risk:
   all 13 candidates together scored *worse* than the production 16.

## Open experiment — as-of-week Savant Rating (`savant_asof_diff`)

**Status: evaluated 2026-07-21, DEFERRED. Re-test after the 2026 season.**

Idea: feed the model each team's Savant Net rating *as of the previous week*
(from `savant_weekly`), a drives-quality signal plausibly additive over raw Elo
— especially in the measured weeks-4–8 weak zone.

What changed to make it testable: the July 2026 summary backfill gave every
season 2016–2025 complete ESPN drive data, so `savant_weekly` was backfilled for
**all 10 seasons** (23,581 snapshots), not just 2025. Coverage was never the
blocker in the end.

Leakage handling (verified, keep if re-implemented): a game in week *W* may only
read snapshots from weeks **< W**; postseason games may read regular-season
snapshots (≤18) but **never** the week-20 sentinel, which contains postseason
results. An empirical check over 14,517 team-game lookups found **0 violations**.

**Result — it did not clear the bar:**

| Evaluation | v1 (16 feat) | v2 (+savant_asof) | Δ |
|---|---|---|---|
| val 2024 accuracy | 0.6892 | 0.6917 | +0.0025 |
| test 2025 accuracy | 0.7178 | 0.7228 | +0.0050 |
| test 2025 Brier | 0.1839 | 0.1826 | −0.0013 |
| **walk-forward 2019–2025, weighted acc** | **0.7110** | **0.7104** | **−0.0006** |

The single-season 2025 gain was noise. Across seven walk-forward seasons v2 won
3 and lost 4, and McNemar over the pooled disagreements (63 v1-only-right vs 60
v2-only-right) gave **p = 0.857** — no significant difference. Brier improved
consistently but tinily (−0.0004 to −0.0015), suggesting a whisper of real
signal in probability quality that accuracy can't detect at this sample size.
The feature also partly cannibalizes `elo_diff` (its coefficient fell
+0.67 → +0.41), i.e. it is largely re-expressing information Elo already has.

Given the GBT precedent (+0.9 val accuracy, rejected for complexity), a feature
worth −0.0006 across seasons does not justify a model change.

**Re-test in January** with 2026 included (one more season of snapshots, and the
first season whose snapshots were generated live rather than backfilled). The
experiment is reproducible: add `savant_asof_diff` to `FEATURE_NAMES`, load
`savant_weekly` in `_load_reference`, and gate the lookup on
`week < game_week` / `≤18` for postseason. Judge it on **walk-forward weighted
accuracy and McNemar**, not a single test season.

## Broad feature exploration — 2026-07-21 (13 candidates, all rejected)

A full sweep of unused data sources. Tooling kept for the January retrain:
`fetch_forecast_extras.py` (venues / weather / talent / coaches / opening
lines), `build_game_boxstats.py` (turnovers, penalties, possession from the
stored ESPN summaries), `forecast_candidates.py` (feature builders + the
walk-forward/McNemar harness). Re-run those three, then
`python3 forecast_candidates.py`, to repeat the sweep with a new season added.

**Data inventory.** Available and point-in-time clean: venue coordinates
(travel distance), per-game weather (temp/wind/precipitation, ~all games
2016–2025), 247-composite team talent, head coach per team-season (first-year
flag), SP+ special-teams rating (already in the DB but never used by the model),
rivalry flags, bye weeks, rolling recent-form margin, rolling turnover margin,
and as-of-week strength of schedule. Available but limited: opening betting
lines (`spread_open`) exist only for **2021–2025** — zero coverage 2016–2020, so
early walk-forward folds see a constant. Not available anywhere: injury
reports, snap counts, depth charts, coordinator (as opposed to head-coach)
changes.

**Baseline for all comparisons:** production 16-feature logistic, walk-forward
2019–2025 weighted accuracy **0.7110**, Brier 0.1866.

| candidate | Δacc | Δbrier | McNemar p | verdict |
|---|---|---|---|---|
| tov3_diff (rolling turnover margin) | +0.0025 | −0.0004 | 0.263 | reject |
| sp_st_diff (SP+ special teams) | +0.0011 | +0.0001 | 0.488 | reject |
| new_coach_diff (first-year HC) | +0.0006 | −0.0000 | 0.863 | reject |
| travel_diff (haversine miles) | +0.0004 | +0.0000 | 0.905 | reject |
| wind_speed | +0.0004 | +0.0000 | 0.804 | reject |
| talent_diff (247 composite) | +0.0000 | −0.0000 | 1.000 | reject |
| recent3_diff (last-3 margin) | +0.0000 | −0.0000 | 1.000 | reject |
| temp_dev | −0.0002 | +0.0003 | 1.000 | reject |
| rivalry | −0.0006 | +0.0001 | 0.839 | reject |
| precip | −0.0008 | −0.0000 | 0.289 | reject |
| line_move (open→close) | −0.0008 | −0.0004 | 0.794 | reject |
| bye_diff | −0.0015 | −0.0000 | 0.115 | reject |
| sos_asof_diff | −0.0019 | −0.0002 | 0.411 | reject |

**Combinations** (Step 3) fared no better, and piling everything on actively hurt
— the overfitting the sweep was warned about:

| combination | Δacc | McNemar p |
|---|---|---|
| tov3 + sp_st | +0.0019 | 0.419 |
| + new_coach | +0.0021 | 0.483 |
| + travel + recent3 | +0.0029 | 0.357 |
| **all 13 candidates** | **−0.0023** | 0.565 |

**GBT re-check** (does the expanded feature set change the model-class call?)
No — it got *worse*: GBT on the production 16 scored −0.0004 accuracy with a
notably worse Brier (0.1909 vs 0.1866), and on all 29 features −0.0013
(Brier 0.1912). Logistic remains the right call on both accuracy and
calibration, not just explainability.

**Verdict: nothing shipped.** No candidate cleared "meaningfully AND
significantly beats" (>+0.002 walk-forward accuracy with p<0.05). The best
single candidate, rolling turnover margin, is the one worth re-testing in
January — it is the only feature that moved accuracy above the threshold
(+0.0025) and improved Brier, and it plausibly carries real information Elo
lacks; it simply could not clear significance at this sample size.

*Process note:* the first run of this sweep silently tested `tov3_diff` as a
column of zeros (the ESPN mascot-name join bug). See **Standing practice #1**
above — this is why distribution/coverage checks are now mandatory before any
evaluation result is trusted.

## Backfilled 2025 forecasts (2026-07-21)

`game_predictions` holds two kinds of rows, both read identically by the
display/upset/tracker code:
- **2026+ (live):** written by `predict_games.py` before kickoff, frozen on
  scoring. The real going-forward record.
- **2025 (backfilled backtest):** written once by `backfill_2025_forecasts.py`.
  2025 was the held-out TEST season, so these are genuine out-of-sample
  point-in-time forecasts (each game's features use only earlier games) — the
  same predictions that produced the validated **71.8%** test accuracy (808
  games, 228 upsets). They exist so the completed-game forecast display and
  upset badges aren't blank on 2025 games.

Frozen-safety: `predict_games.py` only touches `season = current_cfb_season()`
and only `scored=0` rows, so it never overwrites the 2025 backfill. If the
accuracy tracker is ever resurfaced as a page, label 2025 as backtested vs 2026
as live. Older seasons (2017–2024) were deliberately NOT backfilled — doing so
honestly requires walk-forward models (2017–2023 are training seasons;
forecasting them with the production model would be in-sample).

## Known model behavior (documented, not a defect)

- Weeks 4–8 is the weakest stretch (~69% in 2025) — priors are fading while
  in-season data is still thin. Stated on the public `/forecast` methodology.
- Preseason priors dominate only in week 1 (~62% of logit contribution); from
  **week 2 onward in-season signal leads (~58–62%)**, and Elo is the single
  largest coefficient all season.
