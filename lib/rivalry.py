"""Series detail for /rivalry/<a>/<b>: the season-by-season columns behind the
hero strip and the margin chart, where the games were played, the streaks.

Pure functions over the meeting rows main.rivalry_history already loads (each
carrying winner / margin / a_pts / b_pts / neutral), so the logic is checked
without a database: python -m lib.rivalry
"""


def _and(items):
    items = [str(i) for i in items]
    return items[0] if len(items) == 1 else ', '.join(items[:-1]) + ' and ' + items[-1]


def _span(a, b):
    """2016–19, or 2025 for a single season."""
    return str(a) if a == b else f'{a}–{str(b)[2:]}'


def series_line(s, ta, tb, since):
    """'Kent State leads 6–4', 'Series tied 5–5' or 'First meeting since 2016',
    from a _rivalry_series dict."""
    if not s['meetings']:
        return f'First meeting since {since}'
    if s['leader'] == ta:
        return f"{ta} leads {s['a_wins']}–{s['b_wins']}"
    if s['leader'] == tb:
        return f"{tb} leads {s['b_wins']}–{s['a_wins']}"
    return f"Series tied {s['a_wins']}–{s['b_wins']}"


def columns(games, ta, tb, nxt=None):
    """Oldest-first columns: one per meeting, a 'none' column for each season
    inside the span that had no meeting, then the next meeting if one is on the
    schedule. Nothing outside the span, so a pair that met twice in ten years
    gets two columns rather than eight empty ones.

    Each game column carries `side` ('a' / 'b', None for a tie) and `frac`, its
    margin as a share of the largest on the chart; the next meeting's `frac` is
    the forecast margin on the same scale, on the favourite's side."""
    chron = sorted(games, key=lambda g: (g['season'], g.get('date') or ''))
    nxt_season = nxt['season'] if nxt else None
    cols = []
    if chron:
        for yr in range(chron[0]['season'], max(chron[-1]['season'], nxt_season or 0) + 1):
            ms = [g for g in chron if g['season'] == yr]
            if ms:
                cols.extend({'kind': 'game', 'season': yr, 'g': g} for g in ms)
            elif yr != nxt_season:
                cols.append({'kind': 'none', 'season': yr})
    if nxt:
        cols.append({'kind': 'next', 'season': nxt_season, 'g': nxt})

    top = max([c['g']['margin'] or 0 for c in cols if c['kind'] == 'game']
              + [abs(nxt.get('margin') or 0) if nxt else 0] + [1])
    for c in cols:
        g = c.get('g')
        if c['kind'] == 'game':
            c['side'] = 'a' if g['winner'] == ta else 'b' if g['winner'] == tb else None
            c['frac'] = round((g['margin'] or 0) / top, 3)
            hi, lo = max(g['a_pts'], g['b_pts']), min(g['a_pts'], g['b_pts'])
            c['score'] = f'{hi}–{lo}'
        elif c['kind'] == 'next':
            c['side'] = 'a' if g.get('fav') == ta else 'b' if g.get('fav') == tb else None
            c['frac'] = round(abs(g.get('margin') or 0) / top, 3)
    return cols


def _venue(label, gs, ta, tb):
    a = sum(1 for g in gs if g['winner'] == ta)
    b = sum(1 for g in gs if g['winner'] == tb)
    lead = ta if a > b else tb if b > a else None
    return {'label': label, 'lead': lead or 'Even', 'rec': f'{max(a, b)}–{min(a, b)}'}


def detail(games, ta, tb, since, cols):
    """Tiles and side-column facts. `games` newest first; ties and unscored
    rows (winner None) count as meetings elsewhere but not here."""
    decided = [g for g in games if g['winner']]
    out = {'venues': [], 'road_line': '', 'streaks': [], 'ppg': None,
           'closest_note': '', 'biggest_note': '', 'meetings_note': f'since {since}'}

    gaps = [c['season'] for c in cols if c['kind'] == 'none']
    if len(gaps) == 1:
        out['meetings_note'] = f'{gaps[0]} was not played'
    elif len(gaps) == 2:
        out['meetings_note'] = f'{gaps[0]} and {gaps[1]} not played'
    elif gaps:
        out['meetings_note'] = f'{len(gaps)} seasons without one'
    if not decided:
        return out

    for host in (ta, tb):
        gs = [g for g in decided if not g['neutral'] and g['home_team'] == host]
        if gs:
            out['venues'].append(_venue(f'At {host}', gs, ta, tb))
    gs = [g for g in decided if g['neutral']]
    if gs:
        out['venues'].append(_venue('Neutral site', gs, ta, tb))
    road = [g for g in decided if not g['neutral']]
    rw = sum(1 for g in road if g['winner'] != g['home_team'])
    hw = len(road) - rw
    if len(road) >= 2:
        out['road_line'] = (f'The road team has won {rw} of the {len(road)}.' if rw > hw else
                            f'The home team has won {hw} of the {len(road)}.' if hw > rw else
                            f'Home and road have split them {hw}–{rw}.')

    # Longest run for each side (the latest, when two are equally long).
    best, team, run, start = {}, None, 0, None
    for g in reversed(decided):
        if g['winner'] == team:
            run += 1
        else:
            team, run, start = g['winner'], 1, g['season']
        if run >= best.get(team, (0,))[0]:
            best[team] = (run, start, g['season'])
    for t in (ta, tb):
        if t in best:
            n, a, b = best[t]
            out['streaks'].append({'team': t, 'span': _span(a, b), 'n': n})

    n = len(decided)
    out['ppg'] = (round(sum(g['a_pts'] for g in decided) / n, 1),
                  round(sum(g['b_pts'] for g in decided) / n, 1))
    low = min(g['margin'] for g in decided)
    out['closest_note'] = _and(sorted({g['season'] for g in decided if g['margin'] == low}, reverse=True)[:3])
    big = max(decided, key=lambda g: g['margin'])   # newest first, so ties keep the latest
    out['biggest_note'] = f"{big['winner']}, {big['season']}"
    return out


if __name__ == '__main__':
    def G(season, home, away, hp, ap, neutral=False):
        w = home if hp > ap else away if ap > hp else None
        a_pts = hp if home == 'Michigan' else ap
        b_pts = ap if home == 'Michigan' else hp
        return {'season': season, 'home_team': home, 'away_team': away, 'winner': w, 'margin': abs(hp - ap),
                'a_pts': a_pts, 'b_pts': b_pts, 'neutral': neutral, 'date': f'{season}-11-25'}
    A, B = 'Michigan', 'Ohio State'
    games = [G(2025, A, B, 9, 27), G(2024, B, A, 10, 13), G(2023, A, B, 30, 24), G(2022, B, A, 23, 45),
             G(2021, A, B, 42, 27), G(2019, A, B, 27, 56), G(2018, B, A, 62, 39), G(2017, A, B, 20, 31),
             G(2016, B, A, 30, 27)]
    nxt = {'season': 2026, 'fav': B, 'margin': 13.8}
    cols = columns(games, A, B, nxt)
    assert [c['season'] for c in cols] == list(range(2016, 2027)), cols
    assert cols[4]['kind'] == 'none' and cols[-1]['kind'] == 'next' and cols[-1]['side'] == 'b'
    assert cols[3]['frac'] == 1 and cols[3]['side'] == 'b' and cols[3]['score'] == '56–27'
    d = detail(games, A, B, 2016, cols)
    assert d['meetings_note'] == '2020 was not played'
    assert d['venues'] == [{'label': 'At Michigan', 'lead': B, 'rec': '3–2'},
                           {'label': 'At Ohio State', 'lead': 'Even', 'rec': '2–2'}], d['venues']
    assert d['road_line'] == 'The road team has won 5 of the 9.'
    assert d['streaks'] == [{'team': A, 'span': '2021–24', 'n': 4}, {'team': B, 'span': '2016–19', 'n': 4}], d['streaks']
    assert d['ppg'] == (28.0, 32.2) and d['closest_note'] == '2024 and 2016' and d['biggest_note'] == 'Ohio State, 2019'
    # A pair that met twice, years apart: no empty columns outside the span, one gap inside.
    two = [G(2022, A, B, 20, 10), G(2018, B, A, 7, 3)]
    assert [c['kind'] for c in columns(two, A, B)] == ['game', 'none', 'none', 'none', 'game']
    assert detail(two, A, B, 2016, columns(two, A, B))['meetings_note'] == '3 seasons without one'
    # No meetings yet, one scheduled.
    assert [c['kind'] for c in columns([], A, B, nxt)] == ['next']
    assert series_line({'meetings': 10, 'leader': None, 'a_wins': 5, 'b_wins': 5}, A, B, 2016) == 'Series tied 5–5'
    assert series_line({'meetings': 10, 'leader': B, 'a_wins': 4, 'b_wins': 6}, A, B, 2016) == 'Ohio State leads 6–4'
    print('lib.rivalry ok')
