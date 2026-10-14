"""Season simulator: play out the rest of the season many times and count how
often each FBS team reaches each stage (title game, conference title, Playoff,
bye, semifinal, final, national title).

Inputs
  * completed results are fixed (records from _compute_conference_standings)
  * every remaining game through selection day is decided by its stored Savant
    Forecast probability (game_predictions, scored = 0)
  * conference title games pair the top two by conference record; they and the
    Playoff games use the Savant Rating gap (logistic, 9-point scale)

Selection and seeding follow the committee's 2026 protocol:
  * automatic bids for the ACC, Big Ten, Big 12 and SEC champions, and for the
    single highest-ranked team from the American, C-USA, MAC, Mountain West,
    Pac-12 and Sun Belt (champion or not)
  * Notre Dame is in if ranked in the top 12; the rest fill by ranking
  * seeds follow the ranking (top four = byes); an automatic qualifier ranked
    outside the top 12 is seeded last
  * games after selection day are ignored (Army-Navy)
  * first-round games are on campus (seeds 5-8 host); the rest are neutral

The committee's RANKING is a stand-in: Savant Rating + 3.5 per game over .500 +
half the strength of schedule, then a head-to-head pass among near-equal teams.
ponytail: formula ranking; anchor to cfp_rankings once the committee publishes
if the stand-in drifts from it.

Run weekly by pipeline/simulate_season.py; /simulator reads the stored result.
"""
import math
import random

POWER = ('ACC', 'Big 12', 'Big Ten', 'SEC')
GROUP_OF_SIX = ('American Athletic', 'Conference USA', 'Mid-American',
                'Mountain West', 'Pac-12', 'Sun Belt')
INDEPENDENT = 'FBS Independents'
FIELD = 12
# Home field in Savant Rating points: the walk-forward backtest's fitted edge
# under the 2026-10-07 calibration (2017-26, 5,709 games), applied to the
# on-campus first round.
HFA_PTS = 2.7
STAGES = ('ccg', 'champ', 'cfp', 'bye', 'semi', 'final', 'title')


def selection_day(season):
    """First Sunday of December: the committee's final ranking."""
    import datetime
    d = datetime.date(season, 12, 1)
    return (d + datetime.timedelta(days=(6 - d.weekday()) % 7)).isoformat()


def load(cursor, season, standings_fn):
    """Everything a simulation needs, from the database.

    standings_fn is main._compute_conference_standings (passed in so this module
    doesn't import the Flask app)."""
    stand = standings_fn(cursor, season)
    cursor.execute('SELECT team, net_rating, sos FROM savant_ratings WHERE season = %s', (season,))
    rating = {t: (float(n) if n is not None else -10.0, float(s or 0)) for t, n, s in cursor.fetchall()}
    teams = []
    for conf, rows in stand.items():
        for r in rows:
            n = r['name']
            rt, sos = rating.get(n, (-10.0, 0.0))
            teams.append(dict(team=n, conf=conf, w=r['wins'], l=r['losses'],
                              cw=r['conf_wins'], cl=r['conf_losses'], rating=rt, sos=sos))
    idx = {t['team']: i for i, t in enumerate(teams)}
    cursor.execute('''
        SELECT p.home_team, p.away_team, p.home_prob
          FROM game_predictions p JOIN games g ON g.id = p.game_id
         WHERE p.season = %s AND p.scored = 0 AND COALESCE(g.completed, 0) = 0
           AND LEFT(g.start_date, 10) <= %s
    ''', (season, selection_day(season)))
    games = []
    for h, a, p in cursor.fetchall():
        hi, ai = idx.get(h, -1), idx.get(a, -1)
        if hi < 0 and ai < 0:
            continue
        same = (hi >= 0 and ai >= 0 and teams[hi]['conf'] == teams[ai]['conf']
                and teams[hi]['conf'] != INDEPENDENT)
        games.append((hi, ai, float(p), same))
    cursor.execute('''SELECT home_team, away_team, home_points, away_points FROM games
                       WHERE season = %s AND completed = 1 AND home_points IS NOT NULL''', (season,))
    h2h = {}
    for h, a, hp, ap in cursor.fetchall():
        if h in idx and a in idx and hp != ap:
            w, l = (idx[h], idx[a]) if hp > ap else (idx[a], idx[h])
            h2h[(w, l)] = h2h.get((w, l), 0) + 1
    return dict(teams=teams, games=games, h2h=h2h)


def simulate(data, n=10000, seed=20261010):
    """Run n seasons; returns per-team stage probabilities plus mean seed and
    projected wins. Asserts the protocol's invariants on every season."""
    T, G, H = data['teams'], data['games'], data['h2h']
    nT = len(T)
    if nT < FIELD or not any(t['conf'] in GROUP_OF_SIX for t in T):
        return None
    rnd = random.Random(seed).random
    R = [t['rating'] for t in T]
    SOS = [t['sos'] for t in T]
    nd = next((i for i, t in enumerate(T) if t['team'] == 'Notre Dame'), -1)
    confs = {}
    for i, t in enumerate(T):
        if t['conf'] != INDEPENDENT:
            confs.setdefault(t['conf'], []).append(i)

    def pr(a, b):
        return 1 / (1 + math.exp(-(R[a] - R[b]) / 9))

    def play(a, b):
        return a if rnd() < pr(a, b) else b

    def host(h, a):
        # First-round games are on the higher seed's campus, not neutral.
        return h if rnd() < 1 / (1 + math.exp(-(R[h] - R[a] + HFA_PTS) / 9)) else a

    pair_games = {}
    for gi, (h, a, _, _) in enumerate(G):
        if h >= 0 and a >= 0:
            pair_games.setdefault(frozenset((h, a)), []).append(gi)

    count = {k: [0] * nT for k in STAGES}
    seed_sum, win_sum = [0] * nT, [0] * nT
    res = [0] * len(G)
    for _ in range(n):
        w = [t['w'] for t in T]; l = [t['l'] for t in T]
        cw = [t['cw'] for t in T]; cl = [t['cl'] for t in T]
        for gi, (h, a, p, same) in enumerate(G):
            win, lose = (h, a) if rnd() < p else (a, h)
            res[gi] = win
            if win >= 0: w[win] += 1
            if lose >= 0: l[lose] += 1
            if same: cw[win] += 1; cl[lose] += 1
        champ_of, ccg = {}, {}
        for conf, mem in confs.items():
            ranked = sorted(mem, key=lambda i: (-(cw[i] / max(1, cw[i] + cl[i])), -(R[i] + rnd() * 4)))
            a, b = ranked[0], ranked[1]
            ch = play(a, b)
            w[ch] += 1; l[b if ch == a else a] += 1
            champ_of[conf] = ch; ccg[frozenset((a, b))] = ch
            count['ccg'][a] += 1; count['ccg'][b] += 1; count['champ'][ch] += 1

        def h2h_net(x, y):
            net = H.get((x, y), 0) - H.get((y, x), 0)
            for gi in pair_games.get(frozenset((x, y)), ()):
                net += 1 if res[gi] == x else -1
            c = ccg.get(frozenset((x, y)))
            if c is not None:
                net += 1 if c == x else -1
            return net

        score = [R[i] + 3.5 * (w[i] - l[i]) + 0.5 * SOS[i] for i in range(nT)]
        order = sorted(range(nT), key=lambda i: -score[i])
        for i in range(30):
            hi, lo = order[i], order[i + 1]
            if score[hi] - score[lo] < 4 and h2h_net(lo, hi) > 0:
                order[i], order[i + 1] = lo, hi
        pos = {t: i + 1 for i, t in enumerate(order)}

        auto = [champ_of[c] for c in POWER if c in champ_of]
        auto.append(next(t for t in order if T[t]['conf'] in GROUP_OF_SIX))
        if nd >= 0 and pos[nd] <= FIELD:
            auto.append(nd)
        field = list(dict.fromkeys(auto))
        for t in order:
            if len(field) >= FIELD:
                break
            if t not in field:
                field.append(t)
        auto_set = set(auto)
        field.sort(key=lambda t: (t in auto_set and pos[t] > FIELD, pos[t]))
        assert len(field) == FIELD and sum(T[t]['conf'] in POWER and champ_of.get(T[t]['conf']) == t for t in field) == len(POWER)

        f = field
        q = [play(f[0], host(f[7], f[8])), play(f[3], host(f[4], f[11])),
             play(f[1], host(f[6], f[9])), play(f[2], host(f[5], f[10]))]
        f1, f2 = play(q[0], q[1]), play(q[2], q[3])
        title = play(f1, f2)
        for s, t in enumerate(f, start=1):
            count['cfp'][t] += 1; seed_sum[t] += s
            if s <= 4: count['bye'][t] += 1
        for t in q: count['semi'][t] += 1
        count['final'][f1] += 1; count['final'][f2] += 1
        count['title'][title] += 1
        for i in range(nT): win_sum[i] += w[i]

    out = []
    for i, t in enumerate(T):
        row = dict(team=t['team'], conf=t['conf'], w=t['w'], l=t['l'],
                   proj_wins=round(win_sum[i] / n, 2),
                   mean_seed=round(seed_sum[i] / count['cfp'][i], 2) if count['cfp'][i] else None)
        row.update({k: round(count[k][i] / n, 4) for k in STAGES})
        out.append(row)
    out.sort(key=lambda r: (-r['cfp'], -r['title'], r['team']))
    return dict(n=n, teams=out)


if __name__ == '__main__':
    # Self-check on a synthetic league: invariants hold and stronger is likelier.
    confs = list(POWER) + list(GROUP_OF_SIX)
    teams = [dict(team=f'T{c}{k}', conf=c, w=3, l=1, cw=1, cl=1, rating=20 - 2 * k + (10 if c in POWER else 0), sos=0)
             for c in confs for k in range(6)]
    data = dict(teams=teams, games=[], h2h={})
    r = simulate(data, n=400)
    by = {x['team']: x for x in r['teams']}
    assert abs(sum(x['cfp'] for x in r['teams']) - FIELD) < 1e-6
    assert abs(sum(x['cfp'] for x in r['teams'] if x['conf'] in GROUP_OF_SIX) - 1) < 0.2
    assert by['TSEC0']['cfp'] > by['TSEC5']['cfp']
    print('ok')
