"""One-time backfill of the per-feature breakdown for already-scored forecasts.

pipeline/predict_games.py writes `contrib` alongside every new prediction, but scored
rows are frozen — it only ever updates `WHERE scored = 0` — so the 2025 rows
laid down by backfill/backfill_2025_forecasts.py have a probability and no breakdown.
This fills that column in, and nothing else.

Safety: a scored prediction is a public record (it drives the accuracy tracker
and the upset badges), so this script never touches home_prob, predicted_margin,
scored, home_won or correct. It only writes `contrib`.

Correctness: the breakdown is only meaningful if it decomposes the SAME number
that is already stored. So each game's feature vector is replayed through
forecast_features.build_dataset() — the leakage-safe pipeline that produced the
original forecast — and the resulting probability is checked against the stored
one. A row whose replay disagrees is skipped and reported rather than written,
because a breakdown that doesn't add up to the displayed probability would be
worse than no breakdown at all.

Usage:  python3 backfill/backfill_forecast_contrib.py            # dry run: reports match rate
        python3 backfill/backfill_forecast_contrib.py --write    # persist contrib
"""

# This script lives one directory below the repo root; ROOT points back at it so
# .env, the model artifacts and the shared modules resolve the same as before.
import os as _os, sys as _sys
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if ROOT not in _sys.path:
    _sys.path.insert(0, ROOT)
import json
import math
import os
import sys

os.environ.setdefault('POOL_BACKFILL', '1')
import main
from forecast_features import build_dataset
from forecast_explain import explain

MODEL_PATH = os.path.join(ROOT, 'forecast_model.json')
# Stored probabilities are rounded to 4dp on write, so anything at or under
# half a unit in the last place is rounding, not drift.
TOL = 1e-4


def main_():
    write = '--write' in sys.argv
    with open(MODEL_PATH) as f:
        model = json.load(f)

    def prob(feats):
        z = [(x - m) / s for x, m, s in zip(feats, model['scaler_mean'], model['scaler_std'])]
        return 1.0 / (1.0 + math.exp(-(model['intercept'] +
                                       sum(c * v for c, v in zip(model['coef'], z)))))

    rows, _ = build_dataset()
    replay = {r['game_id']: r['features'] for r in rows}

    conn = main.get_db()
    try:
        cur = conn.cursor()
        cur.execute('ALTER TABLE game_predictions ADD COLUMN IF NOT EXISTS contrib JSONB')
        conn.commit()
        cur.execute('SELECT game_id, home_prob FROM game_predictions '
                    'WHERE scored = 1 AND home_prob IS NOT NULL')
        stored = cur.fetchall()

        fills, drift, absent = [], [], 0
        for gid, sp in stored:
            feats = replay.get(gid)
            if feats is None:
                absent += 1          # FBS-vs-FCS row: different model, no breakdown
                continue
            if abs(prob(feats) - sp) > TOL:
                drift.append((gid, sp, prob(feats)))
                continue
            fills.append((json.dumps(explain(model, feats)), gid))

        print(f"scored rows: {len(stored)}  |  replayed & matched: {len(fills)}  |  "
              f"no FBS-vs-FBS replay (FCS model): {absent}  |  drifted: {len(drift)}",
              flush=True)
        for gid, sp, got in drift[:5]:
            print(f"  DRIFT game {gid}: stored {sp:.4f} vs replay {got:.4f}", flush=True)

        if not write:
            print("(dry run — pass --write to persist)")
            return

        cur.executemany('UPDATE game_predictions SET contrib = %s WHERE game_id = %s', fills)
        conn.commit()
        print(f"wrote contrib for {len(fills)} scored forecasts", flush=True)
    finally:
        main.release_db(conn)


if __name__ == '__main__':
    main_()

    if '--write' in sys.argv:
        try:
            from cache_notify import notify_cache_clear
            notify_cache_clear()
        except Exception:
            pass
