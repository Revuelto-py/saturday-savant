"""The game page's Game leaders rows, from the page's own box score.

One row per category, the away team's leader on the left and the home team's
on the right, as the hero has them: passing, rushing and receiving yards,
tackles, sacks, and takeaways (interceptions plus fumbles recovered), each
leader with the line that belongs under his name. Everything comes from the
box score main.game builds (box[side][category] = {'labels', 'athletes'}), so
interceptions thrown, fumbles lost and the defence are all there; ESPN's own
leaders array carries $ref placeholders for college games and no defence.
Self-check: python3 lib/game_leaders.py
"""


def _num(v):
    try:
        return float(str(v).replace(',', ''))
    except (TypeError, ValueError):
        return 0.0


def _fig(v):
    return str(int(v)) if float(v).is_integer() else f'{v:g}'


def _short(name):
    """"Bear Bachmeier" -> "B. Bachmeier"; everything after the first token is kept."""
    parts = (name or '').split()
    return f'{parts[0][0]}. {" ".join(parts[1:])}' if len(parts) > 1 else (name or '')


def _plural(n, word):
    return f'{_fig(n)} {word}' + ('' if n == 1 else 's')


def _athletes(box, side, cat):
    """A category's real players (ESPN adds a 'Team' line for team stats)."""
    return [a for a in ((box.get(side) or {}).get(cat) or {}).get('athletes') or []
            if a.get('name') and a.get('name') != 'Team']


def _best(box, side, cat, col):
    """The side's leader in one column of one category, or None if nobody has any."""
    best = max(_athletes(box, side, cat), key=lambda a: _num((a.get('stats') or {}).get(col)), default=None)
    return best if best and _num(best['stats'].get(col)) > 0 else None


def _lost(box, side, name):
    """Fumbles a player lost, from the fumbles category."""
    for a in _athletes(box, side, 'fumbles'):
        if a['name'] == name:
            return _num(a['stats'].get('LOST'))
    return 0


def _line(bits):
    """Pieces joined with dots, each kept whole when the line wraps ("long 30", not "long / 30")."""
    return ' · '.join(b.replace(' ', '\u00a0') for b in bits if b)


def _player(a, v, line, warn=''):
    return {'name': _short(a['name']), 'full': a['name'], 'headshot': a.get('headshot') or '',
            'player_id': a.get('player_id'), 'v': v, 'fig': _fig(v), 'line': line, 'warn': warn}


def _offence(box, side, cat):
    a = _best(box, side, cat, 'YDS')
    if not a:
        return None
    s, td = a['stats'], _num(a['stats'].get('TD'))
    lost = _lost(box, side, a['name'])
    if cat == 'passing':
        ints = _num(s.get('INT'))
        bits = [s.get('C/ATT') or '', f'{_fig(td)} TD' if td else '']
        warn = f'{_fig(ints)} INT' if ints else ''
        if not ints:
            bits.append('0 INT')
    else:
        lead = f"{s.get('CAR')} car" if cat == 'rushing' else f"{s.get('REC')} rec"
        long_ = _num(s.get('LONG'))
        bits = [lead, f'{_fig(td)} TD' if td else '', f'long {_fig(long_)}' if long_ else '']
        warn = _plural(lost, 'fumble') + ' lost' if lost else ''
    return _player(a, _num(s.get('YDS')), _line(bits), warn)


def _defence(box, side, col):
    a = _best(box, side, 'defensive', col)
    if not a:
        return None
    s = a['stats']
    solo, sacks, tfl, pd, tot = (_num(s.get(k)) for k in ('SOLO', 'SACKS', 'TFL', 'PD', 'TOT'))
    if col == 'TOT':
        bits = [f'{_fig(solo)} solo' if solo else '', _plural(sacks, 'sack') if sacks else '',
                f'{_fig(tfl)} TFL' if tfl else '',
                ('1 pass broken up' if pd == 1 else f'{_fig(pd)} passes broken up') if pd else '']
    else:
        bits = [f'{_fig(tfl)} TFL' if tfl else '', _plural(tot, 'tackle') if tot else '']
    return _player(a, _num(s.get(col)), _line(bits))


def _takeaways(box, side):
    """Interceptions by this side's defence plus the other side's fumbles lost.
    Fumble recoverers are named only when this side's recoveries add up to the
    other side's losses (a player who falls on his own team's fumble is
    credited a recovery too, and is no takeaway)."""
    other = 'home' if side == 'away' else 'away'
    picks = [(a['name'], _num(a['stats'].get('INT'))) for a in _athletes(box, side, 'interceptions')]
    picks = [p for p in picks if p[1] > 0]
    n_int = sum(n for _, n in picks)
    n_fum = sum(_num(a['stats'].get('LOST')) for a in _athletes(box, other, 'fumbles'))
    recs = [(a['name'], _num(a['stats'].get('REC'))) for a in _athletes(box, side, 'fumbles')]
    recs = [r for r in recs if r[1] > 0]
    if sum(n for _, n in recs) != n_fum:
        recs = []
    total = n_int + n_fum
    if not total:
        return {'name': 'No takeaways', 'v': 0, 'fig': '0', 'line': '', 'warn': '', 'team': True}
    parts = []
    if n_int:
        parts.append(_plural(n_int, 'interception'))
    if n_fum:
        parts.append(_plural(n_fum, 'fumble recovery').replace('recoverys', 'recoveries'))
    who = [n.split()[-1] if len(n.split()) > 1 and n.split()[-1] not in ('Jr.', 'Sr.', 'II', 'III', 'IV')
           else ' '.join(n.split()[-2:]) for n, _ in picks + recs]
    return {'name': ', '.join(parts), 'v': total, 'fig': _fig(total), 'line': ', '.join(w.replace(' ', '\u00a0') for w in who), 'warn': '', 'team': True}


CATS = (
    ('Passing yds', lambda b, s: _offence(b, s, 'passing'), 'No passing'),
    ('Rushing yds', lambda b, s: _offence(b, s, 'rushing'), 'No rushing'),
    ('Receiving yds', lambda b, s: _offence(b, s, 'receiving'), 'No catches'),
    ('Tackles', lambda b, s: _defence(b, s, 'TOT'), 'No tackles'),
    ('Sacks', lambda b, s: _defence(b, s, 'SACKS'), 'No sacks'),
)


def rows(box):
    """[{k, away, home, share_away, share_home}] — a side with nobody gets a
    team placeholder ({'name': 'No sacks', 'v': 0, 'team': True}); a category
    with nobody on either side is left out, and with no box score at all the
    list is empty and the section stays hidden."""
    if not (box.get('away') or box.get('home')):
        return []
    out = []
    for k, pick, none in CATS:
        a, h = pick(box, 'away'), pick(box, 'home')
        if not (a or h):
            continue
        blank = {'name': none, 'v': 0, 'fig': '0', 'line': '', 'warn': '', 'team': True}
        out.append({'k': k, 'away': a or dict(blank), 'home': h or dict(blank)})
    a, h = _takeaways(box, 'away'), _takeaways(box, 'home')
    if a['v'] or h['v'] or out:
        out.append({'k': 'Takeaways', 'away': a, 'home': h})
    for r in out:
        av, hv = r['away']['v'], r['home']['v']
        r['away']['lead'] = av > 0 and av >= hv
        r['home']['lead'] = hv > 0 and hv >= av
        t = av + hv
        r['share_away'], r['share_home'] = (round(av / t, 4), round(hv / t, 4)) if t else (0, 0)
    return out


def turnovers(rows_):
    """Each side's giveaways: the other side's takeaways."""
    for r in rows_:
        if r['k'] == 'Takeaways':
            return {'away': r['home']['fig'], 'home': r['away']['fig']}
    return None


if __name__ == '__main__':
    # BYU 20 at Utah 28, 2026 week 10, trimmed from ESPN's box score.
    def ath(name, **stats):
        return {'name': name, 'headshot': '', 'player_id': None, 'stats': {k.replace('_', '/'): str(v) for k, v in stats.items()}}
    box = {
        'away': {
            'passing': {'athletes': [ath('Bear Bachmeier', C_ATT='21/35', YDS=272, TD=1, INT=4), ath('Team', C_ATT='0/1', YDS=0)]},
            'rushing': {'athletes': [ath('Bear Bachmeier', CAR=8, YDS=59, TD=0, LONG=19), ath('Sione Moa', CAR=10, YDS=37, TD=0, LONG=13)]},
            'receiving': {'athletes': [ath('Legend Glasker', REC=7, YDS=118, TD=1, LONG=75)]},
            'defensive': {'athletes': [ath('Cade Uluave', TOT=7, SOLO=2, SACKS=1, TFL=1, PD=0),
                                       ath('Bodie Schoonover', TOT=6, SOLO=3, SACKS=2, TFL=3, PD=0)]},
            'fumbles': {'athletes': [ath('Therrian Alexander III', FUM=0, LOST=0, REC=1), ath('Nusi Taumoepeau', FUM=0, LOST=0, REC=1)]},
        },
        'home': {
            'passing': {'athletes': [ath('Devon Dampier', C_ATT='14/26', YDS=177, TD=2, INT=0)]},
            'rushing': {'athletes': [ath('Devon Dampier', CAR=20, YDS=89, TD=2, LONG=30)]},
            'receiving': {'athletes': [ath('Braden Pegan', REC=5, YDS=86, TD=2, LONG=46)]},
            'defensive': {'athletes': [ath('Johnathan Hall', TOT=9, SOLO=5, SACKS=0, TFL=0, PD=1)]},
            'interceptions': {'athletes': [ath('Lance Holtzclaw', INT=1), ath('James Chenault Jr.', INT=1),
                                           ath('Trey Reynolds', INT=1), ath('Nate Ritchie', INT=1)]},
            'fumbles': {'athletes': [ath('Braden Pegan', FUM=1, LOST=1, REC=0), ath('Devon Dampier', FUM=1, LOST=1, REC=0)]},
        },
    }
    r = {x['k']: x for x in rows(box)}
    for x in r.values():
        for s_ in ('away', 'home'):
            x[s_]['line'] = x[s_]['line'].replace('\u00a0', ' ')
    assert r['Passing yds']['away']['line'] == '21/35 · 1 TD' and r['Passing yds']['away']['warn'] == '4 INT'
    assert r['Passing yds']['home']['line'] == '14/26 · 2 TD · 0 INT' and r['Passing yds']['away']['lead']
    assert r['Rushing yds']['home']['line'] == '20 car · 2 TD · long 30' and r['Rushing yds']['home']['warn'] == '1 fumble lost'
    assert r['Rushing yds']['away']['line'] == '8 car · long 19' and r['Rushing yds']['home']['lead']
    assert r['Receiving yds']['home']['warn'] == '1 fumble lost'
    assert r['Tackles']['away']['line'] == '2 solo · 1 sack · 1 TFL' and r['Tackles']['home']['line'] == '5 solo · 1 pass broken up'
    assert r['Sacks']['away']['name'] == 'B. Schoonover' and r['Sacks']['home'] == {**r['Sacks']['home'], 'name': 'No sacks', 'team': True}
    assert r['Takeaways']['home']['name'] == '4 interceptions' and r['Takeaways']['home']['line'] == 'Holtzclaw, Chenault Jr., Reynolds, Ritchie'
    assert r['Takeaways']['away']['name'] == '2 fumble recoveries' and r['Takeaways']['away']['line'] == 'Alexander III, Taumoepeau'
    assert turnovers(rows(box)) == {'away': '4', 'home': '2'}
    assert abs(r['Sacks']['away']['v'] - 2) < 1e-9 and r['Sacks']['share_away'] == 1
    assert rows({'away': {}, 'home': {}}) == []
    print('game leaders: ok')
