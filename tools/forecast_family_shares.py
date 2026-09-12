#!/usr/bin/env python3
"""Recompute FORECAST_FAMILIES in main.py from the trained model artifact.

The /savant-forecast page shows how much of the decision each family of inputs
carries. That share is the family's slice of the summed ABSOLUTE standardized
logistic coefficients — absolute because a negative coefficient (points allowed,
say) is just as influential as a positive one, and standardized because the raw
coefficients are on wildly different scales (Elo points vs a 0/1 neutral flag).

Run after every retrain and paste the four numbers into main.py.

    python3 tools/forecast_family_shares.py
"""
import json
import os
import sys

FAMILIES = {
    'In-season form':      ['elo_diff', 'ppg_diff', 'papg_diff', 'wpct_diff', 'games_min'],
    'Roster & recruiting': ['recruit4_diff', 'ret_prod_diff', 'transfer_diff', 'recruit_diff'],
    'Preseason priors':    ['prior_sp_diff', 'prior_savant_diff', 'prior_missing'],
    'Situation':           ['neutral', 'postseason', 'rest_diff', 'week'],
}

path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    'forecast_model.json')
model = json.load(open(path))
coef = model['coef']
coef = coef[0] if isinstance(coef[0], list) else coef
weight = {name: abs(c) for name, c in zip(model['feature_names'], coef)}

# A feature that moved between families, or one added by a retrain, would
# silently vanish from the chart and leave the shares summing to under 100.
assigned = {f for names in FAMILIES.values() for f in names}
missing = set(weight) - assigned
if missing:
    sys.exit(f'Unassigned features (add them to a family first): {sorted(missing)}')
unknown = assigned - set(weight)
if unknown:
    sys.exit(f'Families name features the model does not have: {sorted(unknown)}')

total = sum(weight.values())
print(f'{model["feature_names"].__len__()} features, trained {model["trained_at"][:10]}\n')
for family, names in FAMILIES.items():
    print(f'  {family:<22} {100 * sum(weight[n] for n in names) / total:5.1f}%')
