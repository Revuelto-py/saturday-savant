"""ESPN's scoreboard, as a second opinion on which games are under way.

CFBD is the site's primary live source and is usually right. It is not always.
A game that kicks off late keeps `status: scheduled` on CFBD's board for the
whole delay: on 2026-09-07, SMU at Florida State was pushed back two hours and
sat at "scheduled, no score" on CFBD while ESPN already had it in the first
quarter. Nothing downstream could tell the game had begun — /api/live dropped
it because its state was neither live nor final, the score cron skipped it for
the same reason, and the ticker went on advertising a 7:30 kickoff.

This fills that gap. It is deliberately NOT a replacement: callers apply it only
where CFBD reports nothing or still calls a game upcoming, so the primary source
keeps precedence everywhere it has an opinion.

One request covers the whole slate, so it costs the same as CFBD's board.
"""

import datetime
import json
import urllib.request
from zoneinfo import ZoneInfo

SCOREBOARD = ('https://site.api.espn.com/apis/site/v2/sports/football/'
              'college-football/scoreboard')

# ESPN's own vocabulary for where a game is, mapped onto the site's.
_STATE = {'in': 'live', 'post': 'final', 'pre': 'pre'}


def _slate_date():
    """Today in Eastern time. A game kicking off at 8pm ET is already tomorrow
    in UTC, and asking ESPN for the UTC date would miss the entire night slate.
    """
    return datetime.datetime.now(ZoneInfo('America/New_York')).strftime('%Y%m%d')


def fetch(dates=None, timeout=6):
    """{game_id: {'state', 'home', 'away', 'status'}} for one day's games.

    Returns {} on any failure — every caller treats this as an optional extra
    opinion, so a timeout or a shape change must never take a page down with it.
    """
    day = dates or _slate_date()
    url = f'{SCOREBOARD}?limit=400&dates={day}'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except Exception as exc:
        print(f'espn board unavailable: {type(exc).__name__} {exc}', flush=True)
        return {}

    out = {}
    for event in payload.get('events') or []:
        gid = str(event.get('id') or '')
        if not gid:
            continue
        stype = ((event.get('status') or {}).get('type') or {})
        state = _STATE.get(stype.get('state'), 'pre')
        comp = (event.get('competitions') or [{}])[0]
        side = {}
        for team in comp.get('competitors') or []:
            try:
                side[team.get('homeAway')] = int(team.get('score'))
            except (TypeError, ValueError):
                side[team.get('homeAway')] = None
        out[gid] = {
            'state': state,
            'home': side.get('home'),
            'away': side.get('away'),
            # shortDetail is already "9:32 - 1st" / "Final" — ESPN's own wording,
            # which is what a reader checking a second source would see.
            'status': 'Final' if state == 'final' else (stype.get('shortDetail') or 'Live'),
        }
    return out
