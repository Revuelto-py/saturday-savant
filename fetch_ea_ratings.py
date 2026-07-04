"""Populate an ea_ratings table with EA Sports College Football 27 player
ratings, used ONLY as an internal signal for lineup/starter determination
(never displayed in the UI).

── Source / terms note ────────────────────────────────────────────────────
This is proprietary, licensed game data (EA's subjective player ratings), NOT
public factual sports statistics like CFBD/ESPN box scores. EA's robots.txt
carries an explicit reservation of rights against "web scraping … or any form
of text or data mining", and the EA User Agreement prohibits extracting data
from EA Services without authorization. This fetch was run with the project
owner's explicit, informed decision to use the data strictly as a private,
non-displayed input to the starter model. Do not surface these ratings in any
public template or API response.

── How the data is obtained ───────────────────────────────────────────────
The ratings page (ea.com/games/ea-sports-college-football/ratings) is a
Next.js app. Its underlying drop-api.ea.com JSON endpoint requires
server-derived params, but the page itself is server-rendered and paginates
via a simple `?page=N` query string (100 players/page, ~91 pages for the full
~9,013). Each response embeds the page's data as JSON in the __NEXT_DATA__
script tag, so we page through and parse that — no headless browser or
brittle DOM scraping needed.

── Matching to our players ────────────────────────────────────────────────
EA rows have no foreign key to our players table, so — like the transfers
importer — we match on normalized name + team. EA team labels are reconciled
to our canonical names via TEAM_ALIASES. Matching is resolved at ingest and
the players.id is stored on the row, so the runtime starter lookup is a plain
id join. Unmatched players and unmapped teams are logged, never guessed.

Standalone structure matching the other fetch_*.py scripts.
"""

import json
import os
import re
import time
import unicodedata
import urllib.request

import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

load_dotenv()

UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120 Safari/537.36')
BASE = 'https://www.ea.com/games/ea-sports-college-football/ratings?page='
NEXT_RX = re.compile(r'__NEXT_DATA__[^>]*>(.*?)</script>', re.S)

# The six attributes we persist (EA stat key -> our column). EA exposes ~50
# attributes; we keep overall plus the headline six shown on the ratings grid.
ATTR_KEYS = {
    'speed': 'speed',
    'strength': 'strength',
    'agility': 'agility',
    'changeOfDirection': 'change_of_direction',
    'injury': 'injury',
    'awareness': 'awareness',
}

# EA team label -> our players.team canonical name. Only the 13 labels that
# don't match ours verbatim need an entry; the other 125 map by identity.
# North Dakota State / Sacramento State are FCS teams EA includes but that
# aren't in our FBS roster — intentionally left unmapped (they'll log as
# unmatched, which is correct).
TEAM_ALIASES = {
    'Appalachian State': 'App State',
    'Cal': 'California',
    'Connecticut': 'UConn',
    'FAU': 'Florida Atlantic',
    'FIU': 'Florida International',
    'Hawaii': "Hawai'i",
    'Miami (Ohio)': 'Miami (OH)',
    'Middle Tennessee State': 'Middle Tennessee',
    'San Jose State': 'San José State',
    'UMass': 'Massachusetts',
    'USF': 'South Florida',
}

_SUFFIXES = {'jr', 'sr', 'ii', 'iii', 'iv', 'v'}


def norm(s):
    """Lowercase, strip accents/punctuation, collapse spaces — for name keys.
    Hyphens/apostrophes become spaces first so compound names like
    'Coleman-Williams' or "O'Brien" tokenize instead of fusing."""
    s = unicodedata.normalize('NFKD', s or '')
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace('-', ' ')          # hyphen splits: Coleman-Williams
    s = s.replace("'", '').replace('’', '')  # apostrophe drops: Ja'Marr -> jamarr
    s = re.sub(r"[^a-z0-9 ]", '', s)
    return re.sub(r'\s+', ' ', s).strip()


def norm_last(last):
    """Normalized last name with a trailing generational suffix removed, so
    'Smith Jr.' and 'Smith' key the same."""
    toks = norm(last).split()
    if len(toks) > 1 and toks[-1] in _SUFFIXES:
        toks = toks[:-1]
    return ' '.join(toks)


def fetch_all():
    """Page through the SSR ratings grid, returning all player dicts.

    If EA_RATINGS_CACHE points to a saved JSON array of the same player
    objects, load that instead of re-hitting EA (used during development to
    avoid repeated requests)."""
    cache = os.getenv('EA_RATINGS_CACHE')
    if cache and os.path.exists(cache):
        items = json.load(open(cache))
        print(f'Loaded {len(items)} players from cache {cache}.')
        return items
    all_items, page, total = [], 1, None
    while True:
        req = urllib.request.Request(BASE + str(page),
                                     headers={'User-Agent': UA, 'Accept': 'text/html'})
        with urllib.request.urlopen(req, timeout=30) as r:
            html = r.read().decode('utf-8', 'ignore')
        m = NEXT_RX.search(html)
        if not m:
            break
        rd = json.loads(m.group(1))['props']['pageProps'].get('ratingDetails', {})
        items = rd.get('items', [])
        if not items:
            break
        all_items.extend(items)
        total = rd.get('totalItems', total)
        if total and len(all_items) >= total:
            break
        page += 1
        time.sleep(0.4)
    print(f'Fetched {len(all_items)} players across {page} page(s) '
          f'(EA reported {total}).')
    return all_items


def build_player_index(cursor):
    """In-memory indexes of our active players for name+team matching."""
    cursor.execute('''
        SELECT id, first_name, last_name, team, position
        FROM players WHERE active_2026 = 1 AND team IS NOT NULL
    ''')
    by_full, by_last, by_tok = {}, {}, {}
    for pid, first, last, team, pos in cursor.fetchall():
        nf, nl = norm(first), norm_last(last)
        by_full.setdefault((nf, nl, team), []).append(pid)
        by_last.setdefault((nl, team, (pos or '').upper()), []).append(pid)
        # Index each token of the last name so a compound EA name
        # ('Coleman-Williams') can still reach our single-token 'Williams'.
        for tok in set(nl.split()):
            by_tok.setdefault((nf, tok, team), []).append(pid)
    return by_full, by_last, by_tok


def resolve_player_id(ea, by_full, by_last, by_tok):
    """Return (player_id, method) or (None, reason) for one EA player."""
    ea_team = ea['team']['label']
    team = TEAM_ALIASES.get(ea_team, ea_team)
    first = norm(ea.get('firstName', ''))
    last = norm_last(ea.get('lastName', ''))
    pos = (ea.get('position') or {}).get('id', '').upper()

    hit = by_full.get((first, last, team))
    if hit and len(hit) == 1:
        return hit[0], 'name+team'
    if hit and len(hit) > 1:
        # Same first+last+team (e.g. twins) — disambiguate by position.
        narrowed = [p for p in by_last.get((last, team, pos), []) if p in hit]
        if len(narrowed) == 1:
            return narrowed[0], 'name+team+pos'
        return None, 'ambiguous_name'

    # Fallback A: first name + team + any shared last-name token, when it
    # resolves to a single player (recovers compound/hyphenated last names).
    tok_ids = set()
    for tok in set(last.split()):
        tok_ids.update(by_tok.get((first, tok, team), []))
    if len(tok_ids) == 1:
        return next(iter(tok_ids)), 'first+lasttoken+team'

    # Fallback B: last name + team + position, when unique (covers EA using a
    # nickname/common first name we don't store).
    lhit = by_last.get((last, team, pos))
    if lhit and len(lhit) == 1:
        return lhit[0], 'last+team+pos'
    return None, ('unmapped_team' if team == ea_team and team not in _known_teams
                  else 'no_name_match')


_known_teams = set()


def main():
    conn = psycopg2.connect(os.getenv('DATABASE_URL'))
    cursor = conn.cursor()
    try:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ea_ratings (
                ea_id              INTEGER PRIMARY KEY,
                first_name         TEXT,
                last_name          TEXT,
                ea_team            TEXT,
                position           TEXT,
                conference         TEXT,
                overall            INTEGER,
                speed              INTEGER,
                strength           INTEGER,
                agility            INTEGER,
                change_of_direction INTEGER,
                injury             INTEGER,
                awareness          INTEGER,
                player_id          INTEGER,
                match_method       TEXT,
                updated_at         TIMESTAMPTZ DEFAULT now()
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_ea_ratings_player_id ON ea_ratings(player_id)')
        conn.commit()

        cursor.execute("SELECT DISTINCT team FROM players WHERE active_2026 = 1 AND team IS NOT NULL")
        _known_teams.update(r[0] for r in cursor.fetchall())

        players = fetch_all()
        if not players:
            print('No players fetched — aborting without touching the table.')
            return

        by_full, by_last, by_tok = build_player_index(cursor)

        rows = []
        matched = 0
        reasons = {}
        unmatched_samples = []
        for ea in players:
            def stat(key):
                s = ea.get('stats', {}).get(key) or {}
                return s.get('value')
            pid, method = resolve_player_id(ea, by_full, by_last, by_tok)
            if pid:
                matched += 1
            else:
                reasons[method] = reasons.get(method, 0) + 1
                if len(unmatched_samples) < 20:
                    unmatched_samples.append(
                        f"{ea.get('firstName')} {ea.get('lastName')} "
                        f"({ea['team']['label']}, {(ea.get('position') or {}).get('id')}) "
                        f"OVR {ea.get('overallRating')} [{method}]")
            rows.append((
                ea['id'], ea.get('firstName'), ea.get('lastName'),
                ea['team']['label'], (ea.get('position') or {}).get('id'),
                (ea.get('conference') or {}).get('label'),
                ea.get('overallRating'),
                stat('speed'), stat('strength'), stat('agility'),
                stat('changeOfDirection'), stat('injury'), stat('awareness'),
                pid, method if pid else None,
            ))

        cursor.execute('TRUNCATE ea_ratings')
        execute_values(cursor, '''
            INSERT INTO ea_ratings (
                ea_id, first_name, last_name, ea_team, position, conference,
                overall, speed, strength, agility, change_of_direction,
                injury, awareness, player_id, match_method)
            VALUES %s
        ''', rows, page_size=1000)
        conn.commit()

        # Unmapped EA teams (labels with no equivalent in our active roster) —
        # surfaced so a future alias can be added rather than silently dropped.
        our = _known_teams
        unmapped_teams = sorted({
            p['team']['label'] for p in players
            if TEAM_ALIASES.get(p['team']['label'], p['team']['label']) not in our
        })

        print(f'\nStored {len(rows)} EA ratings.')
        print(f'Matched to players: {matched}/{len(rows)} '
              f'({matched * 100 // len(rows)}%).')
        print('Unmatched by reason:', dict(sorted(reasons.items())))
        if unmapped_teams:
            print(f'Unmapped EA teams ({len(unmapped_teams)}, likely FCS): {unmapped_teams}')
        print('\nSample unmatched players:')
        for s in unmatched_samples:
            print('   ', s)
    finally:
        cursor.close()
        conn.close()


if __name__ == '__main__':
    main()
