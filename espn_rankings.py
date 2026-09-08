"""ESPN's AP Top 25, as a second source for the weeks CFBD hasn't posted.

CFBD is the primary source for `ap_rankings` and is usually first. It is not
always. The AP released its Week 2 poll for 2026 on the morning of 2026-09-08
(week 1 ran through Labor Day, so the first in-season poll landed on a Tuesday
rather than the usual Sunday); CFBD still returned only the preseason poll hours
later, which left the site advertising a week-old top 25.

Two ESPN endpoints disagree about this, which is the trap worth documenting:

    site.api.espn.com/.../college-football/rankings   -> still said "Preseason"
    sports.core.api.espn.com/v2/.../weeks/2/rankings/1 -> had the Week 2 poll

The site API is the one most code reaches for and it is the one that was stale,
so this module uses the core API, where each season/week/poll is addressed
directly. Poll id 1 is the AP Top 25.

Teams arrive as `$ref` URLs carrying an ESPN team id. The site does not store
that id in a column, but `teams.logo` is an ESPN CDN URL with the id in its
filename, so the caller resolves names through that.

This is deliberately NOT a replacement for CFBD: the caller keeps every week
CFBD has an opinion on and fills only the gaps.
"""

import json
import re
import urllib.request

CORE = ('http://sports.core.api.espn.com/v2/sports/football/leagues/'
        'college-football/seasons/{season}/types/{stype}/weeks/{week}/rankings')

AP_POLL_ID = 1          # ESPN's id for the AP Top 25
_TYPE_REGULAR = 2       # ESPN season types: 2 = regular, 3 = postseason
_TYPE_POST = 3

_UA = {'User-Agent': 'Mozilla/5.0'}


def _get(url, timeout):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def team_id_from_logo(url):
    """ESPN team id out of a logo URL, or None.

    `https://a.espncdn.com/i/teamlogos/ncaa/500/2032.png` -> '2032'
    """
    if not url:
        return None
    m = re.search(r'/(\d+)\.png', url)
    return m.group(1) if m else None


def _poll(season, stype, week, timeout):
    """One week's AP poll, or None when ESPN has no such poll.

    Returns {'week', 'season_type', 'date', 'ranks': [{'espn_id', 'rank',
    'points', 'first_place_votes'}]}.
    """
    url = CORE.format(season=season, stype=stype, week=week)
    try:
        d = _get(f'{url}/{AP_POLL_ID}?lang=en&region=us', timeout)
    except Exception:
        return None
    ranks = []
    for r in d.get('ranks') or []:
        tid = None
        ref = ((r.get('team') or {}).get('$ref')) or ''
        m = re.search(r'/teams/(\d+)', ref)
        if m:
            tid = m.group(1)
        if not tid or r.get('current') is None:
            continue
        pts = r.get('points')
        fpv = r.get('firstPlaceVotes')
        ranks.append({
            'espn_id': tid,
            'rank': int(r['current']),
            # ESPN sends these as floats; the column is an integer.
            'points': int(pts) if pts is not None else None,
            'first_place_votes': int(fpv) if fpv is not None else None,
        })
    if not ranks:
        return None
    return {
        'week': week,
        'season_type': 'postseason' if stype == _TYPE_POST else 'regular',
        'date': d.get('date'),
        'ranks': sorted(ranks, key=lambda x: x['rank']),
    }


def ap_polls(season, max_week=20, timeout=15):
    """Every AP poll ESPN holds for a season, chronological.

    Walks regular-season weeks until two consecutive weeks come back empty (a
    single gap is survivable; the season simply has not reached that week yet),
    then checks the postseason final. Returns [] on any failure, because every
    caller treats this as an optional second opinion.

    Note this does NOT set prev_rank: the caller derives that by walking the
    merged CFBD+ESPN set, so movement stays correct across a mixed-source
    season. ESPN's own `previous` field disagrees with the stored preseason poll
    in places and is not used.
    """
    out = []
    misses = 0
    for wk in range(1, max_week + 1):
        p = _poll(season, _TYPE_REGULAR, wk, timeout)
        if p:
            out.append(p)
            misses = 0
        else:
            misses += 1
            if misses >= 2:
                break
    final = _poll(season, _TYPE_POST, 1, timeout)
    if final:
        out.append(final)
    return out
