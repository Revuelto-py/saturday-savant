"""Store CFP seeds per season (cfp_seeds) from the selection-day committee
ranking — the final AP poll can't seed a bracket because it re-ranks teams
after the playoff has been played.

    2016-2023 (4-team):  seeds 1-4 = committee top four, straight.
    2024 (12-team, champion-bye rule): the five highest-ranked conference
        champions qualify automatically; the four highest-ranked champions
        are seeds 1-4 in rank order; the remaining eight spots (seeds 5-12)
        go to the best-ranked teams left. Conference champions are detected
        from our games table (conference championship game winners).
    2025+ (12-team, straight seeding): five champions still auto-qualify,
        but seeds 1-12 follow the committee ranking directly.

One CFBD call per season. Rerunnable (per-season upsert).
"""
import os
import re
import sys
import cfbd
import psycopg2
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=True)

CCG_RE = re.compile(r'(SEC|Big Ten|Big 12|ACC|Pac-12|Pac 12|Mountain West|American|'
                    r'Sun Belt|MAC|Mid-American|Conference USA|C-USA)[^a-z]*Championship', re.I)

conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cur = conn.cursor()
cur.execute('''
    CREATE TABLE IF NOT EXISTS cfp_seeds (
        season INTEGER NOT NULL,
        seed   INTEGER NOT NULL,
        team   TEXT NOT NULL,
        PRIMARY KEY (season, seed)
    )
''')
conn.commit()


def conference_champs(season):
    cur.execute('''
        SELECT home_team, away_team, home_points, away_points, notes FROM games
        WHERE season = %s AND completed = 1 AND notes IS NOT NULL
    ''', (season,))
    champs = set()
    for home, away, hp, ap, notes in cur.fetchall():
        if not CCG_RE.search(notes or ''):
            continue
        if hp is None or ap is None:
            continue
        champs.add(home if hp > ap else away)
    return champs


def seeds_for(season, ranks):
    """ranks: {team: committee_rank}. Returns {team: seed}."""
    ordered = [t for t, _ in sorted(ranks.items(), key=lambda kv: kv[1])]
    if season <= 2023:
        return {t: i + 1 for i, t in enumerate(ordered[:4])}

    champs = conference_champs(season)
    ranked_champs = [t for t in ordered if t in champs]
    auto = ranked_champs[:5]                       # five best champions are in
    field = list(auto)
    for t in ordered:                              # fill to 12 by ranking
        if len(field) >= 12:
            break
        if t not in field:
            field.append(t)
    field = field[:12]

    if season == 2024:
        byes = ranked_champs[:4]                   # champion-bye rule
        rest = [t for t in sorted(field, key=lambda t: ranks.get(t, 99)) if t not in byes]
        order = byes + rest
    else:
        order = sorted(field, key=lambda t: ranks.get(t, 99))   # straight seeding
    return {t: i + 1 for i, t in enumerate(order)}


def main():
    seasons = [int(a) for a in sys.argv[1:]] or list(range(2016, 2026))
    cfg = cfbd.Configuration(access_token=os.getenv('CFBD_API_KEY'))
    with cfbd.ApiClient(cfg) as api:
        rk_api = cfbd.RankingsApi(api)
        for y in seasons:
            weeks = rk_api.get_rankings(year=y)
            committee = [(wk.week, poll) for wk in weeks for poll in wk.polls
                         if poll.poll == 'Playoff Committee Rankings'
                         and 'REGULAR' in str(wk.season_type).upper()]
            if not committee:
                print(f"{y}: no committee poll — skipped")
                continue
            _, final_poll = max(committee, key=lambda x: x[0])
            ranks = {r.school: r.rank for r in final_poll.ranks}
            seeds = seeds_for(y, ranks)
            cur.execute('DELETE FROM cfp_seeds WHERE season = %s', (y,))
            for team, seed in seeds.items():
                cur.execute('INSERT INTO cfp_seeds (season, seed, team) VALUES (%s,%s,%s)',
                            (y, seed, team))
            conn.commit()
            shown = sorted(seeds.items(), key=lambda kv: kv[1])
            print(f"{y}: {', '.join(f'{s}.{t}' for t, s in shown)}")
    conn.close()


if __name__ == '__main__':
    main()
