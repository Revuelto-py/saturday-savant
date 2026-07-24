"""Savant Forecast — candidate feature exploration (LOCAL ONLY, never deployed).

Builds candidate features alongside the production 16 and evaluates each ONE AT
A TIME against the production model under the established protocol:
walk-forward 2019–2025, McNemar significance on pooled disagreements, Brier /
log-loss, and a coefficient-cannibalization check.

Every candidate obeys the same leakage contract as the production features:
nothing may be knowable only after kickoff. Where a candidate derives from
completed games (rolling form, turnover margin), it uses strictly earlier games
of the same season, mirroring how build_dataset accumulates season-to-date state.

Usage:  python3 forecast_candidates.py           # evaluate all candidates
        python3 forecast_candidates.py combo     # + combined-survivor pass
"""
import math
import os
import sys
from collections import defaultdict

os.environ.setdefault('POOL_BACKFILL', '1')
import main
from forecast_features import build_dataset, FEATURE_NAMES, _parse_dt

EARTH_MI = 3958.8


def haversine(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_MI * math.asin(math.sqrt(a))


def load_candidate_sources():
    """Everything the candidates need, in a handful of queries."""
    conn = main.get_db()
    src = {}
    try:
        cur = conn.cursor()

        # venue coordinates per game (via the home team's usual venue: CFBD
        # weather rows carry venue_id per game, which also covers neutral sites)
        cur.execute('SELECT id, latitude, longitude, elevation, dome FROM venues')
        src['venue'] = {i: (la, lo, el, dm) for i, la, lo, el, dm in cur.fetchall()}
        cur.execute('SELECT game_id, venue_id, temperature, wind_speed, precipitation, '
                    'game_indoors FROM game_weather')
        src['weather'] = {g: (v, t, w, p, ind) for g, v, t, w, p, ind in cur.fetchall()}

        # each team's modal home venue per season -> "home base" for travel
        cur.execute('''
            SELECT g.season, g.home_team, w.venue_id, COUNT(*) AS n
            FROM games g JOIN game_weather w ON w.game_id = g.id
            WHERE COALESCE(g.neutral_site,0) = 0
            GROUP BY g.season, g.home_team, w.venue_id''')
        base = {}
        for season, team, vid, n in cur.fetchall():
            k = (season, team)
            if vid is not None and (k not in base or n > base[k][1]):
                base[k] = (vid, n)
        src['home_venue'] = {k: v[0] for k, v in base.items()}

        cur.execute('SELECT team, season, talent FROM team_talent')
        src['talent'] = {(s, t): v for t, s, v in cur.fetchall() if v is not None}

        cur.execute('SELECT team, season, coach FROM coaches')
        src['coach'] = {(s, t): c for t, s, c in cur.fetchall()}

        cur.execute('SELECT team, season, special_teams_rating FROM sp_ratings')
        src['sp_st'] = {(s, t): v for t, s, v in cur.fetchall() if v is not None}

        cur.execute('SELECT team1, team2 FROM rivalries')
        riv = set()
        for a, b in cur.fetchall():
            riv.add((a, b)); riv.add((b, a))
        src['rivalry'] = riv

        # ESPN stores display names with mascots ("Maine Black Bears"), which
        # never match the games table — resolve the team via the is_home flag.
        cur.execute('''SELECT b.game_id,
                              CASE WHEN b.is_home = 1 THEN g.home_team ELSE g.away_team END,
                              b.turnovers
                       FROM game_boxstats b JOIN games g ON g.id = b.game_id
                       WHERE b.turnovers IS NOT NULL''')
        tov = defaultdict(dict)
        for gid, team, t in cur.fetchall():
            if t is not None:
                tov[gid][team] = t
        src['turnovers'] = tov

        cur.execute('SELECT game_id, spread, spread_open FROM betting_lines '
                    'WHERE spread_open IS NOT NULL')
        src['line_move'] = {g: (sp, op) for g, sp, op in cur.fetchall()}

        cur.execute('SELECT season, team, week, sos FROM savant_weekly ORDER BY season, team, week')
        sos = {}
        for s, t, w, v in cur.fetchall():
            if v is not None:
                sos.setdefault((s, t), []).append((w, v))
        src['sos_weekly'] = sos

        # ── EA-rating-weighted transfer net (CANDIDATE, 2026-07 investigation) ──
        # Net EA OVR per (year,team) = sum(incoming OVR) - sum(outgoing OVR),
        # joining transfers.player_id -> ea_ratings. EA CFB 27 is a SINGLE 2026
        # snapshot: only players still active in 2026 carry a rating, so a
        # historical transfer (player since departed) never matches. The coverage
        # print below shows this collapses to ~zero before 2024 — a data-gate
        # failure (Standing Practice #1), kept here to demonstrate it, not because
        # a valid point-in-time walk-forward is possible.
        cur.execute('''
            SELECT year, team, SUM(n) FROM (
                SELECT t.year, t.destination AS team,  SUM(e.overall) n
                  FROM transfers t JOIN ea_ratings e ON e.player_id = t.player_id
                  WHERE t.destination IS NOT NULL GROUP BY t.year, t.destination
                UNION ALL
                SELECT t.year, t.origin AS team, -SUM(e.overall) n
                  FROM transfers t JOIN ea_ratings e ON e.player_id = t.player_id
                  WHERE t.origin IS NOT NULL GROUP BY t.year, t.origin
            ) x GROUP BY year, team''')
        src['xfer_ea'] = {(y, t): float(n) for y, t, n in cur.fetchall()}

        # ── incoming-coach prior-program record (CANDIDATE) ──
        # team-season W-L from completed games + each coach's stint list, so a
        # genuinely NEW coach's prior FBS win% (seasons strictly < the game's) can
        # be looked up — the "proven winner vs unproven first-timer" distinction
        # the rejected first-year binary flag could not make. Point-in-time clean.
        cur.execute('''SELECT season, home_team, away_team, home_points, away_points
                       FROM games WHERE completed = 1
                         AND home_points IS NOT NULL AND away_points IS NOT NULL''')
        rec = defaultdict(lambda: [0, 0])
        for s, h, a, hp, ap in cur.fetchall():
            if hp == ap:
                continue
            rec[(s, h)][1] += 1; rec[(s, a)][1] += 1
            if hp > ap:
                rec[(s, h)][0] += 1
            else:
                rec[(s, a)][0] += 1
        src['team_rec'] = dict(rec)
        stints = defaultdict(list)
        for (s, t), co in src['coach'].items():
            if co:
                stints[co].append((s, t))
        src['coach_stints'] = dict(stints)
    finally:
        main.release_db(conn)
    return src


def build_candidates(rows, src):
    """Return {candidate_name: [value per row]} aligned with `rows`.

    Rolling state (recent margin, turnover margin) is accumulated in
    chronological order and read BEFORE the current game is folded in, exactly
    as build_dataset does for season-to-date stats.
    """
    out = defaultdict(list)
    recent = defaultdict(list)      # (season, team) -> [margin, ...] chronological
    tovhist = defaultdict(list)     # (season, team) -> [turnover margin, ...]
    last_wk = {}                    # (season, team) -> week of previous game

    def asof_sos(season, team, week, post):
        cutoff = 18 if post else (week or 0) - 1
        best = None
        for w, v in src['sos_weekly'].get((season, team), ()):
            if w <= cutoff:
                best = v
            else:
                break
        return best

    for r in rows:
        s, wk, home, away = r['season'], r['week'], r['home'], r['away']
        post = r['features'][FEATURE_NAMES.index('postseason')] == 1.0
        gid = r['game_id']

        # ── travel distance (away team's trip), altitude change ────────────
        vinfo = src['weather'].get(gid)
        game_vid = vinfo[0] if vinfo else src['home_venue'].get((s, home))
        def coords(vid):
            v = src['venue'].get(vid)
            return (v[0], v[1]) if v and v[0] is not None and v[1] is not None else None
        gv = coords(game_vid)
        av = coords(src['home_venue'].get((s, away)))
        hv = coords(src['home_venue'].get((s, home)))
        trav_a = haversine(*av, *gv) if (av and gv) else None
        trav_h = haversine(*hv, *gv) if (hv and gv) else None
        out['travel_diff'].append(((trav_a or 0.0) - (trav_h or 0.0)) / 1000.0)

        # ── weather ─────────────────────────────────────────────────────────
        _, temp, wind, precip, indoors = (vinfo if vinfo else (None, None, None, None, None))
        out['wind_speed'].append(float(wind) if (wind is not None and not indoors) else 0.0)
        out['temp_dev'].append(abs(float(temp) - 60.0) / 10.0 if (temp is not None and not indoors) else 0.0)
        out['precip'].append(float(precip) if (precip is not None and not indoors) else 0.0)

        # ── prior-season roster talent + SP+ special teams ──────────────────
        th = src['talent'].get((s - 1, home)); ta = src['talent'].get((s - 1, away))
        out['talent_diff'].append(((th or 0.0) - (ta or 0.0)) / 100.0)
        sh = src['sp_st'].get((s - 1, home)); sa = src['sp_st'].get((s - 1, away))
        out['sp_st_diff'].append((sh or 0.0) - (sa or 0.0))

        # ── first-year head coach (coach differs from prior season) ─────────
        def first_yr(team):
            c, p = src['coach'].get((s, team)), src['coach'].get((s - 1, team))
            return 1.0 if (c and p and c != p) else 0.0
        out['new_coach_diff'].append(first_yr(home) - first_yr(away))

        # ── rivalry ─────────────────────────────────────────────────────────
        out['rivalry'].append(1.0 if (home, away) in src['rivalry'] else 0.0)

        # ── bye week (>= 12 days since last game) ───────────────────────────
        def bye(team):
            lw = last_wk.get((s, team))
            return 1.0 if (lw is not None and wk is not None and (wk - lw) >= 2) else 0.0
        out['bye_diff'].append(bye(home) - bye(away))

        # ── recent form: mean margin over last 3 games ─────────────────────
        def rec(team):
            h = recent[(s, team)][-3:]
            return sum(h) / len(h) if h else 0.0
        out['recent3_diff'].append((rec(home) - rec(away)) / 10.0)

        # ── rolling turnover margin, last 4 games ──────────────────────────
        def tov(team):
            h = tovhist[(s, team)][-4:]
            return sum(h) / len(h) if h else 0.0
        out['tov3_diff'].append(tov(home) - tov(away))

        # ── as-of-week strength of schedule faced ──────────────────────────
        sh_ = asof_sos(s, home, wk, post); sa_ = asof_sos(s, away, wk, post)
        out['sos_asof_diff'].append((sh_ - sa_) if (sh_ is not None and sa_ is not None) else 0.0)

        # ── betting line movement (open -> close), NOT the closing level ────
        lm = src['line_move'].get(gid)
        out['line_move'].append((lm[1] - lm[0]) if lm and lm[0] is not None and lm[1] is not None else 0.0)

        # ── EA-weighted transfer net diff (CANDIDATE) ──────────────────────
        eh = src['xfer_ea'].get((s, home), 0.0); ea_ = src['xfer_ea'].get((s, away), 0.0)
        out['xfer_ea_diff'].append((eh - ea_) / 100.0)

        # ── incoming-coach prior-program win% diff (CANDIDATE) ─────────────
        # Non-zero only for a real coaching change (coach differs from prior
        # season, which needs season-1 present -> first detectable in 2017).
        # Prior FBS record shrunk toward .500 (k=10 pseudo-games) so a thin
        # sample can't spike; centered so an average past record reads 0 and a
        # first-time HC (no prior stint) also reads 0 (genuinely unknown).
        def coach_prior(team):
            c = src['coach'].get((s, team)); p = src['coach'].get((s - 1, team))
            if not (c and p and c != p):
                return 0.0
            w = g = 0
            for ps, pt in src['coach_stints'].get(c, ()):
                if ps < s:
                    rw, rg = src['team_rec'].get((ps, pt), (0, 0))
                    w += rw; g += rg
            return (w + 5.0) / (g + 10.0) - 0.5
        out['coach_prior_diff'].append(coach_prior(home) - coach_prior(away))

        # ── fold THIS game into rolling state (after features are recorded) ─
        hp, ap = r['home_points'], r['away_points']
        recent[(s, home)].append(hp - ap); recent[(s, away)].append(ap - hp)
        tg = src['turnovers'].get(gid) or {}
        if home in tg and away in tg:
            tovhist[(s, home)].append(tg[away] - tg[home])
            tovhist[(s, away)].append(tg[home] - tg[away])
        if wk is not None:
            last_wk[(s, home)] = wk; last_wk[(s, away)] = wk
    return out


CANDIDATES = ['travel_diff', 'wind_speed', 'temp_dev', 'precip', 'talent_diff',
              'sp_st_diff', 'new_coach_diff', 'rivalry', 'bye_diff',
              'recent3_diff', 'tov3_diff', 'sos_asof_diff', 'line_move',
              # 2026-07 investigation: talent-weighted transfers + coach quality
              'xfer_ea_diff', 'coach_prior_diff']

# Candidates added by the 2026-07 transfer/coaching investigation, reported in
# detail (coverage + walk-forward) regardless of outcome.
INVESTIGATION_2026_07 = ['xfer_ea_diff', 'coach_prior_diff']


# ── evaluation harness ──────────────────────────────────────────────────────
def _eval():
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import brier_score_loss, log_loss
    from math import comb

    rows, _ = build_dataset()
    rows = [r for r in rows if not r['burn_in']]
    src = load_candidate_sources()
    cand = build_candidates(rows, src)
    base = np.array([r['features'] for r in rows], dtype=float)
    yall = np.array([r['target'] for r in rows], dtype=int)
    seasons = np.array([r['season'] for r in rows])
    print(f"rows={len(rows)}  base features={base.shape[1]}  candidates={len(CANDIDATES)}\n")

    # ── Standing Practice #1: distribution + per-season coverage of the new
    #    candidates BEFORE reading any evaluation number (the Maine lesson). ──
    print("=== coverage / distribution of investigation candidates ===")
    for c in INVESTIGATION_2026_07:
        col = np.array(cand[c], dtype=float)
        by = "  ".join(f"{S}:{np.mean(col[seasons == S] != 0) * 100:.0f}%"
                       for S in range(2019, 2026))
        print(f"{c:16} overall nonzero={np.mean(col != 0) * 100:4.1f}%  "
              f"mean={col.mean():+.4f} std={col.std():.4f}  by-season[{by}]")
    print()

    def walk(X):
        """Walk-forward 2019-2025: returns (weighted acc, weighted brier, preds by row idx)."""
        accs = wts = brs = 0.0
        preds = np.full(len(X), np.nan)
        for S in range(2019, 2026):
            tri = (seasons >= 2017) & (seasons < S)
            vai = seasons == (S - 1)
            tei = seasons == S
            if tei.sum() == 0:
                continue
            Xtr, ytr = X[tri], yall[tri]
            mu, sd = Xtr.mean(0), Xtr.std(0); sd[sd == 0] = 1
            best = None
            for C in (0.01, 0.03, 0.1, 0.3, 1.0):
                m = LogisticRegression(C=C, max_iter=3000).fit((Xtr - mu) / sd, ytr)
                ll = log_loss(yall[vai], m.predict_proba((X[vai] - mu) / sd)[:, 1])
                if best is None or ll < best[0]:
                    best = (ll, m)
            p = best[1].predict_proba((X[tei] - mu) / sd)[:, 1]
            preds[tei] = p
            n = tei.sum()
            accs += ((p >= .5).astype(int) == yall[tei]).mean() * n
            brs += brier_score_loss(yall[tei], p) * n
            wts += n
        return accs / wts, brs / wts, preds

    def mcnemar(p0, p1):
        m = ~np.isnan(p0) & ~np.isnan(p1)
        c0 = (p0[m] >= .5).astype(int) == yall[m]
        c1 = (p1[m] >= .5).astype(int) == yall[m]
        n01 = int((c0 & ~c1).sum()); n10 = int((~c0 & c1).sum()); n = n01 + n10
        if n == 0:
            return n01, n10, 1.0
        p = min(1.0, 2 * sum(comb(n, i) for i in range(min(n01, n10) + 1)) / 2 ** n)
        return n01, n10, p

    a0, b0_, p0 = walk(base)
    print(f"BASELINE (production 16 feat): walk-forward acc={a0:.4f}  brier={b0_:.4f}\n")
    print(f"{'candidate':16}{'acc':>8}{'Δacc':>8}{'brier':>9}{'Δbrier':>9}{'v1>':>5}{'v2>':>5}{'p':>8}  verdict")
    results = {}
    for c in CANDIDATES:
        col = np.array(cand[c], dtype=float).reshape(-1, 1)
        a1, b1, p1 = walk(np.hstack([base, col]))
        n01, n10, pv = mcnemar(p0, p1)
        passes = (a1 - a0) > 0.002 and pv < 0.05
        results[c] = (a1 - a0, b1 - b0_, pv, passes)
        print(f"{c:16}{a1:>8.4f}{a1-a0:>+8.4f}{b1:>9.4f}{b1-b0_:>+9.4f}{n01:>5}{n10:>5}{pv:>8.3f}  "
              f"{'PASS' if passes else 'reject'}")
    return results


if __name__ == '__main__':
    _eval()
