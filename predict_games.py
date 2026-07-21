"""Savant Forecast — weekly prediction + scoring step (end of the cron chain).

Two passes, both against the game_predictions table:

  1. SCORE: any previously predicted game that has since completed gets its
     result recorded (home_won, correct). Scored rows are frozen — the
     prediction that was on the page before kickoff is the one the public
     accuracy tracker is graded on, never retro-edited.
  2. PREDICT: every not-yet-completed FBS-vs-FBS game of the active season
     gets a fresh forecast (win probability + expected margin). Re-running
     before kickoff simply refreshes the row with the latest Elo state.

Inference is a dot product against forecast_model.json (trained locally by
train_forecast.py) — no sklearn, no numpy, no new deploy dependencies. The
feature vector comes from forecast_features._feature_vector, the exact code
path training used, extended from the replayed post-2016 game state.

Usage:  python3 predict_games.py
"""
import json
import math
import os

os.environ.setdefault('POOL_BACKFILL', '1')
import main
from season_util import current_cfb_season
from forecast_features import (build_dataset, _feature_vector, _parse_dt,
                               ELO_START, ELO_CARRY, ELO_NEW_TEAM)

MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'forecast_model.json')


def main_():
    with open(MODEL_PATH) as f:
        model = json.load(f)
    season = current_cfb_season()

    # Replay history through every completed game (2016 -> now) to get current
    # Elo / season-to-date state, including the active season's played games.
    _, _, state = build_dataset(first_season=2016, last_season=season, return_state=True)
    elo, season_of, stats = state['elo'], state['season_of'], state['stats']
    refs = state['refs']

    # If the replay's newest season is older than the active one (offseason:
    # no games played yet), the per-season stats are stale — predictions for
    # the new season start from a fresh 0-0 slate.
    if state['cur_season'] != season:
        stats = {}

    conn = main.get_db()
    try:
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS game_predictions (
                game_id          BIGINT PRIMARY KEY,
                season           INTEGER NOT NULL,
                week             INTEGER,
                home_team        TEXT, away_team TEXT,
                home_prob        REAL,      -- P(home team wins)
                predicted_margin REAL,      -- expected home margin (points)
                model_version    INTEGER,
                predicted_at     TIMESTAMPTZ DEFAULT now(),
                scored           INTEGER DEFAULT 0,
                home_won         INTEGER,
                correct          INTEGER
            )
        ''')

        # ── pass 1: score completed predictions (then freeze them) ──────────
        cur.execute('''
            UPDATE game_predictions p SET
                scored = 1,
                home_won = CASE WHEN g.home_points > g.away_points THEN 1 ELSE 0 END,
                correct  = CASE WHEN (p.home_prob >= 0.5) =
                                     (g.home_points > g.away_points) THEN 1 ELSE 0 END
            FROM games g
            WHERE g.id = p.game_id AND g.completed = 1
              AND g.home_points IS NOT NULL AND g.away_points IS NOT NULL
              AND g.home_points <> g.away_points AND p.scored = 0
        ''')
        print(f"scored {cur.rowcount} completed predictions", flush=True)

        # ── pass 2: predict upcoming games of the active season ─────────────
        # FBS membership: sp_ratings has no rows for a season that hasn't been
        # played, so the upcoming season uses the teams table (current, which
        # is exactly right for the upcoming year).
        cur.execute('SELECT name FROM teams WHERE conference IS NOT NULL '
                    'AND conference <> ALL(%s)', (list(main.FCS_CONFS),))
        fbs_now = {r[0] for r in cur.fetchall()}

        cur.execute('''
            SELECT id, week, season_type, home_team, away_team,
                   COALESCE(neutral_site, 0), start_date
            FROM games WHERE season = %s AND completed = 0
            ORDER BY week, id
        ''', (season,))
        upcoming = cur.fetchall()

        # Returning production for the upcoming season (site's own definition;
        # reads the precomputed store, computes on miss).
        if not any(k[0] == season for k in refs['retprod']):
            data = main._returning_production_ranks.uncached(season) \
                if hasattr(main._returning_production_ranks, 'uncached') \
                else main._returning_production_ranks(season)
            for team, d in data.get('teams', {}).items():
                if d.get('overall_pct') is not None:
                    refs['retprod'][(season, team)] = d['overall_pct']

        mu, sd = model['scaler_mean'], model['scaler_std']
        coef, b0 = model['coef'], model['intercept']
        mcoef, mb0 = model['margin_coef'], model['margin_intercept']

        def rest_days(team, kickoff_dt):
            """Days between the team's last completed game and this kickoff —
            the same computation (and ±14 clamp, 7.0 default) training used."""
            s = stats.get(team)
            if kickoff_dt is None or not s or s.get('last') is None:
                return 7.0
            return max(-14.0, min(14.0, (kickoff_dt - s['last']).days))

        n = 0
        for gid, week, stype, home, away, neutral, start_date in upcoming:
            if home not in fbs_now or away not in fbs_now:
                continue
            # Preseason Elo carry for teams whose rating was last touched in a
            # prior season (applied once — season_of is updated to match).
            for t in (home, away):
                if t not in elo:
                    elo[t] = ELO_NEW_TEAM
                elif season_of.get(t, season) != season:
                    elo[t] = ELO_START + ELO_CARRY * (elo[t] - ELO_START)
                season_of[t] = season

            post = 1.0 if 'POST' in (stype or '').upper() else 0.0
            kick = _parse_dt(start_date)
            feats = _feature_vector(season, week, neutral, post, home, away,
                                    elo, stats, refs['sp'], refs['savant'],
                                    refs['recruit'], refs['transfer'], refs['retprod'],
                                    rest_days(home, kick), rest_days(away, kick))
            z = [(f - m) / s for f, m, s in zip(feats, mu, sd)]
            logit = b0 + sum(c * v for c, v in zip(coef, z))
            prob = 1.0 / (1.0 + math.exp(-logit))
            margin = mb0 + sum(c * v for c, v in zip(mcoef, z))

            cur.execute('''
                INSERT INTO game_predictions
                    (game_id, season, week, home_team, away_team,
                     home_prob, predicted_margin, model_version, predicted_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (game_id) DO UPDATE SET
                    week = EXCLUDED.week, home_prob = EXCLUDED.home_prob,
                    predicted_margin = EXCLUDED.predicted_margin,
                    model_version = EXCLUDED.model_version, predicted_at = now()
                WHERE game_predictions.scored = 0
            ''', (gid, season, week, home, away,
                  round(prob, 4), round(margin, 1), model['version']))
            n += 1
        conn.commit()
        print(f"predicted {n} upcoming {season} games", flush=True)
    finally:
        main.release_db(conn)


if __name__ == '__main__':
    main_()


# Data changed — tell the live site to drop its in-memory page cache so the
# update is visible immediately instead of after the cache TTL.
try:
    from cache_notify import notify_cache_clear
    notify_cache_clear()
except Exception:
    pass
