"""Savant Rating (SVR) — Saturday Savant's proprietary team efficiency model.

Computes an opponent-adjusted Offensive Rating, Defensive Rating, and Net
Rating for every FBS team, in the spirit of tempo-free efficiency systems
from other sports, rebuilt around football's natural unit of tempo: the
drive.

METHODOLOGY (documented decisions)
──────────────────────────────────
Unit — points per 10 drives.
    Basketball efficiency uses points per 100 possessions; football's
    possession is the drive. An FBS team runs ~10-12 countable drives per
    game, so scaling per-drive efficiency by 10 lands the numbers on a
    familiar "points per game vs an average opponent" scale (a 30.0
    offense reads like a 30-point offense) while staying fully
    pace-neutral: slow, grinding teams are not punished for having fewer
    possessions.

Drive inclusion rules (what counts as a possession):
    • FBS vs FBS games only. Games against FCS opponents are dropped
      entirely — the opponent cannot be rated, so the game cannot be
      opponent-adjusted, and cupcake blowouts would only inflate ratings.
    • Overtime drives excluded: OT possessions start at the opponent 25,
      which breaks the points-per-drive scale.
    • Kneel-out drives excluded: drives that end a half/game with ≤3
      plays and ≤5 yards are clock management, not possessions.
    • Zero-play administrative "drives" excluded.
    • Garbage-time drives excluded: any drive beginning when the score
      margin exceeds 38 in Q2, 28 in Q3, or 21 in Q4. This is the
      football answer to the margin-of-victory question: instead of
      capping blowout scores after the fact, the possessions in which
      neither side is playing normal football never enter the sample.
      Within competitive play there is deliberately NO cap — a drive can
      yield at most 8 points, so the per-drive unit already bounds how
      much any single event can swing a rating.
    • Defensive and special-teams touchdowns are excluded by
      construction: a drive's points are measured as the change in the
      OFFENSE's score during its own drive, so a pick-six neither counts
      as offensive production for the defense's team nor as "points
      allowed" by the defense that scored it. PATs and 2-point tries ride
      along with the touchdown play's running score, so touchdown drives
      are worth their true 6/7/8 points.

Home-field neutralization:
    League-wide home vs away points-per-drive is measured from the same
    drive sample, and each non-neutral game's raw efficiencies are scaled
    by the square root of that ratio (home offense down, away offense up)
    so every rating is expressed at a neutral site. Games carrying an
    event note (bowls, playoff games, kickoff classics) are treated as
    neutral-site.

Opponent adjustment — iterative, KenPom-style:
    A team's adjusted offensive efficiency in one game is its raw
    (neutralized) points per drive × (national average PPD ÷ opponent's
    adjusted defensive efficiency); defense mirrors it. Team season
    values are the drive- and recency-weighted average of game values,
    and the whole system is recomputed until it converges (a full pass
    changes no rating by more than 1e-9). With ~130 teams × ~12 games the
    fixed point is reached in well under a second, and iteration is
    strictly better than a one-pass adjustment here: one pass corrects
    for opponents' raw strength, which is itself polluted by *their*
    schedules — iteration removes that bias to any depth.

Recency weighting:
    Later games get up to 1.35× the weight of the opener (linear ramp).
    The system asks "how good is this team today?", not "how good was
    their September?" — the same predictive philosophy as the reference
    system, kept gentle because football's 12-game season offers far
    fewer samples than basketball's 30.

Early-season stability — Bayesian prior:
    Every team's weighted game sample is blended with PRIOR_DRIVES
    pseudo-drives at the national average efficiency. Over a full season
    (~120+ countable drives each way) the prior contributes only a few
    percent; if the script is re-run in September with two games played
    it pulls extreme small-sample teams toward average instead of letting
    a 2-0 team post an absurd rating. This replaces any hard games-played
    minimum — teams are always ratable, just shrunk while unproven.

Net Rating = Offensive Rating − Defensive Rating: the expected scoring
margin per 10 drives (≈ one game) against an average FBS team on a
neutral field. Purely predictive of current strength — it does not care
about win-loss résumé, and a team can rank above another it lost to.

Usage:
    python3 compute_savant_ratings.py            # dry run: compute + validate, no writes
    python3 compute_savant_ratings.py --write    # also (re)create and fill savant_ratings
"""

import gzip
import json
import os
import sys

import psycopg2
from dotenv import load_dotenv

from season_util import current_cfb_season

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=True)

# Season comes from the CLI (first numeric arg); the active season is the
# default. The active season is computed from the stored ESPN game summaries
# (the original, validated path). Older seasons have no ESPN summaries — they
# are computed from CFBD's drives feed (one API call per season), mapped into
# the exact same per-game aggregates with the same exclusion rules.
SEASON = next((int(a) for a in sys.argv[1:] if a.isdigit()), current_cfb_season())

# FCS conferences — mirrors FCS_CONFS in main.py
FCS_CONFS = ('CAA', 'Big Sky', 'MVFC', 'SWAC', 'MEAC', 'Southland', 'Big South',
             'OVC', 'Patriot', 'NEC', 'Pioneer', 'FCS Independents')

# Garbage time: margin at drive start beyond which the drive is excluded
GARBAGE_MARGIN = {1: None, 2: 38, 3: 28, 4: 21}

PRIOR_DRIVES   = 25      # pseudo-drives at national average blended into every team
RECENCY_MAX    = 0.35    # last game weighted (1 + this) × the first game
CONVERGENCE    = 1e-9
MAX_ITERATIONS = 500
SCALE          = 10      # ratings expressed per 10 drives


def parse_game_drives(summary, home_name, away_name):
    """Yield (offense_name, defense_name, points, period, margin_at_start)
    for every countable drive in one game. Returns None if the summary
    has no usable drive data."""
    drives = (summary.get('drives') or {}).get('previous') or []
    if not drives:
        return None

    # Map ESPN competitor id -> our home/away team name
    side_by_id = {}
    for comp in (summary.get('header', {}).get('competitions') or []):
        for c in comp.get('competitors', []):
            tid = (c.get('team') or {}).get('id')
            if tid is not None:
                side_by_id[str(tid)] = home_name if c.get('homeAway') == 'home' else away_name
    if len(side_by_id) < 2:
        return None

    out = []
    run_home, run_away = 0, 0    # running score entering each drive
    for d in drives:
        tid = str((d.get('team') or {}).get('id'))
        offense = side_by_id.get(tid)

        plays = d.get('plays') or []
        # advance running score from the drive's plays (scores are cumulative)
        end_home, end_away = run_home, run_away
        for p in plays:
            hs, as_ = p.get('homeScore'), p.get('awayScore')
            if hs is not None and as_ is not None:
                end_home = max(end_home, hs)
                end_away = max(end_away, as_)

        start_home, start_away = run_home, run_away
        run_home, run_away = end_home, end_away

        if offense is None:
            continue

        period = ((d.get('start') or {}).get('period') or {}).get('number') or 1

        # ── exclusions ──
        if period > 4:                                   # overtime
            continue
        n_plays = d.get('offensivePlays') or len(plays)
        if n_plays == 0:                                 # administrative
            continue
        result = (d.get('displayResult') or d.get('result') or '').lower()
        if 'end of' in result and n_plays <= 3 and (d.get('yards') or 0) <= 5:
            continue                                     # kneel-out
        limit = GARBAGE_MARGIN.get(period)
        if limit is not None and abs(start_home - start_away) > limit:
            continue                                     # garbage time

        if offense == home_name:
            pts = end_home - start_home
            defense = away_name
        else:
            pts = end_away - start_away
            defense = home_name
        pts = max(0, min(8, pts))                        # guard against data glitches
        out.append((offense, defense, pts, period))
    return out


def load_game_samples(cur):
    """Return (games, fbs, hfa_ratio): per-game drive aggregates for every
    FBS-vs-FBS completed game, the FBS team set, and the measured
    home-field points-per-drive ratio."""
    fcs_in = "','".join(FCS_CONFS)
    cur.execute(f"SELECT name FROM teams WHERE conference NOT IN ('{fcs_in}')")
    fbs = {r[0] for r in cur.fetchall()}

    cur.execute('''
        SELECT g.id, g.week, g.season_type, g.home_team, g.away_team, g.notes, s.summary_gz
        FROM games g JOIN game_summaries s ON s.game_id = g.id
        WHERE g.completed = 1 AND g.season = %s
        ORDER BY g.week, g.id
    ''', (SEASON,))

    games = []           # {home, away, neutral, order, h_pts, h_drv, a_pts, a_drv}
    home_pts = home_drv = away_pts = away_drv = 0
    skipped_non_fbs = skipped_no_drives = 0

    for gid, week, stype, home, away, notes, gz in cur.fetchall():
        if home not in fbs or away not in fbs:
            skipped_non_fbs += 1
            continue
        summary = json.loads(gzip.decompress(gz))
        rows = parse_game_drives(summary, home, away)
        if not rows:
            skipped_no_drives += 1
            continue
        is_post = 'postseason' in (stype or '').lower()
        neutral = bool(notes) or is_post
        g = {'home': home, 'away': away, 'neutral': neutral,
             'order': (1 if is_post else 0, week or 0),
             'h_pts': 0, 'h_drv': 0, 'a_pts': 0, 'a_drv': 0}
        for offense, _defense, pts, _period in rows:
            if offense == home:
                g['h_pts'] += pts; g['h_drv'] += 1
            else:
                g['a_pts'] += pts; g['a_drv'] += 1
        if g['h_drv'] == 0 or g['a_drv'] == 0:
            skipped_no_drives += 1
            continue
        games.append(g)
        if not neutral:
            home_pts += g['h_pts']; home_drv += g['h_drv']
            away_pts += g['a_pts']; away_drv += g['a_drv']

    hfa_ratio = (home_pts / home_drv) / (away_pts / away_drv) if home_drv and away_drv else 1.0
    print(f"games used: {len(games)}  (skipped: {skipped_non_fbs} non-FBS opponent, "
          f"{skipped_no_drives} without drive data)")
    print(f"home-field PPD ratio: {hfa_ratio:.4f}")
    return games, fbs, hfa_ratio


def load_game_samples_cfbd(cur, season):
    """Same output as load_game_samples, sourced from CFBD's drives feed —
    used for historical seasons, which have no stored ESPN summaries. The
    exclusion rules mirror parse_game_drives: no OT, no zero-play
    administrative drives, no kneel-outs, no garbage time (same margins)."""
    import cfbd
    fcs_in = "','".join(FCS_CONFS)
    cur.execute(f"SELECT name FROM teams WHERE conference NOT IN ('{fcs_in}')")
    fbs = {r[0] for r in cur.fetchall()}

    cur.execute('''
        SELECT id, week, season_type, home_team, away_team, notes
        FROM games WHERE completed = 1 AND season = %s
    ''', (season,))
    meta = {r[0]: r[1:] for r in cur.fetchall()}

    cfg = cfbd.Configuration(access_token=os.getenv('CFBD_API_KEY'))
    with cfbd.ApiClient(cfg) as api:
        drives = cfbd.DrivesApi(api).get_drives(year=season)
    by_game = {}
    for d in drives:
        by_game.setdefault(d.game_id, []).append(d)

    games = []
    home_pts = home_drv = away_pts = away_drv = 0
    skipped_non_fbs = skipped_no_drives = 0
    for gid, (week, stype, home, away, notes) in meta.items():
        if home not in fbs or away not in fbs:
            skipped_non_fbs += 1
            continue
        ds = by_game.get(gid)
        if not ds:
            skipped_no_drives += 1
            continue
        is_post = 'postseason' in (stype or '').lower()
        neutral = bool(notes) or is_post
        g = {'home': home, 'away': away, 'neutral': neutral,
             'order': (1 if is_post else 0, week or 0),
             'h_pts': 0, 'h_drv': 0, 'a_pts': 0, 'a_drv': 0}
        for d in ds:
            period = d.start_period or 1
            if period > 4:                                   # overtime
                continue
            n_plays = d.plays or 0
            if n_plays == 0:                                 # administrative
                continue
            result = (str(d.drive_result) or '').lower()
            if 'end of' in result and n_plays <= 3 and (d.yards or 0) <= 5:
                continue                                     # kneel-out
            limit = GARBAGE_MARGIN.get(period)
            margin = abs((d.start_offense_score or 0) - (d.start_defense_score or 0))
            if limit is not None and margin > limit:
                continue                                     # garbage time
            pts = max(0, min(8, (d.end_offense_score or 0) - (d.start_offense_score or 0)))
            if d.offense == home:
                g['h_pts'] += pts; g['h_drv'] += 1
            elif d.offense == away:
                g['a_pts'] += pts; g['a_drv'] += 1
        if g['h_drv'] == 0 or g['a_drv'] == 0:
            skipped_no_drives += 1
            continue
        games.append(g)
        if not neutral:
            home_pts += g['h_pts']; home_drv += g['h_drv']
            away_pts += g['a_pts']; away_drv += g['a_drv']

    hfa_ratio = (home_pts / home_drv) / (away_pts / away_drv) if home_drv and away_drv else 1.0
    print(f"games used: {len(games)}  (skipped: {skipped_non_fbs} non-FBS opponent, "
          f"{skipped_no_drives} without drive data)")
    print(f"home-field PPD ratio: {hfa_ratio:.4f}")
    return games, fbs, hfa_ratio


def compute_ratings(games, hfa_ratio):
    """Iterative opponent adjustment. Returns {team: dict} of ratings."""
    hfa = hfa_ratio ** 0.5

    # Per-team chronological game list of (opponent, raw_off_ppd, raw_def_ppd,
    # own_drives, opp_drives) with home-field neutralization applied.
    team_games = {}
    for g in games:
        h_ppd = g['h_pts'] / g['h_drv']
        a_ppd = g['a_pts'] / g['a_drv']
        if not g['neutral']:
            h_ppd /= hfa      # home offense benefits from HFA — remove it
            a_ppd *= hfa      # away offense is suppressed by it — restore it
        team_games.setdefault(g['home'], []).append(
            (g['order'], g['away'], h_ppd, a_ppd, g['h_drv'], g['a_drv']))
        team_games.setdefault(g['away'], []).append(
            (g['order'], g['home'], a_ppd, h_ppd, g['a_drv'], g['h_drv']))

    for t in team_games:
        team_games[t].sort(key=lambda r: r[0])

    total_pts = sum(g['h_pts'] + g['a_pts'] for g in games)
    total_drv = sum(g['h_drv'] + g['a_drv'] for g in games)
    natl = total_pts / total_drv
    print(f"national average: {natl:.4f} points per drive "
          f"({total_drv} countable drives)")

    teams = list(team_games)
    adj_o = {t: natl for t in teams}
    adj_d = {t: natl for t in teams}

    for it in range(MAX_ITERATIONS):
        max_delta = 0.0
        new_o, new_d = {}, {}
        for t in teams:
            gs = team_games[t]
            n = len(gs)
            o_num = o_den = d_num = d_den = 0.0
            for i, (_ord, opp, off_ppd, def_ppd, own_drv, opp_drv) in enumerate(gs):
                w = 1 + (RECENCY_MAX * i / (n - 1) if n > 1 else 0)
                # KenPom core: raw × national average ÷ opponent's adjusted counterpart
                go = off_ppd * natl / adj_d[opp] if adj_d[opp] > 0 else off_ppd
                gd = def_ppd * natl / adj_o[opp] if adj_o[opp] > 0 else def_ppd
                o_num += w * own_drv * go; o_den += w * own_drv
                d_num += w * opp_drv * gd; d_den += w * opp_drv
            # Bayesian prior toward the national average
            new_o[t] = (o_num + PRIOR_DRIVES * natl) / (o_den + PRIOR_DRIVES)
            new_d[t] = (d_num + PRIOR_DRIVES * natl) / (d_den + PRIOR_DRIVES)
            max_delta = max(max_delta, abs(new_o[t] - adj_o[t]), abs(new_d[t] - adj_d[t]))
        adj_o, adj_d = new_o, new_d
        if max_delta < CONVERGENCE:
            print(f"converged after {it + 1} iterations (Δ={max_delta:.2e})")
            break

    out = {}
    for t in teams:
        gs = team_games[t]
        own_drv = sum(r[4] for r in gs)
        opp_drv = sum(r[5] for r in gs)
        raw_o = sum(r[2] * r[4] for r in gs) / own_drv
        raw_d = sum(r[3] * r[5] for r in gs) / opp_drv
        out[t] = {
            'games': len(gs), 'drives_off': own_drv, 'drives_def': opp_drv,
            'raw_off': round(raw_o * SCALE, 2), 'raw_def': round(raw_d * SCALE, 2),
            'off_rating': round(adj_o[t] * SCALE, 2),
            'def_rating': round(adj_d[t] * SCALE, 2),
            'net_rating': round((adj_o[t] - adj_d[t]) * SCALE, 2),
        }
    # Strength of schedule: drive-weighted average opponent net rating
    for t in teams:
        gs = team_games[t]
        tot = sum(r[4] + r[5] for r in gs)
        out[t]['sos'] = round(sum(
            (out[opp]['net_rating']) * (own + oppd) / tot
            for _o, opp, _x, _y, own, oppd in gs), 2)

    # Rankings — offense high=good, defense low=good, net high=good
    for key, rank_key, reverse in (('off_rating', 'off_ranking', True),
                                   ('def_rating', 'def_ranking', False),
                                   ('net_rating', 'net_ranking', True)):
        for i, t in enumerate(sorted(teams, key=lambda x: out[x][key], reverse=reverse)):
            out[t][rank_key] = i + 1
    return out


def validate(cur, ratings):
    """Smell test: how do the Net Rating top 25 line up with the AP poll?"""
    cur.execute('SELECT team, rank FROM ap_rankings WHERE season = %s ORDER BY rank', (SEASON,))
    ap = dict(cur.fetchall())
    top = sorted(ratings, key=lambda t: ratings[t]['net_ranking'])[:25]
    print(f"\n{'NET':>4} {'TEAM':<22} {'NET RTG':>8} {'OFF':>6} {'DEF':>6} {'SOS':>6}  AP")
    for t in top:
        r = ratings[t]
        print(f"{r['net_ranking']:>4} {t:<22} {r['net_rating']:>8} {r['off_rating']:>6} "
              f"{r['def_rating']:>6} {r['sos']:>6}  {ap.get(t, '—')}")
    overlap = sum(1 for t in top if t in ap)
    print(f"\nAP top-25 overlap in Net top 25: {overlap}/25")
    return overlap


def write_table(cur, ratings):
    # Multi-season table (PK (team, season)) — refresh only this season's rows.
    cur.execute('''
        CREATE TABLE IF NOT EXISTS savant_ratings (
            team          TEXT,
            season        INTEGER,
            games         INTEGER,
            drives_off    INTEGER,
            drives_def    INTEGER,
            raw_off       REAL,
            raw_def       REAL,
            off_rating    REAL,
            off_ranking   INTEGER,
            def_rating    REAL,
            def_ranking   INTEGER,
            net_rating    REAL,
            net_ranking   INTEGER,
            sos           REAL,
            updated_at    TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (team, season)
        )
    ''')
    cur.execute('DELETE FROM savant_ratings WHERE season = %s', (SEASON,))
    for t, r in ratings.items():
        cur.execute('''
            INSERT INTO savant_ratings
                (team, season, games, drives_off, drives_def, raw_off, raw_def,
                 off_rating, off_ranking, def_rating, def_ranking,
                 net_rating, net_ranking, sos)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ''', (t, SEASON, r['games'], r['drives_off'], r['drives_def'],
              r['raw_off'], r['raw_def'],
              r['off_rating'], r['off_ranking'],
              r['def_rating'], r['def_ranking'],
              r['net_rating'], r['net_ranking'], r['sos']))
    print(f"\nwrote {len(ratings)} rows to savant_ratings")


def main():
    write = '--write' in sys.argv
    conn = psycopg2.connect(dsn=os.getenv('DATABASE_URL'))
    try:
        cur = conn.cursor()
        print(f"season: {SEASON}")
        if SEASON == current_cfb_season():
            games, _fbs, hfa_ratio = load_game_samples(cur)   # stored ESPN summaries
        else:
            games, _fbs, hfa_ratio = load_game_samples_cfbd(cur, SEASON)
        ratings = compute_ratings(games, hfa_ratio)
        validate(cur, ratings)
        if write:
            write_table(cur, ratings)
            conn.commit()
        else:
            print("\n(dry run — pass --write to persist)")
    finally:
        conn.close()


if __name__ == '__main__':
    main()


# Data changed — tell the live site to drop its in-memory page cache so the
# update is visible immediately instead of after the cache TTL.
try:
    from cache_notify import notify_cache_clear
    notify_cache_clear()
except Exception:
    pass
