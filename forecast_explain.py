"""Savant Forecast — per-game feature attribution ("why the model favors X").

The model is a logistic regression served as a dot product:

    z_i    = (f_i - scaler_mean_i) / scaler_std_i
    logit  = intercept + Σ coef_i · z_i
    P(home) = sigmoid(logit)

so each feature's push on THIS game is exactly `coef_i · z_i` — the same
numbers that produced the stored probability, not a parallel re-derivation.
This module never re-implements the feature vector: it takes the vector that
`forecast_features._feature_vector` already built (the one training and serving
share) and decomposes the dot product that `predict_games._predict` computed.
Positive contribution = pushes toward the HOME team, negative = toward AWAY,
matching how game_predictions stores everything home-perspective.

WHICH FEATURES ARE PUBLIC
The model fits 16 features, but only six carry independently meaningful,
sign-correct weight; the rest are collinearity artifacts or bookkeeping and
were deliberately kept out of the public explanation (a wpct_diff that lands
NEGATIVE is re-expressing Elo, not saying wins hurt you). The six below are
exactly the ones the methodology write-up named, at the weights it stated:

    elo_diff       +0.67    recruit4_diff  +0.39    ppg_diff   +0.31
    ret_prod_diff  +0.28    papg_diff      -0.27    prior_sp_diff +0.23

Omitted: prior_savant_diff (-0.02, collinear with prior SP+), recruit_diff
(-0.01, collinear with the 4-year average), transfer_diff (-0.001, a measured
null), wpct_diff (-0.12, sign-flipped Elo restatement), and the bookkeeping
inputs prior_missing / games_min / rest_diff / week / postseason.

HOME FIELD is not a feature — it lives in the intercept, which is the model's
log-odds for a game where every feature sits at its training mean (sigmoid of
+0.435 = 60.7%, the historical home win rate). The `neutral` feature adjusts
it, so the pair is reported as one "Home field" / "Neutral site" row.

Because six of sixteen features are shown, the displayed rows do not sum to
the full logit — the UI says so rather than implying a closed ledger.
"""
import json

# (feature name in FEATURE_NAMES, public label, value formatter key)
PUBLIC_FEATURES = [
    ('elo_diff',      'Team strength',        'elo'),
    ('recruit4_diff', 'Recruiting (4-yr)',    'recruit'),
    ('ppg_diff',      'Points per game',      'ppg'),
    ('ret_prod_diff', 'Returning production', 'pct'),
    ('papg_diff',     'Points allowed',       'papg'),
    ('prior_sp_diff', "Last season's SP+",    'sp'),
]

# Feature index of the neutral-site flag, folded into the home-field row.
_NEUTRAL = 'neutral'


def _fmt(kind, raw):
    """Plain-language magnitude for a feature's raw home−away differential,
    always stated from the favored side's perspective (direction is carried by
    the row's sign, so the text never repeats it)."""
    m = abs(raw)
    if kind == 'elo':
        return f'{m:.0f} rating'
    if kind == 'recruit':
        return f'{m:.0f} class pts'
    if kind == 'ppg':
        return f'{m:.1f} pts/gm'
    if kind == 'pct':
        return f'{m:.0f}% returning'
    if kind == 'papg':
        return f'{m:.1f} fewer'
    if kind == 'sp':
        return f'{m:.1f} SP+'
    return f'{m:.1f}'


def explain(model, feats):
    """Decompose one game's prediction into its public per-feature pushes.

    `model`  — the loaded forecast_model.json artifact.
    `feats`  — the feature vector from forecast_features._feature_vector, i.e.
               the exact vector the stored prediction was computed from.

    Returns a list of rows, largest push first: {key, logit, raw} — where
    `logit` is the signed contribution to the home-win log-odds (+ = home,
    − = away) and `raw` is the underlying home−away differential. This is what
    gets STORED: model numbers only, no wording. Labels and units are applied
    at render time by describe(), so copy can change without a data migration.
    Returns [] if the vector doesn't match the artifact (never guesses).
    """
    names = model.get('feature_names') or []
    if len(feats) != len(names) or len(names) != len(model['coef']):
        return []
    idx = {n: i for i, n in enumerate(names)}

    def contrib(name):
        i = idx[name]
        z = (feats[i] - model['scaler_mean'][i]) / model['scaler_std'][i]
        return model['coef'][i] * z

    # Before either side has kicked off, season-to-date scoring is 0−0: the
    # feature is present but carries no information, so a "0.0 pts/gm" row
    # would be noise dressed as evidence. Drop those two rows in week 1 (the
    # priors are doing the work there, and the display should show that).
    no_games = 'games_min' in idx and feats[idx['games_min']] <= 0

    rows = []
    for name, label, kind in PUBLIC_FEATURES:
        if name not in idx:
            continue
        if no_games and name in ('ppg_diff', 'papg_diff'):
            continue
        rows.append({
            'key': name,
            'logit': round(contrib(name), 4),
            'raw': round(feats[idx[name]], 3),
        })

    # Home field: the intercept (baseline home edge at mean features) plus the
    # neutral-site adjustment. Reported as one row so the page never implies
    # the model has a standalone "home field" coefficient.
    neutral_on = _NEUTRAL in idx and feats[idx[_NEUTRAL]] >= 0.5
    hf = model['intercept'] + (contrib(_NEUTRAL) if _NEUTRAL in idx else 0.0)
    # Always labelled "Home field", pointed at the designated home team: a
    # neutral site shrinks that edge but doesn't erase it in the model, and
    # labelling the row "Neutral site" would read as though the neutral venue
    # were itself an advantage for one side.
    rows.append({
        'key': 'home_field',
        'logit': round(hf, 4),
        'raw': 1.0 if neutral_on else 0.0,   # 1 = neutral site
    })

    rows.sort(key=lambda r: abs(r['logit']), reverse=True)
    return rows


def describe(rows):
    """Turn stored explain() rows into display rows: {key, label, logit, value}.

    Render-time only — keeps wording out of the database, so relabelling a
    factor never means rewriting 1,500 stored predictions. Unknown keys (from a
    future model revision) are dropped rather than shown raw."""
    if isinstance(rows, str):      # a driver that hands back raw JSON text
        try:
            rows = json.loads(rows)
        except ValueError:
            return []
    if not isinstance(rows, list):
        return []
    kinds = {name: (label, kind) for name, label, kind in PUBLIC_FEATURES}
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        key = r.get('key')
        if key == 'home_field':
            out.append({'key': key, 'label': 'Home field', 'logit': r.get('logit', 0.0),
                        'value': 'neutral site' if r.get('raw') else 'hosting'})
        elif key in kinds:
            label, kind = kinds[key]
            out.append({'key': key, 'label': label, 'logit': r.get('logit', 0.0),
                        'value': _fmt(kind, r.get('raw', 0.0))})
    return out
