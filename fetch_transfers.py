"""Ingest transfers: CFBD portal (2021+) plus a roster-derived pre-portal era.

CFBD's /player/portal endpoint has data from 2021 onward only (the portal
itself launched in Oct 2018), so the pre-2021 era is derived from our own
FBS rosters: a player on team A in season Y-1 and team B in season Y is a
transfer of class Y. Derived rows carry source='roster' and have no
rating/stars/date/eligibility — the page labels the era accordingly.
The 2016 class needs the 2015 roster, which predates our rosters table;
it is fetched from CFBD in-memory only (never persisted, so player pages
and season lists are unaffected).

Every row gets a resolved player_id where possible (exact-name match,
disambiguated by the origin team's roster), replacing the page's old
name-join that could duplicate rows for shared names.

Usage:  python3 fetch_transfers.py              # portal 2021-2026 + derive era if missing
        python3 fetch_transfers.py 2026         # specific portal year(s)
        python3 fetch_transfers.py --derive     # force-rebuild the 2016-2020 derived era
"""
import sys
import cfbd
import psycopg2
import os
from psycopg2.extras import execute_values
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'), override=True)

PORTAL_YEARS = list(range(2021, 2027))   # CFBD portal coverage
DERIVED_YEARS = list(range(2016, 2021))  # roster-diff era

configuration = cfbd.Configuration(access_token=os.getenv("CFBD_API_KEY"))
conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cursor = conn.cursor()

# Schema: provenance + resolved identity (idempotent)
cursor.execute("ALTER TABLE transfers ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'portal'")
cursor.execute("ALTER TABLE transfers ADD COLUMN IF NOT EXISTS player_id INTEGER")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_transfers_year ON transfers(year)")
conn.commit()


def next_id():
    cursor.execute('SELECT COALESCE(MAX(id), 0) FROM transfers')
    return cursor.fetchone()[0] + 1


def build_name_maps():
    """players by lowercase name + roster membership for disambiguation."""
    cursor.execute('SELECT lower(first_name), lower(last_name), id FROM players')
    by_name = {}
    for f, l, i in cursor.fetchall():
        by_name.setdefault((f, l), []).append(i)
    cursor.execute('SELECT player_id, team, season FROM rosters')
    on_roster = set(cursor.fetchall())
    return by_name, on_roster


def resolve_pid(by_name, on_roster, first, last, origin, year):
    cands = by_name.get(((first or '').lower(), (last or '').lower()), [])
    if len(cands) == 1:
        return cands[0]
    if len(cands) > 1 and origin:
        narrowed = [i for i in cands
                    if (i, origin, year - 1) in on_roster or (i, origin, year - 2) in on_roster]
        if len(narrowed) == 1:
            return narrowed[0]
    return None


def ingest_portal(players_api, years, by_name, on_roster):
    for yr in years:
        try:
            transfers = players_api.get_transfer_portal(year=yr)
        except Exception as e:
            print(f"{yr}: portal fetch failed — {type(e).__name__}: {str(e)[:100]}")
            continue
        cursor.execute("DELETE FROM transfers WHERE year=%s AND source='portal'", (yr,))
        nid = next_id()
        rows = []
        for t in transfers:
            td = getattr(t, 'transfer_date', None)
            elig = getattr(t, 'eligibility', None)
            pid = resolve_pid(by_name, on_roster, t.first_name, t.last_name, t.origin, yr)
            rows.append((nid, t.first_name, t.last_name, getattr(t, 'position', None),
                         t.origin, getattr(t, 'destination', None),
                         td.isoformat() if td else None,
                         getattr(t, 'rating', None), getattr(t, 'stars', None),
                         elig.value if hasattr(elig, 'value') else (str(elig) if elig else None),
                         yr, 'portal', pid))
            nid += 1
        execute_values(cursor, '''
            INSERT INTO transfers (id, first_name, last_name, position, origin,
                destination, transfer_date, rating, stars, eligibility, year, source, player_id)
            VALUES %s''', rows, page_size=1000)
        conn.commit()
        resolved = sum(1 for r in rows if r[12])
        print(f"{yr}: {len(rows)} portal transfers saved ({resolved} matched to a player page)", flush=True)


def roster_2015():
    """The 2015 FBS roster, in-memory only — enables the 2016 derived class."""
    with cfbd.ApiClient(configuration) as api_client:
        teams_api = cfbd.TeamsApi(api_client)
        fbs = {t.school for t in teams_api.get_fbs_teams(year=2015)}
        out = {}
        for p in teams_api.get_roster(year=2015):
            if p.team not in fbs:
                continue
            try:
                pid = int(p.id)
            except (TypeError, ValueError):
                continue
            if pid > 0:
                out[pid] = p.team
        return out


def derive_pre_portal():
    """2016-2020 transfer classes from year-over-year FBS roster changes."""
    cursor.execute("DELETE FROM transfers WHERE year = ANY(%s)", (DERIVED_YEARS,))
    nid = next_id()
    total = 0
    for yr in DERIVED_YEARS:
        if yr == 2016:
            prev = roster_2015()
            cursor.execute('''
                SELECT r.player_id, p.first_name, p.last_name, r.position, r.team
                FROM rosters r JOIN players p ON p.id = r.player_id
                WHERE r.season = 2016''')
            moved = [(pid, f, l, pos, prev[pid], team)
                     for pid, f, l, pos, team in cursor.fetchall()
                     if pid in prev and prev[pid] != team]
        else:
            cursor.execute('''
                SELECT r2.player_id, p.first_name, p.last_name, r2.position,
                       r1.team, r2.team
                FROM rosters r1
                JOIN rosters r2 ON r2.player_id = r1.player_id
                                AND r2.season = r1.season + 1
                                AND r2.team <> r1.team
                JOIN players p ON p.id = r2.player_id
                WHERE r2.season = %s''', (yr,))
            moved = cursor.fetchall()
        rows = [(nid + n, f, l, pos, origin, dest, None, None, None, None,
                 yr, 'roster', pid)
                for n, (pid, f, l, pos, origin, dest) in enumerate(moved)]
        nid += len(rows)
        total += len(rows)
        execute_values(cursor, '''
            INSERT INTO transfers (id, first_name, last_name, position, origin,
                destination, transfer_date, rating, stars, eligibility, year, source, player_id)
            VALUES %s''', rows, page_size=1000)
        conn.commit()
        print(f"{yr}: {len(rows)} transfers derived from roster changes", flush=True)
    print(f"derived era rebuilt: {total} rows", flush=True)


def update_current_teams():
    """Reflect 2025/2026 portal destinations onto the current players snapshot
    (2026 applied last so it wins)."""
    # Only player_id-resolved rows: an unresolved row means the name was
    # unknown or ambiguous, and a name-based update would guess wrong either way.
    cursor.execute('''
        SELECT player_id, destination FROM transfers
        WHERE year IN (2025, 2026) AND player_id IS NOT NULL
          AND destination IS NOT NULL AND destination != ''
        ORDER BY year ASC''')
    updated = 0
    for pid, dest in cursor.fetchall():
        cursor.execute('SELECT name FROM teams WHERE name=%s', (dest,))
        if not cursor.fetchone():
            continue
        cursor.execute('UPDATE players SET team=%s WHERE id=%s AND team<>%s',
                       (dest, pid, dest))
        updated += cursor.rowcount
    conn.commit()
    print(f"players.team updated for {updated} transfers", flush=True)


def main():
    args = sys.argv[1:]
    force_derive = '--derive' in args
    years = [int(a) for a in args if a.isdigit()] or PORTAL_YEARS

    by_name, on_roster = build_name_maps()
    with cfbd.ApiClient(configuration) as api_client:
        ingest_portal(cfbd.PlayersApi(api_client), years, by_name, on_roster)

    cursor.execute('SELECT COUNT(*) FROM transfers WHERE year = ANY(%s)', (DERIVED_YEARS,))
    if force_derive or cursor.fetchone()[0] == 0:
        derive_pre_portal()

    update_current_teams()
    conn.close()
    print("Done!")


if __name__ == '__main__':
    main()


# Data changed — tell the live site to drop its in-memory page cache so the
# update is visible immediately instead of after the cache TTL.
try:
    from cache_notify import notify_cache_clear
    notify_cache_clear()
except Exception:
    pass
