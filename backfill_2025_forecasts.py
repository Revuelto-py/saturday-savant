"""One-time backfill of point-in-time Savant Forecasts for completed 2025 games.

2025 was the model's held-out TEST season — never used in training — so these
are genuine out-of-sample forecasts, the exact predictions that produced the
validated 71.8% test accuracy. Each game's feature vector comes from
build_dataset()'s leakage-safe pipeline (features for a game use only games
that finished before its kickoff), so this is a faithful backtest, not
hindsight: nothing here could see the game it is forecasting.

The forecasts are written to game_predictions as SCORED rows (scored=1) with
the real result, so the completed-game forecast display, upset badges, and the
accuracy tracker read them exactly as they will read live 2026 rows. They are
frozen on write — predict_games.py only ever touches the active season and only
rows with scored=0, so this backfill is never overwritten.

Inference is a dot product against forecast_model.json — the production
artifact, unchanged. This does not touch the model.

Usage:  python3 backfill_2025_forecasts.py           # dry run: prints accuracy
        python3 backfill_2025_forecasts.py --write    # persist to game_predictions
"""
import json
import math
import os
import sys

os.environ.setdefault('POOL_BACKFILL', '1')
import main
from forecast_features import build_dataset

SEASON = 2025
MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'forecast_model.json')


def main_():
    write = '--write' in sys.argv
    with open(MODEL_PATH) as f:
        model = json.load(f)
    mu, sd = model['scaler_mean'], model['scaler_std']
    coef, b0 = model['coef'], model['intercept']
    mcoef, mb0 = model['margin_coef'], model['margin_intercept']

    rows = [r for r in build_dataset()[0] if r['season'] == SEASON]
    preds = []
    for r in rows:
        z = [(f - m) / s for f, m, s in zip(r['features'], mu, sd)]
        prob = 1.0 / (1.0 + math.exp(-(b0 + sum(c * v for c, v in zip(coef, z)))))
        margin = mb0 + sum(c * v for c, v in zip(mcoef, z))
        home_won = 1 if r['home_points'] > r['away_points'] else 0
        correct = 1 if (prob >= 0.5) == (home_won == 1) else 0
        preds.append((r['game_id'], r['week'], r['home'], r['away'],
                      round(prob, 4), round(margin, 1), home_won, correct))

    n = len(preds)
    acc = sum(p[7] for p in preds) / n if n else 0.0
    upsets = sum(1 for p in preds if p[7] == 0)
    print(f"{SEASON}: {n} games forecast  |  accuracy {acc:.4f}  |  {upsets} upsets "
          f"(favorite lost)", flush=True)

    if not write:
        print("(dry run — pass --write to persist to game_predictions)")
        return

    conn = main.get_db()
    try:
        cur = conn.cursor()
        for gid, week, home, away, prob, margin, home_won, correct in preds:
            cur.execute('''
                INSERT INTO game_predictions
                    (game_id, season, week, home_team, away_team, home_prob,
                     predicted_margin, model_version, predicted_at, scored,
                     home_won, correct)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), 1, %s, %s)
                ON CONFLICT (game_id) DO UPDATE SET
                    home_prob = EXCLUDED.home_prob,
                    predicted_margin = EXCLUDED.predicted_margin,
                    scored = 1, home_won = EXCLUDED.home_won,
                    correct = EXCLUDED.correct, week = EXCLUDED.week
            ''', (gid, SEASON, week, home, away, prob, margin,
                  model['version'], home_won, correct))
        conn.commit()
        print(f"wrote {n} scored forecasts to game_predictions", flush=True)
    finally:
        main.release_db(conn)


if __name__ == '__main__':
    main_()

# Data changed — refresh the live cache so 2025 game pages/hub show the fills.
if '--write' in sys.argv:
    try:
        from cache_notify import notify_cache_clear
        notify_cache_clear()
    except Exception:
        pass
