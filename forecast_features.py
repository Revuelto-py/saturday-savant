"""Savant Forecast — point-in-time feature pipeline.

Builds one feature row per completed FBS-vs-FBS game (2016–2025) using ONLY
information knowable before that game's kickoff. This is the leakage contract
the whole model rests on:

  ALLOWED
    • An Elo-style rating updated game-by-game from final scores — for a game
      in week N it reflects only games that finished before that kickoff.
      Ratings carry across seasons with regression to the mean; the 2016
      season is Elo burn-in (rows emitted but flagged, excluded from training).
    • Prior-season aggregates: Savant Net rating and SP+ from season S-1 are
      fixed history by the time season S kicks off.
    • Preseason-known roster signals for season S: returning production
      (computed vs S-1), recruiting class points (current + trailing 4-class
      average), and net transfer-portal stars.
    • Game context: home/neutral site, rest-day differential, week number,
      postseason flag, and each side's season-to-date record/scoring computed
      strictly from earlier games.

  EXCLUDED (would leak the answer)
    • Same-season savant_ratings / sp_ratings / team_stats / team_advanced —
      all are end-of-season aggregates that already contain the game's result.
    • AP rankings — only the final poll is stored historically.
    • A conference-game flag — the teams table stores CURRENT conference only,
      so historical conference membership would be silently wrong (realignment).

FBS membership is per season, proxied by presence in that season's sp_ratings
(SP+ covers exactly the FBS field each year, 128–137 teams).
"""
import os
from datetime import datetime, timezone

os.environ.setdefault('POOL_BACKFILL', '1')
import main  # DB pool + the site's own returning-production definition

# ── Elo configuration (fixed, documented; not fitted on test data) ──────────
ELO_START = 1500.0     # league mean
ELO_NEW_TEAM = 1400.0  # FBS newcomers (usually transitioning up) start below mean
ELO_CARRY = 0.60       # preseason = mean + 60% of last season's deviation
ELO_K = 32.0
ELO_HFA = 55.0         # home-field advantage in Elo points (off for neutral sites)

FEATURE_NAMES = [
    'elo_diff',            # home Elo − away Elo (no HFA baked in)
    'prior_savant_diff',   # prior-season Savant Net, home − away (0 when missing)
    'prior_sp_diff',       # prior-season SP+ rating, home − away (0 when missing)
    'prior_missing',       # 1 if either side lacks BOTH priors (new FBS team)
    'ret_prod_diff',       # returning production overall %, home − away
    'recruit_diff',        # recruiting points, current class, home − away
    'recruit4_diff',       # trailing 4-class average points, home − away
    'transfer_diff',       # net transfer-portal stars this cycle, home − away
    'wpct_diff',           # season-to-date win%, home − away
    'ppg_diff',            # season-to-date points/game, home − away
    'papg_diff',           # season-to-date points-allowed/game, home − away
    'games_min',           # min(games played) — how much in-season signal exists
    'rest_diff',           # rest days home − away, clamped ±14
    'week',
    'neutral',             # 1 = neutral site (home-field intercept shouldn't apply)
    'postseason',
]


def _parse_dt(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _load_reference(cur, seasons):
    """All prior/preseason lookups in a handful of queries."""
    cur.execute('SELECT season, team, rating FROM sp_ratings')
    sp = {(s, t): r for s, t, r in cur.fetchall() if r is not None}
    fbs = {}
    cur.execute('SELECT season, team FROM sp_ratings')
    for s, t in cur.fetchall():
        fbs.setdefault(s, set()).add(t)

    cur.execute('SELECT season, team, net_rating FROM savant_ratings')
    savant = {(s, t): r for s, t, r in cur.fetchall() if r is not None}

    cur.execute('SELECT year, team, points FROM team_recruiting')
    recruit = {(y, t): p for y, t, p in cur.fetchall() if p is not None}

    # Net transfer stars per (year, team): incoming minus outgoing, unrated
    # transfers counted at a modest 2 stars so volume still registers.
    cur.execute('''
        SELECT year, team, SUM(n) FROM (
            SELECT year, destination AS team,  SUM(COALESCE(stars, 2)) AS n
            FROM transfers WHERE destination IS NOT NULL GROUP BY year, destination
            UNION ALL
            SELECT year, origin AS team, -SUM(COALESCE(stars, 2)) AS n
            FROM transfers WHERE origin IS NOT NULL GROUP BY year, origin
        ) x GROUP BY year, team
    ''')
    transfer = {(y, t): float(n) for y, t, n in cur.fetchall()}

    # Returning production per season via the site's own definition (reads the
    # precomputed store when present, computes+stores on miss). 2016 has no
    # prior season loaded, so it stays empty and the feature reads 0 (burn-in).
    retprod = {}
    for s in seasons:
        if s - 1 < min(seasons):
            continue
        data = main._returning_production_ranks.uncached(s) \
            if hasattr(main._returning_production_ranks, 'uncached') \
            else main._returning_production_ranks(s)
        for team, d in data.get('teams', {}).items():
            if d.get('overall_pct') is not None:
                retprod[(s, team)] = d['overall_pct']
    return sp, fbs, savant, recruit, transfer, retprod


def build_dataset(first_season=2016, last_season=2025):
    """Returns (rows, feature_names). Each row: meta + 'features' vector +
    'target' (1 = home win). Rows are emitted for every completed FBS-vs-FBS
    game; 'burn_in' flags season 2016 (Elo warm-up, excluded from training)."""
    seasons = list(range(first_season, last_season + 1))
    conn = main.get_db()
    try:
        cur = conn.cursor()
        sp, fbs, savant, recruit, transfer, retprod = _load_reference(cur, seasons)
        cur.execute('''
            SELECT id, season, week, season_type, home_team, away_team,
                   home_points, away_points, start_date,
                   COALESCE(neutral_site, 0)
            FROM games
            WHERE completed = 1 AND season = ANY(%s)
            ORDER BY season, start_date NULLS LAST, id
        ''', (seasons,))
        games = cur.fetchall()
        cur.execute('SELECT game_id, spread FROM betting_lines')
        vegas = {gid: s for gid, s in cur.fetchall() if s is not None}
    finally:
        main.release_db(conn)

    elo = {}                    # team -> rating (carried across seasons)
    season_of = {}              # team -> season the rating was last touched
    rows = []
    cur_season = None
    stats = {}                  # (team) -> season-to-date {g,w,pf,pa,last_dt}

    def recruit4(season, team):
        vals = [recruit.get((y, team)) for y in range(season - 3, season + 1)]
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else 0.0

    for gid, season, week, stype, home, away, hp, ap, sd, neutral in games:
        if home not in fbs.get(season, set()) or away not in fbs.get(season, set()):
            continue
        if hp is None or ap is None or hp == ap:
            continue  # unplayed edge case or (pre-OT era) tie — no target

        if season != cur_season:
            cur_season = season
            stats = {}

        for t in (home, away):
            if t not in elo:
                elo[t] = ELO_START if season == first_season else ELO_NEW_TEAM
            elif season_of.get(t, season) != season:
                elo[t] = ELO_START + ELO_CARRY * (elo[t] - ELO_START)
            season_of[t] = season
            stats.setdefault(t, {'g': 0, 'w': 0, 'pf': 0, 'pa': 0, 'last': None})

        hs, as_ = stats[home], stats[away]
        dt = _parse_dt(sd)

        def rest(s):
            if dt is None or s['last'] is None:
                return 7.0
            return max(-14.0, min(14.0, (dt - s['last']).days))

        post = 1.0 if 'POST' in (stype or '').upper() else 0.0
        prior_missing = 1.0 if (
            (season - 1, home) not in savant and (season - 1, home) not in sp
            or (season - 1, away) not in savant and (season - 1, away) not in sp) else 0.0

        feats = [
            elo[home] - elo[away],
            savant.get((season - 1, home), 0.0) - savant.get((season - 1, away), 0.0),
            sp.get((season - 1, home), 0.0) - sp.get((season - 1, away), 0.0),
            prior_missing,
            retprod.get((season, home), 50.0) - retprod.get((season, away), 50.0),
            recruit.get((season, home), 0.0) - recruit.get((season, away), 0.0),
            recruit4(season, home) - recruit4(season, away),
            transfer.get((season, home), 0.0) - transfer.get((season, away), 0.0),
            (hs['w'] / hs['g'] if hs['g'] else 0.5) - (as_['w'] / as_['g'] if as_['g'] else 0.5),
            (hs['pf'] / hs['g'] if hs['g'] else 0.0) - (as_['pf'] / as_['g'] if as_['g'] else 0.0),
            (hs['pa'] / hs['g'] if hs['g'] else 0.0) - (as_['pa'] / as_['g'] if as_['g'] else 0.0),
            float(min(hs['g'], as_['g'])),
            rest(hs) - rest(as_),
            float(week or 0),
            float(neutral),
            post,
        ]

        rows.append({
            'game_id': gid, 'season': season, 'week': week,
            'home': home, 'away': away,
            'home_points': hp, 'away_points': ap,
            'target': 1 if hp > ap else 0,
            'vegas_spread': vegas.get(gid),
            'elo_home': elo[home], 'elo_away': elo[away],
            'burn_in': season == first_season,
            'features': feats,
        })

        # ── Elo update (AFTER the features are recorded) ────────────────────
        h_eff = elo[home] + (0.0 if neutral else ELO_HFA)
        exp_home = 1.0 / (1.0 + 10 ** ((elo[away] - h_eff) / 400.0))
        margin = abs(hp - ap)
        winner_elo_diff = (h_eff - elo[away]) if hp > ap else (elo[away] - h_eff)
        import math
        mov = math.log(margin + 1) * (2.2 / (winner_elo_diff * 0.001 + 2.2))
        delta = ELO_K * mov * ((1 if hp > ap else 0) - exp_home)
        elo[home] += delta
        elo[away] -= delta

        # season-to-date update
        hs['g'] += 1; as_['g'] += 1
        hs['w'] += 1 if hp > ap else 0
        as_['w'] += 1 if ap > hp else 0
        hs['pf'] += hp; hs['pa'] += ap
        as_['pf'] += ap; as_['pa'] += hp
        if dt is not None:
            hs['last'] = dt; as_['last'] = dt

    return rows, FEATURE_NAMES


if __name__ == '__main__':
    rows, names = build_dataset()
    from collections import Counter
    by_season = Counter(r['season'] for r in rows)
    n_vegas = sum(1 for r in rows if r['vegas_spread'] is not None)
    print(f"rows: {len(rows)}  (vegas lines on {n_vegas})")
    print("by season:", dict(sorted(by_season.items())))
    print(f"home win rate: {sum(r['target'] for r in rows) / len(rows):.3f}")
