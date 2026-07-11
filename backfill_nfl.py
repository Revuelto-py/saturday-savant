"""Backfill NFL outcomes (draft picks + undrafted free-agent signings) for
historical players (careers ending 2016-2024).

Stage `draft` — CFBD draft picks for the 2017-2026 drafts, matched EXACTLY by
college_athlete_id (CFBD uses the same ESPN athlete ids as our players table),
not by name like the current-class script. Sets nfl_status='drafted',
draft_year/round/pick, and the NFL team resolved through ESPN's team list by
id (CFBD's nfl_team is just a city — ambiguous for 4 of 32 franchises).

Stage `udfa` — free-agent signings have no queryable feed for past years, but
ESPN athlete ids are stable across college -> NFL: an undrafted player with an
NFL athlete page signed as a free agent. Checks the plausible-NFL pool
(final-season usage >= 3% of team plays, or >= 25 tackles for defenders —
13.3k players) against ESPN's NFL athlete endpoint. Resumable: players whose
nfl_status is already set are skipped.

Run:  python3 backfill_nfl.py            # both stages
      python3 backfill_nfl.py draft      # one stage
"""
import os
import sys
import time
import cfbd
import psycopg2
from psycopg2.extras import execute_values
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=True)

def _connect():
    return psycopg2.connect(os.getenv('DATABASE_URL'))

# Render's Postgres drops idle connections while slow upstream fetches run, so
# every stage opens fresh connections per unit of work instead of sharing one.
conn = _connect()
cursor = conn.cursor()

def _fresh():
    """Replace the module connection after a drop (or proactively per year)."""
    global conn, cursor
    try:
        conn.close()
    except Exception:
        pass
    conn = _connect()
    cursor = conn.cursor()


def espn_nfl_teams():
    """id -> {name, logo}, same mapping fetch_nfl_status.py uses."""
    r = requests.get('https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams',
                     params={'limit': 40}, timeout=15)
    out = {}
    for t in r.json()['sports'][0]['leagues'][0]['teams']:
        team = t['team']
        out[int(team['id'])] = {
            'name': team['displayName'],
            'logo': next((l['href'] for l in team.get('logos', [])), ''),
        }
    return out


def stage_draft():
    nfl_teams = espn_nfl_teams()
    cfg = cfbd.Configuration(access_token=os.getenv('CFBD_API_KEY'))
    total = 0
    with cfbd.ApiClient(cfg) as api:
        draft_api = cfbd.DraftApi(api)
        for yr in range(2017, 2027):
            try:
                picks = draft_api.get_draft_picks(year=yr)
            except Exception as e:
                print(f"draft {yr}: {type(e).__name__} {str(e)[:80]}", flush=True)
                continue
            rows = []
            for p in picks:
                cid = getattr(p, 'college_athlete_id', None)
                if not cid:
                    continue
                team_info = nfl_teams.get(getattr(p, 'nfl_team_id', None), {})
                rows.append((int(cid), team_info.get('name') or p.nfl_team or '',
                             team_info.get('logo', ''), yr, p.round, p.pick))
            # One batched statement per draft year, retried on a fresh
            # connection if the WAN link dropped during the CFBD fetch.
            matched = 0
            for attempt in (1, 2):
                try:
                    _fresh()
                    execute_values(cursor, '''
                        UPDATE players AS pl SET
                            nfl_status = 'drafted',
                            nfl_team = v.team, nfl_team_logo = v.logo,
                            draft_year = v.yr, draft_round = v.rnd, draft_pick = v.pk
                        FROM (VALUES %s) AS v(id, team, logo, yr, rnd, pk)
                        WHERE pl.id = v.id
                    ''', rows, page_size=300)
                    matched = cursor.rowcount
                    conn.commit()
                    break
                except psycopg2.OperationalError as e:
                    print(f"  draft {yr} attempt {attempt}: {str(e)[:60]}", flush=True)
            total += matched
            print(f"draft {yr}: {len(picks)} picks, {matched} matched by id", flush=True)
    print(f"draft stage done: {total} players marked drafted", flush=True)


def stage_udfa():
    # Plausible-NFL pool: departed by 2024, real final-season role, and no
    # NFL outcome recorded yet (drafted players were just handled above).
    cursor.execute('''
        WITH last AS (
            SELECT player_id::int AS pid, MAX(season) AS final FROM player_stats
            WHERE player_id ~ '^[0-9]+$' GROUP BY player_id
        )
        SELECT last.pid FROM last
        JOIN players p ON p.id = last.pid
        WHERE last.final <= 2024
          AND (p.nfl_status IS NULL OR p.nfl_status = '' OR p.nfl_status = 'graduated')
          AND (
            EXISTS (SELECT 1 FROM player_usage u
                    WHERE u.player_id = last.pid AND u.season = last.final AND u.overall >= 0.03)
            OR EXISTS (SELECT 1 FROM player_stats d
                       WHERE d.player_id = last.pid::text AND d.season = last.final
                         AND d.category='defensive' AND d.stat_type='TOT'
                         AND CAST(d.stat AS REAL) >= 25)
          )
        ORDER BY last.pid
    ''')
    candidates = [r[0] for r in cursor.fetchall()]
    print(f"udfa stage: {len(candidates)} candidates to check against ESPN", flush=True)

    session = requests.Session()

    def check(pid):
        """An NFL athlete page under the same id means the player signed."""
        try:
            r = session.get(
                f'https://site.api.espn.com/apis/common/v3/sports/football/nfl/athletes/{pid}',
                timeout=8)
            if r.status_code != 200:
                return pid, None
            a = r.json().get('athlete') or {}
            if not a.get('id'):
                return pid, None
            team = a.get('team') or {}
            logo = ''
            for l in (team.get('logos') or []):
                if 'dark' not in (l.get('rel') or []):
                    logo = l.get('href', '')
                    break
            return pid, {'team': team.get('displayName', ''), 'logo': logo}
        except Exception:
            return pid, None

    # Scan ESPN first (network-bound, no DB held open), then write all hits
    # in one batched statement on a fresh connection — the long scan would
    # otherwise outlive Render's idle-connection window.
    hits = []
    checked = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(check, pid): pid for pid in candidates}
        for fut in as_completed(futures):
            pid, hit = fut.result()
            checked += 1
            if hit is not None:
                hits.append((pid, hit['team'], hit['logo']))
            if checked % 1000 == 0:
                print(f"  {checked}/{len(candidates)} checked, {len(hits)} signings found", flush=True)

    signed = 0
    for attempt in (1, 2):
        try:
            _fresh()
            execute_values(cursor, '''
                UPDATE players AS pl SET nfl_status='udfa', nfl_team=v.team, nfl_team_logo=v.logo
                FROM (VALUES %s) AS v(id, team, logo)
                WHERE pl.id = v.id
                  AND (pl.nfl_status IS NULL OR pl.nfl_status='' OR pl.nfl_status='graduated')
            ''', hits, page_size=500)
            signed = cursor.rowcount
            conn.commit()
            break
        except psycopg2.OperationalError as e:
            print(f"  udfa write attempt {attempt}: {str(e)[:60]}", flush=True)
    print(f"udfa stage done: {signed} free-agent signings recorded "
          f"(of {len(hits)} ESPN hits)", flush=True)


if __name__ == '__main__':
    stages = sys.argv[1:] or ['draft', 'udfa']
    for s in stages:
        {'draft': stage_draft, 'udfa': stage_udfa}[s]()
    conn.close()
