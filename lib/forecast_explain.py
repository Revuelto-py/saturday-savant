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
The model fits 21 features, but only six carry independently meaningful,
sign-correct weight; the rest are collinearity artifacts or bookkeeping and
were deliberately kept out of the public explanation (a wpct_diff that lands
NEGATIVE is re-expressing Elo, not saying wins hurt you). v3 weights, full
season / extra early-season weight from the `_early` twin (folded into the
same row, see contrib()):

    elo_diff       +0.94 / −0.49    recruit4_diff  +0.15 / +0.25
    ppg_diff       +0.40            ret_prod_diff  +0.21 / +0.05
    papg_diff      −0.34            prior_sp_diff  −0.04 / +0.24
                                    prior_savant   −0.02 / +0.30

(v3, 2026-10-07: refit 2017-25 on the rebuilt Savant history.) So Elo's
weight grows as the season goes and last season's ratings matter in September
and are ~0 by November — the fade the twins exist to express. Prior SP+ and
prior Savant are reported as ONE row ("Last season's ratings"), since they
measure the same thing and the fit splits it between them. Omitted: recruit_diff
(collinear with the 4-year average), transfer_diff (a measured null),
wpct_diff (sign-flipped Elo restatement), and the bookkeeping inputs
prior_missing / games_min / rest_diff / week / postseason.

HOME FIELD is not a feature — it lives in the intercept, which is the model's
log-odds for a game where every feature sits at its training mean (sigmoid of
+0.435 = 60.7%, the historical home win rate). The `neutral` feature adjusts
it, so the pair is reported as one "Home field" / "Neutral site" row.

Because six of the features are shown, the displayed rows do not sum to
the full logit — the UI says so rather than implying a closed ledger.
"""
import json
import math

# (feature name in FEATURE_NAMES, public label, value formatter key)
PUBLIC_FEATURES = [
    ('elo_diff',      'Team strength',        'elo'),
    ('recruit4_diff', 'Recruiting (4-yr)',    'recruit'),
    ('ppg_diff',      'Points per game',      'ppg'),
    ('ret_prod_diff', 'Returning production', 'pct'),
    ('papg_diff',     'Points allowed',       'papg'),
    ('prior_sp_diff', "Last season's ratings", 'sp'),
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
        c = model['coef'][i] * z
        # An `_early` twin is the same signal with an early-season weight, so
        # its push belongs to the parent's row, not a row of its own.
        if name + '_early' in idx:
            c += contrib(name + '_early')
        # Last season's SP+ and last season's Savant Rating are one idea, how
        # good the team was a year ago, and v3 splits its weight between them
        # almost evenly (+0.24 / +0.30 early). Showing SP+ alone hid half of
        # it; showing both would double-count one signal. One row carries both.
        if name == 'prior_sp_diff' and 'prior_savant_diff' in idx:
            c += contrib('prior_savant_diff')
        return c

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
    future model revision) are dropped rather than shown raw. A stored
    explain_sheet() dict (v2) is described by describe_sheet() instead."""
    if isinstance(rows, str):      # a driver that hands back raw JSON text
        try:
            rows = json.loads(rows)
        except ValueError:
            return []
    if isinstance(rows, dict) and rows.get('v') == 2:
        return describe_sheet(rows)
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


# ── v2: the side-by-side breakdown, measured from an even matchup ───────────
#
# explain() measures every push against the TRAINING MEAN of its feature, and
# the mean matchup is not an even one: home teams usually out-recruit their
# visitors, so Alabama's slightly better class (310 vs 307 at home to Georgia)
# drew a bar toward Georgia, and the six rows could never add up to the number.
# Here the baseline is two IDENTICAL teams at this venue in this week: every
# difference feature at 0, the context features (games played, week, neutral
# site, postseason, missing priors) at this game's values. That baseline is
# "home field". Each difference feature's push is then coef * f / std from 0,
# so a team's push always points toward the side with the better number, and
# baseline + pushes is the stored logit exactly (checked; None if not).

_CONTEXT = ('games_min', 'week', 'neutral', 'postseason', 'prior_missing')
SHEET_GROUPS = [            # (key, the features its push sums; `_early` twins fold in)
    ('ret_prod', ('ret_prod_diff',)),
    ('last', ('prior_sp_diff', 'prior_savant_diff')),
    ('papg', ('papg_diff',)),
    ('ppg', ('ppg_diff',)),
    ('elo', ('elo_diff',)),
    ('recruit', ('recruit4_diff',)),
]
_PRESEASON = ('ret_prod', 'last', 'recruit')
_IN_SEASON = ('elo', 'ppg', 'papg')


def _sig(x):
    return 1.0 / (1.0 + math.exp(-x))


def explain_sheet(model, feats, home_vals, away_vals):
    """The stored v2 breakdown for one game: model numbers and each side's raw
    values only (wording is applied by describe_sheet at render time).

    `home_vals` / `away_vals` come from forecast_features.team_values for the
    same state the feature vector was built from. Returns None when the vector
    does not match the artifact or the pushes fail to reproduce the logit, so
    the caller can fall back to explain()."""
    names = model.get('feature_names') or []
    if len(feats) != len(names) or len(names) != len(model['coef']):
        return None
    f = dict(zip(names, feats))
    co = dict(zip(names, model['coef']))
    mu = dict(zip(names, model['scaler_mean']))
    sd = dict(zip(names, model['scaler_std']))
    root = lambda n: n[:-6] if n.endswith('_early') else n
    base = model['intercept'] + sum(
        co[n] * ((f[n] if root(n) in _CONTEXT else 0.0) - mu[n]) / sd[n] for n in names)
    push = {}
    for n in names:
        if root(n) not in _CONTEXT:
            push[root(n)] = push.get(root(n), 0.0) + co[n] * f[n] / sd[n]
    full = model['intercept'] + sum(co[n] * (f[n] - mu[n]) / sd[n] for n in names)
    if abs(base + sum(push.values()) - full) > 1e-6:
        return None

    groups = {k: sum(push.get(n, 0.0) for n in fs) for k, fs in SHEET_GROUPS}
    grouped = {n for _, fs in SHEET_GROUPS for n in fs}
    rest = sum(v for n, v in push.items() if n not in grouped)
    # Season-to-date scoring is 0-0 before a side has played: no row for it,
    # and its (zero) push rides in "everything else".
    for k in ('ppg', 'papg'):
        if not home_vals.get('g') or not away_vals.get('g'):
            rest += groups.pop(k)

    # Points, not log-odds: walk from 50% through home field, then the groups
    # biggest first, then the rest, and record how far each step moves the
    # home team's chance. The steps sum to the final probability minus 50.
    steps = [('home', base)] + sorted(groups.items(), key=lambda kv: -abs(kv[1])) + [('rest', rest)]
    x, rows = 0.0, []
    for key, lg in steps:
        pts = 100.0 * (_sig(x + lg) - _sig(x))
        x += lg
        vals = {'home': ({'neutral': f['neutral'] >= 0.5}, {}), 'rest': ({}, {})}.get(key)
        if vals is None:
            pick = {'ret_prod': ('ret_prod',), 'last': ('sp', 'svr'), 'papg': ('papg',),
                    'ppg': ('ppg',), 'elo': ('elo',), 'recruit': ('recruit4',)}[key]
            vals = tuple({p: (None if v.get(p) is None else round(float(v[p]), 2)) for p in pick}
                         for v in (home_vals, away_vals))
        rows.append({'key': key, 'logit': round(lg, 4), 'pts': round(pts, 2),
                     'h': vals[0], 'a': vals[1]})
    pre = sum(abs(groups.get(k, 0.0)) for k in _PRESEASON)
    ins = sum(abs(groups.get(k, 0.0)) for k in _IN_SEASON)
    return {'v': 2, 'rows': rows, 'logit': round(full, 4),
            'games_min': f.get('games_min', 0.0),
            'pre_share': round(pre / (pre + ins), 3) if pre + ins > 0 else None}


_SHEET_LABELS = {
    'home': 'Home field', 'ret_prod': 'Returning production', 'last': "Last season's ratings",
    'papg': 'Points allowed', 'ppg': 'Points scored', 'elo': 'Results so far',
    'recruit': 'Recruiting', 'rest': 'Everything else',
}


def _sheet_cell(key, v, other):
    """(big text, small caption, is this side the better one) for one side of
    a statistical row (home field and "everything else" are worded inline)."""
    if key == 'last':
        sp, svr = v.get('sp'), v.get('svr')
        if sp is None and svr is None:
            return ('—', 'no FBS season', False)
        o = other.get('sp') if sp is not None else other.get('svr')
        mine = sp if sp is not None else svr
        cap = f'SP+, Savant {svr:+.1f}' if sp is not None and svr is not None else ('SP+' if sp is not None else 'Savant')
        return (f'{mine:+.1f}', cap, o is not None and mine > o)
    spec = {'ret_prod': ('{:.0f}%', 'returning', True), 'papg': ('{:.1f}', 'a game vs FBS', False),
            'ppg': ('{:.1f}', 'a game vs FBS', True), 'elo': ('{:.0f}', 'Elo', True),
            'recruit': ('{:.0f}', '4-year class avg', True)}[key]
    k = {'ret_prod': 'ret_prod', 'papg': 'papg', 'ppg': 'ppg', 'elo': 'elo', 'recruit': 'recruit4'}[key]
    mine, o = v.get(k), other.get(k)
    if mine is None:
        return ('—', spec[1], False)
    better = o is not None and ((mine > o) if spec[2] else (mine < o))
    return (spec[0].format(mine), spec[1], better)


def describe_sheet(sheet):
    """Display rows for a stored explain_sheet() dict, biggest push first with
    "everything else" last: {key, label, pts, toward ('home'|'away'), width,
    h/a: {txt, cap, edge}}. Labels and captions live here, not in the data."""
    rows = [r for r in sheet.get('rows', []) if r.get('key') in _SHEET_LABELS]
    if not rows:
        return {}
    peak = max(abs(r.get('pts', 0.0)) for r in rows) or 1.0
    out = []
    for r in rows:
        k, pts = r['key'], r.get('pts', 0.0)
        h, a = r.get('h') or {}, r.get('a') or {}
        neutral = k == 'home' and h.get('neutral')
        if k == 'home':
            # At a neutral site there is no home field; what remains is the
            # model's small venue-and-week baseline, so it is named for that.
            hc = (None, None, False) if neutral else ('Home', None, True)
            ac = (None, None, False) if neutral else ('Away', None, False)
        elif k == 'rest':
            hc = ac = (None, None, False)
        else:
            hc, ac = _sheet_cell(k, h, a), _sheet_cell(k, a, h)
        out.append({'key': k, 'label': 'Neutral site' if neutral else _SHEET_LABELS[k], 'pts': abs(pts),
                    'note': ("Rest days, transfers, this year's class" if k == 'rest' else
                             'No home field; the baseline for this week' if neutral else None),
                    'toward': 'home' if pts >= 0 else 'away',
                    'width': round(abs(pts) / peak * 50, 1),
                    'h': {'txt': hc[0], 'cap': hc[1], 'edge': hc[2]},
                    'a': {'txt': ac[0], 'cap': ac[1], 'edge': ac[2]}})
    body = sorted((r for r in out if r['key'] != 'rest'), key=lambda r: -r['pts'])
    return {'sheet': True, 'rows': body + [r for r in out if r['key'] == 'rest'],
            'home_prob': _sig(sheet.get('logit', 0.0)),
            'games_min': int(round(sheet.get('games_min') or 0)),
            'pre_share': sheet.get('pre_share')}
