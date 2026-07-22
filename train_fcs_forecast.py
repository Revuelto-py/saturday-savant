"""FBS-vs-FCS forecast — training + honest backtest (LOCAL ONLY, never deployed).

The main forecast can't rate an FCS opponent, so these games get a tiny model
keyed on the FBS team's strength. Because FBS wins ~93% regardless, straight
accuracy is a weak yardstick (always-pick-FBS already scores ~93%); the point
of the model is CALIBRATION — say ~99% for an elite FBS team and ~80% for a
weak one, so the shaky matchups (where the FCS upsets actually happen) read
honestly. Judged on Brier / log-loss / calibration, not accuracy.

Same protocol as the main model: split by season (train 2017–2023, val 2024,
test 2025; 2016 is Elo burn-in), logistic for win prob + Ridge for margin.

Artifact: fcs_forecast_model.json. Serving is a dot product in predict_games.py.

Usage:  python3 train_fcs_forecast.py
"""
import json
import math
from datetime import datetime, timezone

import numpy as np
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import brier_score_loss, log_loss

from forecast_features import build_dataset, FCS_FEATURE_NAMES

TRAIN = list(range(2017, 2024))
VAL, TEST = 2024, 2025


def xy(rows):
    return (np.array([r['features'] for r in rows], dtype=float),
            np.array([r['target'] for r in rows], dtype=int))


def main():
    _, _, fcs = build_dataset(collect_fcs=True)
    fcs = [r for r in fcs if not r['burn_in']]
    tr = [r for r in fcs if r['season'] in TRAIN]
    va = [r for r in fcs if r['season'] == VAL]
    te = [r for r in fcs if r['season'] == TEST]
    print(f"FBS-vs-FCS rows: train {len(tr)} | val {len(va)} | test {len(te)}")

    Xtr, ytr = xy(tr)
    mu, sd = Xtr.mean(0), Xtr.std(0); sd[sd == 0] = 1.0
    Xv, yv = xy(va)
    best = None
    for C in (0.03, 0.1, 0.3, 1.0, 3.0):
        m = LogisticRegression(C=C, max_iter=3000).fit((Xtr - mu) / sd, ytr)
        ll = log_loss(yv, m.predict_proba((Xv - mu) / sd)[:, 1], labels=[0, 1])
        if best is None or ll < best[0]:
            best = (ll, C, m)
    clf, C = best[2], best[1]
    print(f"logistic chosen C={C}\n")

    def ev(name, rows):
        X, y = xy(rows)
        p = clf.predict_proba((X - mu) / sd)[:, 1]
        acc = ((p >= .5).astype(int) == y).mean()
        base = max(y.mean(), 1 - y.mean())           # always-pick-majority
        print(f"  {name:10} n={len(rows):>4}  model acc={acc:.4f}  base(pick-FBS)={base:.4f}"
              f"  brier={brier_score_loss(y, p):.4f}  logloss={log_loss(y, p, labels=[0,1]):.4f}")
        return p, y

    print("== FCS MODEL ==")
    ev('val 2024', va)
    p_te, y_te = ev('test 2025', te)

    print("\n== TEST 2025 CALIBRATION (FBS-win-probability buckets) ==")
    for lo, hi in ((0.5, 0.8), (0.8, 0.9), (0.9, 0.95), (0.95, 0.99), (0.99, 1.001)):
        m = (p_te >= lo) & (p_te < hi)
        if m.sum():
            print(f"  {lo:.0%}-{min(hi,1.0):.0%}: predicted {p_te[m].mean():.3f}  "
                  f"actual {y_te[m].mean():.3f}  n={m.sum()}")

    print("\n== spread of forecasts (test 2025) — strong vs weak FBS ==")
    order = np.argsort(-p_te)
    print(f"  most confident:  {te[order[0]]['fbs']} {p_te[order[0]]:.1%} vs {te[order[0]]['fcs']}")
    print(f"  least confident: {te[order[-1]]['fbs']} {p_te[order[-1]]:.1%} vs {te[order[-1]]['fcs']}")
    print(f"  range: {p_te.min():.1%} – {p_te.max():.1%}")

    # ── margin head ─────────────────────────────────────────────────────────
    mtr = np.array([r['margin'] for r in tr], dtype=float)
    ridge = Ridge(alpha=10.0).fit((Xtr - mu) / sd, mtr)
    Xte, _ = xy(te)
    pred = ridge.predict((Xte - mu) / sd)
    actual = np.array([r['margin'] for r in te], dtype=float)
    print(f"\n== MARGIN HEAD == test MAE={np.abs(pred-actual).mean():.2f} "
          f"(mean actual FBS margin {actual.mean():.1f})")

    print("\n== COEFFICIENTS (standardized) ==")
    for i in np.argsort(-np.abs(clf.coef_[0])):
        print(f"  {FCS_FEATURE_NAMES[i]:18} {clf.coef_[0][i]:+.4f}")
    print(f"  {'<intercept>':18} {clf.intercept_[0]:+.4f}")

    artifact = {
        'version': 1, 'kind': 'fbs_vs_fcs',
        'trained_at': datetime.now(timezone.utc).isoformat(),
        'feature_names': FCS_FEATURE_NAMES,
        'scaler_mean': mu.tolist(), 'scaler_std': sd.tolist(),
        'coef': clf.coef_[0].tolist(), 'intercept': float(clf.intercept_[0]),
        'margin_coef': ridge.coef_.tolist(), 'margin_intercept': float(ridge.intercept_),
        'C': C,
    }
    with open('fcs_forecast_model.json', 'w') as f:
        json.dump(artifact, f, indent=1)
    print("\nwrote fcs_forecast_model.json")


if __name__ == '__main__':
    main()
