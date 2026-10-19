"""Self-check for the live game page's 5-second poll (main._live_*).

Stubs ESPN, so it needs no live game: python3 tools/check_live_poll.py
(main connects its DB pool on import, so .env must be present).
"""
import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402

GID = 401856809
SUMMARY = {
    'header': {'competitions': [{
        'status': {'displayClock': '2:17', 'period': 4, 'type': {'detail': '2:17 - 4th', 'shortDetail': '2:17 - 4th'}},
        'competitors': [{'homeAway': 'home', 'score': '21', 'possession': True},
                        {'homeAway': 'away', 'score': '24', 'possession': False}]}]},
    'drives': {'previous': [{'plays': [{'id': '1', 'modified': 'a', 'text': 'kickoff'}]}]},
    'winprobability': [{'playId': '1'}],
    'odds': [{'details': 'ND -10.5'}],
}


class Resp:
    def __init__(self, data, ok=True):
        self.data, self.ok = data, ok
        self.content = json.dumps(data).encode()

    def json(self):
        return self.data


def main_check():
    v = main._live_version
    base = v(SUMMARY)
    noise = copy.deepcopy(SUMMARY); noise['odds'][0]['details'] = 'ND -7'
    assert v(noise) == base, 'odds are not drawn, so they must not change the version'
    for change in (lambda d: d['drives']['previous'][0]['plays'].append({'id': '2', 'modified': 'b'}),
                   lambda d: d['drives']['previous'][0]['plays'][0].update(modified='a2'),   # corrected play
                   lambda d: d['header']['competitions'][0]['competitors'][0].update(score='28'),
                   lambda d: d['header']['competitions'][0]['status'].update(displayClock='1:47')):
        d = copy.deepcopy(SUMMARY); change(d)
        assert v(d) != base, 'a drawn change must change the version'

    calls = []
    answers = [Resp(SUMMARY), Resp({}, ok=False)]
    main.req.get = lambda *a, **k: calls.append(1) or answers.pop(0)
    main._live_espn.clear()
    raw, ver, as_of = main._live_summary(GID)
    assert ver == base and json.loads(raw) == SUMMARY and as_of
    assert main._live_summary(GID)[1] == base and len(calls) == 1, 'second ask within the TTL is served from memory'
    t, *rest = main._live_espn[GID]
    main._live_espn[GID] = (t - main.LIVE_FETCH_TTL - 1, *rest)                 # let it expire
    assert main._live_summary(GID) == (raw, ver, as_of) and len(calls) == 2, 'a failed fetch keeps the last good copy'

    main._live_window_ids = lambda: frozenset({GID})
    html = '<p>as of ' + main.LIVE_ASOF + '</p>'
    with main.app.test_request_context(f'/game/{GID}', headers={'X-Live-Version': base}):
        r = main._live_page(html, base, as_of)
        assert r.status_code == 204 and r.headers['X-Live-As-Of'] == as_of and r.headers['Cache-Control'] == 'no-store'
        assert main._live_poll_unchanged(), 'an unchanged poll skips the rate limit'
    with main.app.test_request_context(f'/game/{GID}', headers={'X-Live-Version': 'stale'}):
        r = main._live_page(html, base, as_of)
        assert r.status_code == 200 and as_of in r.get_data(as_text=True) and main.LIVE_ASOF not in r.get_data(as_text=True)
        assert not main._live_poll_unchanged(), 'a page that is behind is counted like any request'
    with main.app.test_request_context('/game/1', headers={'X-Live-Version': base}):
        assert not main._live_poll_unchanged(), 'the header lifts the limit only for a game in the live window'
    print('live poll: ok')


if __name__ == '__main__':
    main_check()
