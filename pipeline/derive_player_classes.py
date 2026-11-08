"""Derive every player's class (Fr/So/Jr/Sr + redshirt) for every roster season.

No feed publishes a class we can take as-is. CFBD's roster `year` is the
placeholder '1' for all of 2026 (15,804 of 15,806 rows), and on historical rows
it is frozen at the player's last-seen class (a 2025 junior reads "Junior" on
his 2023 row too). ESPN mixes years-in-school and eligibility class and covers
~60% of rosters. So the class is an estimate built from what we can trust:

  anchor   the player's latest roster row from a feed season whose classes look
           real. A season where nearly everyone is '1' is a placeholder feed
           and is skipped, so a broken feed can never reset the site again.
  presence seasons on an FBS roster or with a stat line
  games    games with recorded action per season (player_game_logs)

Rules (eligibility class, the "Redshirt Sophomore" convention):
  • Back from the anchor: years in the program beyond the anchor class are
    years the class held. 2020 holds first (the COVID year — no redshirt
    label), then the season(s) with the fewest recorded games, earliest first.
  • Forward from the anchor: +1 class per season, except a freshman with ≤ 4
    recorded games (the NCAA redshirt rule) holds and is flagged redshirt.
    Linemen and long snappers have no box score, so they always advance.
  • Senior is the cap; a fifth year is a Redshirt Senior.
  • No anchor at all: a freshman in his first season on a roster.
The redshirt flag for a season = a non-COVID hold before that season.

Writes rosters.class_year + rosters.redshirt for every season, and players.year
+ players.redshirt from each player's latest roster season. The raw feed value
lives in rosters.feed_class and is all this reads, so re-running is idempotent.
Runs in the weekly chain right after the roster fetch.

Usage:  python3 pipeline/derive_player_classes.py [--dry-run | --check]
"""

import os as _os, sys as _sys
ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if ROOT not in _sys.path:
    _sys.path.insert(0, ROOT)
import sys
from collections import Counter, defaultdict

COVID = 2020
REDSHIRT_MAX_GAMES = 4
NO_BOX_SCORE = {'OL', 'OT', 'G', 'C', 'LS', '?', None}
# A real roster is ~30% freshmen; CFBD's unpublished-class placeholder is ~100%.
PLACEHOLDER_FRESHMAN_SHARE = 0.6


def derive(presence, anchor, games, box_score=True):
    """Class per season for one player.

    presence  seasons the player was on a roster or recorded a stat line
    anchor    (season, class) from a trusted feed, or None
    games     {season: games with recorded action}
    Returns {season: (class 1-4, redshirt bool)} for every calendar season from
    the first presence to the last.
    """
    first, last = min(presence), max(presence)
    a_season, a_cls = anchor or (first, 1)
    a_cls = max(1, min(int(a_cls), 4))

    # Back from the anchor: the anchor class tells us how many years held.
    before = range(first, a_season)
    n_holds = max(0, len(before) + 1 - a_cls)
    rank = lambda y: (y != COVID, games.get(y, 0) if box_score else 0, y)
    holds = set(sorted(before, key=rank)[:n_holds])

    out, cls = {}, a_cls
    for y in reversed(before):
        if y not in holds:
            cls -= 1
        out[y] = cls
    out[a_season] = a_cls

    # Forward from the anchor: no evidence, so only the freshman redshirt rule.
    cls, rs = a_cls, any(h != COVID for h in holds)
    for y in range(a_season, last):
        if (box_score and cls == 1 and not rs and y in presence
                and games.get(y, 0) <= REDSHIRT_MAX_GAMES):
            holds.add(y)
            rs = True
        else:
            cls += 1
        out[y + 1] = cls

    result = {}
    for y, c in out.items():
        red = any(h < y and h != COVID for h in holds)
        result[y] = (4, True) if c > 4 else (c, red)
    return result


def check():
    # CJ Carr: on the 2024 roster with no action, CFBD's 2025 class is 1, then
    # the 2025 starter -> 2026 Redshirt Sophomore (CFBD's 2026 feed says '1').
    r = derive({2024, 2025, 2026}, (2025, 1), {2025: 12, 2026: 9})
    assert r == {2024: (1, False), 2025: (1, True), 2026: (2, True)}, r
    # True freshman who played all year -> sophomore, no redshirt.
    assert derive({2025, 2026}, (2025, 1), {2025: 12})[2026] == (2, False)
    # True freshman with 3 recorded games keeps his redshirt.
    assert derive({2025, 2026}, (2025, 1), {2025: 3})[2026] == (1, True)
    # A freshman lineman has no box score: never inferred as a redshirt.
    assert derive({2025, 2026}, (2025, 1), {}, box_score=False)[2026] == (2, False)
    # 2025 senior back for 2026 is a fifth year -> Redshirt Senior, not "5".
    assert derive({2022, 2023, 2024, 2025, 2026}, (2025, 4), {})[2026] == (4, True)
    # Historical rows: a 2025 junior was a freshman in 2023, not a junior.
    r = derive({2023, 2024, 2025}, (2025, 3), {2023: 10, 2024: 12, 2025: 12})
    assert [r[y][0] for y in (2023, 2024, 2025)] == [1, 2, 3], r
    # COVID year holds without a redshirt label.
    r = derive({2018, 2019, 2020, 2021, 2022}, (2022, 4), {y: 10 for y in range(2018, 2023)})
    assert r[2020] == (3, False) and r[2021] == (3, False) and r[2022] == (4, False), r
    # A JUCO/FCS arrival listed as a junior in his first FBS year stays a junior.
    assert derive({2025, 2026}, (2025, 3), {2025: 12})[2026] == (4, False)
    # No trusted class anywhere: first roster season is the freshman year.
    assert derive({2026}, None, {}) == {2026: (1, False)}
    print('derive_player_classes: all checks pass')


def _connect():
    import psycopg2
    # Keepalives: a dropped connection otherwise hangs a read for 10+ minutes.
    return psycopg2.connect(_os.getenv('DATABASE_URL'), keepalives=1,
                            keepalives_idle=30, keepalives_interval=10, keepalives_count=3)


def _ensure_schema():
    """Own short transaction: ALTER takes an exclusive lock even with IF NOT
    EXISTS, which deadlocks against the live site's reads, so it only runs for
    a column that is actually missing."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SET lock_timeout = '15s'")
    cur.execute("SELECT table_name || '.' || column_name FROM information_schema.columns "
                "WHERE table_name IN ('rosters', 'players')")
    have = {r[0] for r in cur.fetchall()}
    if 'rosters.feed_class' not in have:
        # Until now class_year WAS the raw feed value, so it seeds feed_class
        # exactly once; from here on only the roster fetches write feed_class.
        cur.execute('ALTER TABLE rosters ADD COLUMN feed_class TEXT')
        cur.execute('UPDATE rosters SET feed_class = class_year')
        print('rosters.feed_class added and seeded from class_year', flush=True)
    if 'rosters.redshirt' not in have:
        cur.execute('ALTER TABLE rosters ADD COLUMN redshirt INTEGER DEFAULT 0')
    if 'players.redshirt' not in have:
        cur.execute('ALTER TABLE players ADD COLUMN redshirt INTEGER DEFAULT 0')
    conn.commit()
    conn.close()


def _load():
    """Every read, on one fresh connection. Returns (trusted, roster, presence, games)."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT season,
               AVG((feed_class = '1')::int) FILTER (WHERE feed_class ~ '^[1-5]$')
        FROM rosters GROUP BY season ORDER BY season""")
    shares = cur.fetchall()
    trusted = {s for s, share in shares if share is not None and share < PLACEHOLDER_FRESHMAN_SHARE}
    print('feed seasons skipped as placeholder:',
          sorted(s for s, _ in shares if s not in trusted) or 'none', flush=True)

    cur.execute('SELECT player_id, season, feed_class, position FROM rosters')
    roster = defaultdict(dict)   # pid -> {season: (feed, position)}
    for pid, season, feed, pos in cur.fetchall():
        roster[pid][season] = (feed, pos)

    presence = defaultdict(set)
    cur.execute("SELECT DISTINCT player_id, season::int FROM player_stats "
                "WHERE player_id ~ '^[0-9]+$'")
    for pid, season in cur.fetchall():
        presence[int(pid)].add(season)

    games = defaultdict(dict)
    # One "game_id" key per game entry. Counting it as text is ~2 s a season;
    # json_array_length(log::json) is ~2 minutes a season.
    cur.execute('''SELECT player_id, season,
                          (length(log) - length(replace(log, '"game_id"', ''))) / 9
                   FROM player_game_logs WHERE log IS NOT NULL''')
    for pid, season, n in cur.fetchall():
        games[pid][season] = n
    conn.close()
    # A stat line with no game log still means he played; count it as a full year.
    for pid, seasons in presence.items():
        for s in seasons:
            games[pid].setdefault(s, 99)
    return trusted, roster, presence, games


def main(dry_run=False):
    import psycopg2
    from psycopg2.extras import execute_values
    from dotenv import load_dotenv
    load_dotenv(_os.path.join(ROOT, '.env'), override=True)

    _ensure_schema()
    # The Render DB has dropped this connection mid-read ("server closed the
    # connection unexpectedly") on runs where the identical read succeeded
    # minutes later, so the read phase retries on a fresh connection.
    for attempt in range(1, 4):
        try:
            trusted, roster, presence, games = _load()
            break
        except psycopg2.OperationalError as e:
            if attempt == 3:
                raise
            print(f'read failed on attempt {attempt} ({str(e).strip()[:60]}), retrying', flush=True)

    rows, sources = [], Counter()
    for pid, seasons in roster.items():
        anchors = [(s, int(f)) for s, (f, _) in seasons.items()
                   if s in trusted and f and f.isdigit() and 1 <= int(f) <= 5]
        anchor = max(anchors) if anchors else None
        sources['anchored' if anchor else 'no trusted class'] += 1
        pos = seasons[max(seasons)][1]
        result = derive(presence[pid] | set(seasons), anchor, games[pid],
                        box_score=pos not in NO_BOX_SCORE)
        for s in seasons:
            cls, rs = result[s]
            rows.append((pid, s, str(cls), int(rs)))

    print(f'{len(rows):,} roster rows across {len(roster):,} players '
          f'({sources["anchored"]:,} anchored, {sources["no trusted class"]:,} without a trusted class)',
          flush=True)
    cur_season = max(r[1] for r in rows)
    dist = Counter((r[2], r[3]) for r in rows if r[1] == cur_season)
    print(f'{cur_season} class/redshirt distribution:', sorted(dist.items()), flush=True)
    if dry_run:
        return rows

    conn = _connect()
    cur = conn.cursor()
    cur.execute("SET lock_timeout = '15s'")
    cur.execute('CREATE TEMP TABLE derived (pid INT, season INT, cls TEXT, rs INT) ON COMMIT DROP')
    execute_values(cur, 'INSERT INTO derived VALUES %s', rows, page_size=5000)
    cur.execute('''
        UPDATE rosters r SET class_year = d.cls, redshirt = d.rs
        FROM derived d
        WHERE r.player_id = d.pid AND r.season = d.season
          AND (r.class_year IS DISTINCT FROM d.cls OR r.redshirt IS DISTINCT FROM d.rs)''')
    changed_rosters = cur.rowcount
    # players.year/redshirt = the player's latest roster season.
    cur.execute('''
        UPDATE players p SET year = d.cls, redshirt = d.rs
        FROM (SELECT DISTINCT ON (pid) pid, cls, rs FROM derived
              ORDER BY pid, season DESC) d
        WHERE p.id = d.pid
          AND (p.year IS DISTINCT FROM d.cls OR p.redshirt IS DISTINCT FROM d.rs)''')
    changed_players = cur.rowcount
    conn.commit()
    conn.close()
    print(f'updated {changed_rosters:,} roster rows, {changed_players:,} players', flush=True)

    # Data changed — drop the live site's page cache so it shows immediately.
    try:
        from lib.cache_notify import notify_cache_clear
        notify_cache_clear()
    except Exception:
        pass
    return rows


if __name__ == '__main__':
    if '--check' in sys.argv:
        check()
    else:
        check()
        main(dry_run='--dry-run' in sys.argv)
