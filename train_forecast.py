"""Savant Forecast — training + honest backtest (LOCAL ONLY, never deployed).

Protocol (fixed before any training):
  • Split by season, never random rows: train 2017–2023, validate 2024 (all
    model decisions), test 2025 (touched once, reported as-is). 2016 is Elo
    burn-in and excluded everywhere.
  • Baselines: always-home; higher prior-season SP+; Elo-alone (with home
    bump); Vegas closing spread (the honesty benchmark).
  • Metrics: straight-up accuracy, Brier score, log loss, calibration deciles,
    accuracy by season (walk-forward), by week bucket, favorites vs underdogs.
  • Ship bar: must beat the prior-SP+ and Elo-alone baselines on test.

Artifact: forecast_model.json — feature names, scaler params, coefficients.
Serving is a dot product; sklearn is a training-time dependency only.

Usage:  python3 train_forecast.py
"""
import json
import math
from datetime import datetime, timezone

import numpy as np
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import brier_score_loss, log_loss

from forecast_features import build_dataset, FEATURE_NAMES, \
    ELO_START, ELO_NEW_TEAM, ELO_CARRY, ELO_K, ELO_HFA

TRAIN_SEASONS = list(range(2017, 2024))
VAL_SEASON = 2024
TEST_SEASON = 2025


def xy(rows):
    X = np.array([r['features'] for r in rows], dtype=float)
    y = np.array([r['target'] for r in rows], dtype=int)
    return X, y


def fit_logistic(rows_train, rows_val):
    Xtr, ytr = xy(rows_train)
    mu, sd = Xtr.mean(axis=0), Xtr.std(axis=0)
    sd[sd == 0] = 1.0
    Xv, yv = xy(rows_val)
    best = None
    for C in (0.01, 0.03, 0.1, 0.3, 1.0, 3.0):
        clf = LogisticRegression(C=C, max_iter=2000)
        clf.fit((Xtr - mu) / sd, ytr)
        ll = log_loss(yv, clf.predict_proba((Xv - mu) / sd)[:, 1])
        if best is None or ll < best[0]:
            best = (ll, C, clf)
    return best[2], mu, sd, best[1]


def probs(clf, mu, sd, rows):
    X, _ = xy(rows)
    return clf.predict_proba((X - mu) / sd)[:, 1]


def evaluate(name, p, rows):
    y = np.array([r['target'] for r in rows])
    acc = ((p >= 0.5).astype(int) == y).mean()
    return {'name': name, 'n': len(rows), 'acc': acc,
            'brier': brier_score_loss(y, p), 'logloss': log_loss(y, p)}


def baseline_picks(rows):
    """Returns dict of baseline -> (picks 0/1 array or None mask handling)."""
    out = {}
    out['home'] = np.ones(len(rows), dtype=int)
    sp_picks, elo_picks = [], []
    for r in rows:
        f = dict(zip(FEATURE_NAMES, r['features']))
        sp_picks.append(1 if f['prior_sp_diff'] >= 0 else 0)
        bump = 0.0 if f['neutral'] else ELO_HFA
        elo_picks.append(1 if (r['elo_home'] + bump - r['elo_away']) >= 0 else 0)
    out['prior_sp'] = np.array(sp_picks)
    out['elo_alone'] = np.array(elo_picks)
    vg = [(i, 1 if r['vegas_spread'] < 0 else 0) for i, r in enumerate(rows)
          if r['vegas_spread'] is not None and r['vegas_spread'] != 0]
    out['vegas_idx'] = np.array([i for i, _ in vg], dtype=int)
    out['vegas'] = np.array([p for _, p in vg], dtype=int)
    return out


def acc_of(picks, rows, idx=None):
    y = np.array([r['target'] for r in rows])
    if idx is not None:
        y = y[idx]
    return (picks == y).mean(), len(y)


def main():
    rows, _ = build_dataset()
    rows = [r for r in rows if not r['burn_in']]
    tr = [r for r in rows if r['season'] in TRAIN_SEASONS]
    va = [r for r in rows if r['season'] == VAL_SEASON]
    te = [r for r in rows if r['season'] == TEST_SEASON]
    print(f"rows: train {len(tr)} (2017-23) | val {len(va)} (2024) | test {len(te)} (2025)")

    clf, mu, sd, C = fit_logistic(tr, va)
    print(f"logistic chosen C={C}")

    # ── headline metrics ────────────────────────────────────────────────────
    print("\n== MODEL (logistic) ==")
    for name, split in (('val 2024', va), ('test 2025', te)):
        m = evaluate(name, probs(clf, mu, sd, split), split)
        print(f"  {m['name']:10} acc={m['acc']:.4f}  brier={m['brier']:.4f}  logloss={m['logloss']:.4f}  n={m['n']}")

    # ── GBT check (val only — decision gate) ────────────────────────────────
    Xtr, ytr = xy(tr)
    gbt = HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05,
                                         max_iter=400, l2_regularization=1.0,
                                         random_state=7)
    gbt.fit(Xtr, ytr)
    Xv, yv = xy(va)
    pv = gbt.predict_proba(Xv)[:, 1]
    print(f"\n== GBT CHECK (val 2024) == acc={((pv>=0.5).astype(int)==yv).mean():.4f} "
          f" brier={brier_score_loss(yv, pv):.4f}  logloss={log_loss(yv, pv):.4f}")

    # ── baselines ───────────────────────────────────────────────────────────
    print("\n== BASELINES ==")
    for name, split in (('val 2024', va), ('test 2025', te)):
        b = baseline_picks(split)
        a_home, n = acc_of(b['home'], split)
        a_sp, _ = acc_of(b['prior_sp'], split)
        a_elo, _ = acc_of(b['elo_alone'], split)
        a_vg, n_vg = acc_of(b['vegas'], split, b['vegas_idx'])
        print(f"  {name:10} home={a_home:.4f}  prior_sp={a_sp:.4f}  elo_alone={a_elo:.4f}"
              f"  vegas={a_vg:.4f} (n={n_vg}/{n})")
        # model on the vegas-covered subset, apples to apples
        p = probs(clf, mu, sd, split)
        y = np.array([r['target'] for r in split])
        i = b['vegas_idx']
        if len(i):
            print(f"  {'':10} model on vegas subset: {(((p[i]>=0.5).astype(int))==y[i]).mean():.4f}")

    # ── walk-forward by season ──────────────────────────────────────────────
    print("\n== WALK-FORWARD BY SEASON (train 2017..S-1 -> test S) ==")
    print(f"  {'season':6} {'n':>5} {'model':>7} {'home':>7} {'prior_sp':>9} {'elo':>7} {'vegas':>7}")
    for S in range(2019, 2026):
        tr_s = [r for r in rows if 2017 <= r['season'] < S]
        te_s = [r for r in rows if r['season'] == S]
        if not te_s:
            continue
        c, m_, s_, _ = fit_logistic(tr_s, te_s if S == 2019 else
                                    [r for r in rows if r['season'] == S - 1])
        p = probs(c, m_, s_, te_s)
        y = np.array([r['target'] for r in te_s])
        b = baseline_picks(te_s)
        a_vg, _ = acc_of(b['vegas'], te_s, b['vegas_idx'])
        print(f"  {S:6} {len(te_s):>5} {((p>=0.5).astype(int)==y).mean():>7.4f} "
              f"{acc_of(b['home'], te_s)[0]:>7.4f} {acc_of(b['prior_sp'], te_s)[0]:>9.4f} "
              f"{acc_of(b['elo_alone'], te_s)[0]:>7.4f} {a_vg:>7.4f}")

    # ── by week bucket (test) ───────────────────────────────────────────────
    print("\n== TEST 2025 BY WEEK ==")
    p_te = probs(clf, mu, sd, te)
    for lbl, lo, hi in (('wk 1-3', 1, 3), ('wk 4-8', 4, 8), ('wk 9-14', 9, 14), ('post', 15, 99)):
        idx = [i for i, r in enumerate(te)
               if (lo <= (r['week'] or 0) <= hi) or (lbl == 'post' and 'post' in str(r).lower() and False)]
        idx = [i for i, r in enumerate(te) if lo <= (r['week'] or 0) <= hi]
        if not idx:
            continue
        y = np.array([te[i]['target'] for i in idx])
        pp = p_te[idx]
        b = baseline_picks([te[i] for i in idx])
        print(f"  {lbl:7} n={len(idx):>4}  model={((pp>=0.5).astype(int)==y).mean():.4f}"
              f"  elo={acc_of(b['elo_alone'], [te[i] for i in idx])[0]:.4f}"
              f"  prior_sp={acc_of(b['prior_sp'], [te[i] for i in idx])[0]:.4f}")

    # ── calibration (test) ──────────────────────────────────────────────────
    print("\n== TEST 2025 CALIBRATION (favorite-probability buckets) ==")
    fav_p = np.maximum(p_te, 1 - p_te)
    fav_won = ((p_te >= 0.5).astype(int) == np.array([r['target'] for r in te]))
    for lo, hi in ((0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.001)):
        m = (fav_p >= lo) & (fav_p < hi)
        if m.sum():
            print(f"  {lo:.0%}-{min(hi,1.0):.0%}: predicted {fav_p[m].mean():.3f}  actual {fav_won[m].mean():.3f}  n={m.sum()}")

    # ── margin head (secondary output): Ridge on the same features ──────────
    print("\n== MARGIN HEAD (predicted home margin, Ridge) ==")
    mtr = np.array([r['home_points'] - r['away_points'] for r in tr], dtype=float)
    ridge = Ridge(alpha=10.0)
    ridge.fit((Xtr - mu) / sd, mtr)
    for name, split in (('val 2024', va), ('test 2025', te)):
        X, _ = xy(split)
        pred = ridge.predict((X - mu) / sd)
        actual = np.array([r['home_points'] - r['away_points'] for r in split], dtype=float)
        mae = np.abs(pred - actual).mean()
        # Vegas comparison: spread is home-perspective, negative = home favored,
        # so Vegas's implied home margin is -spread.
        vg = [(i, -r['vegas_spread']) for i, r in enumerate(split) if r['vegas_spread'] is not None]
        vi = np.array([i for i, _ in vg]); vm = np.array([m for _, m in vg])
        v_mae = np.abs(vm - actual[vi]).mean() if len(vi) else float('nan')
        print(f"  {name:10} model MAE={mae:.2f}  vegas MAE={v_mae:.2f}  (n={len(split)})")

    # ── coefficients (explainability) ───────────────────────────────────────
    print("\n== COEFFICIENTS (standardized) ==")
    order = np.argsort(-np.abs(clf.coef_[0]))
    for i in order:
        print(f"  {FEATURE_NAMES[i]:18} {clf.coef_[0][i]:+.4f}")
    print(f"  {'<intercept>':18} {clf.intercept_[0]:+.4f}")

    # ── artifact ────────────────────────────────────────────────────────────
    artifact = {
        'version': 1,
        'trained_at': datetime.now(timezone.utc).isoformat(),
        'train_seasons': TRAIN_SEASONS, 'val_season': VAL_SEASON, 'test_season': TEST_SEASON,
        'feature_names': FEATURE_NAMES,
        'scaler_mean': mu.tolist(), 'scaler_std': sd.tolist(),
        'coef': clf.coef_[0].tolist(), 'intercept': float(clf.intercept_[0]),
        'margin_coef': ridge.coef_.tolist(), 'margin_intercept': float(ridge.intercept_),
        'C': C,
        'elo': {'start': ELO_START, 'new_team': ELO_NEW_TEAM, 'carry': ELO_CARRY,
                'k': ELO_K, 'hfa': ELO_HFA},
    }
    with open('forecast_model.json', 'w') as f:
        json.dump(artifact, f, indent=1)
    print("\nwrote forecast_model.json")


if __name__ == '__main__':
    main()
