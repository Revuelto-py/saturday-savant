"""The live game page's situation line and phone key-play feed, read out of
ESPN's live summary (held in memory by main._live_summary, never stored).

ESPN's plays name players only in their text ("#47 B.Bachmeier rush ..."), so
each card's player is the jersey number and surname matched against the box
score, which carries the athlete id, photo and the game line shown under it.
"""
import re

_NAME = re.compile(r"#(\d{1,2})\s+([A-Z][A-Za-z'\-]*\.\s?[A-Z][A-Za-z'\-]+(?:\s(?:Jr\.|Sr\.|II|III|IV))?)")
SWING_MIN = 0.09      # a non-scoring play needs this big a win-probability swing to be a key play
_NOT_A_SNAP = ('timeout', 'end period', 'end of', 'coin toss', 'two-minute', 'two minute')


def _period(n):
    return f'Q{n}' if n <= 4 else ('OT' if n == 5 else f'{n - 4}OT')


def _sides(d):
    comp = d['header']['competitions'][0]
    return comp, {str((c.get('team') or {}).get('id')): c.get('homeAway') for c in comp.get('competitors') or []}


def _plays(d):
    dr = d.get('drives') or {}
    drives = (dr.get('previous') or []) + ([dr['current']] if dr.get('current') else [])
    seen, out = set(), []
    for x in drives:
        for p in x.get('plays') or []:
            if p.get('id') not in seen:      # the current drive can repeat the last plays
                seen.add(p.get('id'))
                out.append((x, p))
    return out


def situation(d, abbr):
    """The hero's live line: clock, who has the ball, down and distance, and
    the drive so far. Any part ESPN hasn't sent comes back empty."""
    comp, side_of = _sides(d)
    st = comp.get('status') or {}
    name = (st.get('type') or {}).get('name', '')
    period = st.get('period') or 0
    if name == 'STATUS_HALFTIME':
        clock = 'Halftime'
    elif name == 'STATUS_END_PERIOD':
        clock = f'End of {_period(period)}'
    else:
        clock = f"{_period(period)} {st.get('displayClock', '')}".strip() if period else ''
    poss = next((c.get('homeAway') for c in comp.get('competitors') or [] if c.get('possession')), None)
    plays = _plays(d)
    end = (plays[-1][1].get('end') or {}) if plays else {}
    cur = (d.get('drives') or {}).get('current') or {}
    if not poss and cur:
        poss = side_of.get(str((cur.get('team') or {}).get('id')))
    drive = ''
    if cur and cur.get('description'):
        drive = cur['description'].replace(' yards', ' yds')
        start = (cur.get('start') or {}).get('text')
        if start:
            drive += f' from {start}'
    wp = d.get('winprobability') or []
    try:
        spot = field(d, abbr)
    except Exception:            # the field is one piece of the hero; never the line
        spot = None
    return {
        'clock': clock,
        'period': period,
        'poss': poss,
        'poss_abbr': abbr.get(poss) if poss else None,
        'down': end.get('downDistanceText') or '',
        'drive': drive,
        'home_wp': wp[-1].get('homeWinPercentage') if wp else None,
        'field': spot,
    }


def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


_SPOT = re.compile(r'^(.+?)\s+(\d{1,2})$')


def _to_go(spot, side, esp):
    """Yards from a play's start or end spot to the end zone `side` attacks.
    Read from the spot ESPN prints ("SC 32"), which is what the label shows;
    its yardsToEndzone is the fallback, and has been seen wrong (32 beside
    "SC 32" for South Carolina, put right on the next play). esp = ESPN's own
    abbreviation per side, which is what possessionText uses."""
    m = _SPOT.match((spot.get('possessionText') or '').strip())
    if m:
        n = int(m.group(2))
        if n == 50:
            return 50
        if m.group(1) == esp.get(side):
            return 100 - n
        if m.group(1) == esp.get('home' if side == 'away' else 'away'):
            return n
    return _num(spot.get('yardsToEndzone'))


def field(d, abbr):
    """The hero field's situation, as static/hx-field.js reads it: who has the
    ball and how far it is from the end zone they attack (the latest play's
    end, which is ESPN's spot for the next snap), where the current drive
    began, the down, distance and goal to go, a score on that play and any
    stoppage. Distances are ESPN's yardsToEndzone, from the offence, so nothing
    depends on which goal line ESPN's yardLine counts from. None until ESPN
    has spotted a play."""
    comp, side_of = _sides(d)
    side = lambda t: side_of.get(str((t or {}).get('id')))
    esp = {c.get('homeAway'): (c.get('team') or {}).get('abbreviation') for c in comp.get('competitors') or []}
    st = comp.get('status') or {}
    name = (st.get('type') or {}).get('name', '')
    brk = ('Halftime' if name == 'STATUS_HALFTIME' else
           f"End of {_period(st.get('period') or 0)}" if name == 'STATUS_END_PERIOD' else None)
    plays = [p for _, p in _plays(d)]
    # The latest real snap: a timeout or end-of-period row carries a spot too,
    # but not a reliable one, and no down.
    def spotted(p):
        e, kind = p.get('end') or {}, ((p.get('type') or {}).get('text') or '').lower()
        return (side(e.get('team')) and _to_go(e, side(e.get('team')), esp) is not None
                and not any(w in kind for w in _NOT_A_SNAP))
    i = len(plays) - 1
    while i >= 0 and not spotted(plays[i]):
        i -= 1
    if i < 0:
        return {'brk': brk, 'label': brk} if brk else None
    p, end = plays[i], plays[i]['end']
    poss = side(end['team'])
    to_go = _to_go(end, poss, esp)

    # A score puts the ball in the end zone the scorer attacks (a safety too: that
    # is the end zone the tackled offence defends); a field goal stays where it was
    # kicked from. The scorer is whichever side's score the play raised.
    score = scorer = None
    if p.get('scoringPlay') and i:
        prev = plays[i - 1]
        for s in ('away', 'home'):
            a, b = _num(p.get(f'{s}Score')), _num(prev.get(f'{s}Score'))
            if a is not None and b is not None and a > b:
                scorer = s
        kind = f"{(p.get('scoringType') or {}).get('name') or ''} {(p.get('type') or {}).get('text') or ''}".lower()
        if scorer:
            score = ('td' if 'touchdown' in kind else 'safety' if 'safety' in kind
                     else 'fg' if 'field' in kind and 'goal' in kind else None)
    if score == 'fg':
        kick = _to_go(p.get('start') or {}, scorer, esp)
        if kick is not None:
            poss, to_go = scorer, kick
    elif score:
        poss, to_go = scorer, 0

    # The drive bar starts at the current drive's first snap by the team with the
    # ball (a kickoff opening the drive is the other team's play, so it is passed).
    cur = (d.get('drives') or {}).get('current') or {}
    drive_to_go = None
    if side(cur.get('team')) == poss:
        for q in cur.get('plays') or []:
            s0 = q.get('start') or {}
            if side(s0.get('team')) == poss and _to_go(s0, poss, esp) is not None:
                drive_to_go = _to_go(s0, poss, esp)
                break
    if drive_to_go is None and not score:
        drive_to_go = to_go                    # a new possession starts where the ball is

    text = end.get('downDistanceText') or ''
    if score:
        label = {'td': 'Touchdown', 'fg': 'Field goal', 'safety': 'Safety'}[score] + (f', {abbr[scorer]}' if abbr.get(scorer) else '')
    elif text:
        label = text.replace(' at ', ' · ', 1)
    else:
        label = ((p.get('type') or {}).get('text') or '').strip()
    if brk and brk != 'Halftime':
        label = f'{brk} · {label}' if label else brk
    down, dist = _num(end.get('down')), _num(end.get('distance'))
    return {
        'poss': poss, 'toGo': to_go, 'driveToGo': drive_to_go,
        'down': down if down in (1, 2, 3, 4) else None,
        'distance': dist if dist and dist > 0 else None,
        'goal': 'goal' in (end.get('shortDownDistanceText') or text).lower(),
        'label': label, 'score': score, 'brk': brk, 'id': str(p.get('id') or ''),
    }


def _athletes(d, side_of, mirror):
    """aid -> {name, short, side, shot, cats}; (side, jersey) -> [aid]."""
    ath, by_jersey = {}, {}
    for box in (d.get('boxscore') or {}).get('players') or []:
        side = side_of.get(str((box.get('team') or {}).get('id')))
        for cat in box.get('statistics') or []:
            for ae in cat.get('athletes') or []:
                a = ae.get('athlete') or {}
                aid = str(a.get('id') or '')
                if not aid:
                    continue
                if aid not in ath:
                    hs = a.get('headshot')
                    parts = (a.get('displayName') or '').split()
                    ath[aid] = {'name': a.get('displayName', ''), 'last': (a.get('lastName') or parts[-1:] or [''])[0],
                                'short': f'{parts[0][0]}. {" ".join(parts[1:])}' if len(parts) > 1 else a.get('displayName', ''),
                                'side': side, 'cats': {},
                                'shot': mirror.get(aid) or (hs.get('href') if isinstance(hs, dict) else hs) or None}
                    by_jersey.setdefault((side, str(a.get('jersey') or '')), []).append(aid)
                ath[aid]['cats'][cat.get('name')] = dict(zip(cat.get('labels') or [], ae.get('stats') or []))
    return ath, by_jersey


def _find(ath, by_jersey, jersey, abbrev, prefer):
    last = abbrev.split('.', 1)[-1].strip().split()[0].lower()
    for side in (prefer, 'home' if prefer == 'away' else 'away'):
        cands = by_jersey.get((side, jersey)) or []
        for aid in cands:
            if ath[aid]['last'].lower().startswith(last) or last in ath[aid]['name'].lower():
                return aid
        if side == prefer and len(cands) == 1:
            return cands[0]
    return None


def _line(cats, cat):
    s = cats.get(cat) or {}
    td = lambda k='TD': f", {s[k]} td" if s.get(k) not in (None, '', '0') else ''
    if not s:
        return ''
    if cat == 'rushing':
        return f"{s.get('CAR')} car, {s.get('YDS')} yd{td()}"
    if cat == 'receiving':
        return f"{s.get('REC')} rec, {s.get('YDS')} yd{td()}"
    if cat == 'passing':
        ints = f", {s['INT']} int" if s.get('INT') not in (None, '', '0') else ''
        return f"{s.get('C/ATT')}, {s.get('YDS')} yd{td()}{ints}"
    if cat == 'interceptions':
        return f"{s.get('INT')} int"
    if cat == 'kicking':
        return f"{s.get('FG')} fg"
    if cat == 'fumbles':
        return f"{s.get('LOST')} fum lost"
    if cat == 'defensive':
        return f"{s.get('SACKS')} sack" if s.get('SACKS') not in (None, '', '0') else f"{s.get('TOT')} tkl"
    return ''


def _after(text, phrase):
    """The first player named after `phrase` in the play text, or None."""
    i = text.lower().find(phrase)
    return _NAME.search(text, i) if i >= 0 else None


def _pat(text):
    t = text.lower()
    if re.search(r'kick attempt good|extra point good|kick\)', t):
        return ('Extra point is good', 'g')
    if re.search(r'(kick attempt|extra point) (failed|blocked|missed|no good)', t):
        return ('Extra point no good', 'r')
    if re.search(r'(rush|pass|two-point|conversion) attempt (successful|good)|conversion (successful|good)', t):
        return ('2-pt try is good', 'g')
    if re.search(r'(rush|pass|two-point|conversion) attempt failed|conversion failed', t):
        return ('2-pt try failed', 'r')
    return None


def key_plays(d, abbr, mirror=None, limit=30):
    """Scoring plays, turnovers, the biggest win-probability swings and the
    latest snap, newest first, each as a feed card."""
    comp, side_of = _sides(d)
    ath, by_jersey = _athletes(d, side_of, mirror or {})
    wp = {str(w.get('playId')): w.get('homeWinPercentage') for w in d.get('winprobability') or []}
    plays = _plays(d)
    snaps = [i for i, (_, p) in enumerate(plays)
             if not any(s in ((p.get('type') or {}).get('text') or '').lower() for s in _NOT_A_SNAP)]
    last_snap = snaps[-1] if snaps else -1
    cards, prev_wp, prev_score = [], None, (0, 0)
    for n, (drive, p) in enumerate(plays):
        pid, typ = str(p.get('id')), (p.get('type') or {}).get('text', '')
        text = re.sub(r'^\(\d{1,2}:\d{2}\)\s*', '', p.get('text') or '')
        score = (p.get('awayScore') or 0, p.get('homeScore') or 0)
        home_wp = wp.get(pid)
        swing = home_wp - prev_wp if home_wp is not None and prev_wp is not None else 0
        if home_wp is not None:
            prev_wp = home_wp
        before, prev_score = prev_score, score
        t, low = typ.lower(), text.lower()
        if any(s in t for s in _NOT_A_SNAP):
            continue
        intercepted = 'interception' in t
        # ESPN leaves isTurnover false on fumbles, so the type decides.
        turnover = bool(p.get('isTurnover')) or intercepted or 'fumble recovery (opponent)' in t \
            or 'fumble return' in t
        if not (p.get('scoringPlay') or turnover or abs(swing) >= SWING_MIN or n == last_snap):
            continue

        offense = next((side_of.get(str(tp.get('id'))) for tp in p.get('teamParticipants') or []
                        if tp.get('type') == 'offense'), None) or side_of.get(str((drive.get('team') or {}).get('id')))
        defense = 'home' if offense == 'away' else 'away'
        names = list(_NAME.finditer(text))
        start, end = p.get('start') or {}, p.get('end') or {}
        yds = p.get('statYardage') or 0
        cat, cat2, second, line, line2 = None, None, None, '', ''
        first = names[0] if names else None
        kick = 'punt' if 'punt' in low else ('kickoff' if 'kickoff' in low else None)
        dist = re.search(r'(?:punt|kickoff) (?:for )?(\d+) y', low)
        ret = re.search(r'return(?:ed)? (?:for )?(-?\d+) y', low)
        fumbled = 'fumble' in t or 'fumbled by' in low
        if intercepted:
            first, second, cat, cat2 = _after(text, 'intercepted by') or (names[1] if len(names) > 1 else None), names[0] if names else None, 'interceptions', 'passing'
        elif fumbled and turnover:
            first, cat = _after(text, 'fumbled by') or first, 'fumbles'
        elif 'pass' in t and ('reception' in t or 'touchdown' in t or 'completion' in t or 'incompletion' in t):
            first, second, cat, cat2 = (names[1] if len(names) > 1 else first), first, 'receiving', 'passing'
        elif 'rush' in t:
            cat = 'rushing'
        elif 'field goal' in t:
            cat = 'kicking'
        elif 'sack' in t:
            first, cat = _after(text, 'sacked by') or first, 'defensive'
        elif kick and len(names) > 1 and (ret or 'fair catch' in low):
            second, first = first, names[1]
            line = f'{ret.group(1)}-yd return' if ret else 'fair catch'
            line2 = f'{dist.group(1)}-yd {kick}' if dist else ''
        elif kick and dist:
            line = f'{dist.group(1)}-yd {kick}'

        def who(m, pref):
            if not m:
                return None
            aid = _find(ath, by_jersey, m.group(1), m.group(2), pref)
            return ath.get(aid) if aid else {'short': m.group(2).replace('.', '. ', 1), 'side': pref, 'shot': None, 'cats': {}}
        pref = defense if (intercepted or 'sack' in t) else offense
        a = who(first, pref)
        b = who(second, offense) if second else None
        line = line or (_line(a['cats'], cat) if a else '')
        if b:
            line2 = f"{b['short']} · {line2 or _line(b['cats'], cat2)}".rstrip(' ·')

        # Headline
        scoring = bool(p.get('scoringPlay'))
        if 'field goal' in t:
            dist = re.search(r'(\d+) y(?:ar)?ds?\s+(?:field goal|fg)|field goal attempt from (\d+)', text, re.I)
            dist = next((g for g in (dist.groups() if dist else ()) if g), None)
            head = f'{dist}-yd field goal' if dist else 'Field goal'
            if 'good' not in t:
                head += ' no good'
        elif intercepted:
            head = 'Pick-six' if scoring else 'Interception'
        elif 'touchdown' in t and 'pass' in t:
            head = f'{yds}-yd TD catch'
        elif 'touchdown' in t and 'rush' in t:
            head = f'{yds}-yd TD run'
        elif 'touchdown' in t:
            head = f"{typ.replace(' Touchdown', '').replace(' Return', ' return')} TD"
        elif fumbled and turnover:
            # The recovering side is whoever has the ball once the play ends.
            rec = abbr.get(side_of.get(str((end.get('team') or {}).get('id'))), '')
            head = ('Muffed punt' if 'muff' in low else 'Punt fumbled' if kick == 'punt'
                    else 'Kickoff fumbled' if kick else 'Fumble') + (f', {rec} recovers' if rec else '')
            if end.get('possessionText'):
                head += f" at the {end['possessionText']}"
        elif kick:
            head = f'{dist.group(1)}-yd {kick}' if dist else kick.capitalize()
            if ret:
                head = f'{ret.group(1)}-yd {kick} return'
            if 'fair catch' in low:
                head += ', fair catch'
            elif 'touchback' in low:
                head += ', touchback'
            if (ret or 'fair catch' in low) and end.get('possessionText'):
                head += f" {'to' if ret else 'at'} the {end['possessionText']}"
        elif 'sack' in t:
            head = f'Sack, {abs(yds)}-yd loss' if yds else 'Sack'
        elif 'penalty' in t:
            m = re.search(r'PENALTY\s+(\S+)\s+(.+?)\s*\(', text)
            head = f'{m.group(2).capitalize()} on {m.group(1)}' if m else 'Penalty'
        elif 'rush' in t or 'pass' in t:
            if 'incompletion' in t:
                head = 'Incomplete pass'
            else:
                what = 'run' if 'rush' in t else 'catch'
                head = f'{yds}-yd {what}' if yds > 0 else (f'{-yds}-yd loss' if yds < 0 else 'No gain')
            converted = end.get('down') == 1 and yds > 0
            if start.get('down') in (3, 4) and start.get('shortDownDistanceText') and (converted or yds <= 0):
                head += f" on {start['shortDownDistanceText']}"
            elif yds > 0 and end.get('possessionText'):
                head += f" to the {end['possessionText']}"
        else:
            head = typ or 'Play'
        if scoring:
            lead = lambda s: (s[0] > s[1]) - (s[0] < s[1])     # +1 away ahead, -1 home ahead, 0 tied
            if lead(score) != lead(before):
                head += ', ties it' if lead(score) == 0 else f", {abbr.get('away' if lead(score) > 0 else 'home', '')} takes the lead"

        # Outcome
        outcome = None
        if scoring and 'touchdown' in t:
            outcome = _pat(text)
        elif 'field goal' in t:
            outcome = ('Field goal is good', 'g') if 'good' in t else ('No good', 'r')
        elif turnover and not intercepted:
            outcome = ('Fumble lost', 'r') if fumbled or 'muff' in low else ('Turnover', 'r')
        elif 'turnover on downs' in low:
            outcome = ('Turnover on downs', 'r')
        elif '1st down' in low and not scoring and 'penalty' not in t:
            outcome = ('First down', 'g')

        gainer = ('home' if swing > 0 else 'away') if swing else None
        per = (p.get('period') or {}).get('number') or 0
        when = f"{_period(per)} {(p.get('clock') or {}).get('displayValue', '')}"
        if kick:
            when += f' · {kick.capitalize()}'
        elif start.get('down'):
            when += f" · {start.get('shortDownDistanceText', '')}"
        cards.append({
            'id': pid, 'head': head, 'scoring': scoring, 'when': when,
            'away': score[0], 'home': score[1],
            'side': (a or {}).get('side') or (defense if intercepted else offense),
            'shot': (a or {}).get('shot'), 'name': (a or {}).get('short'),
            'line': line, 'line2': line2, 'outcome': outcome,
            'swing': round(abs(swing) * 100) if gainer and round(abs(swing) * 100) >= 1 else None,
            'gainer': gainer,
            'gainer_wp': round((home_wp if gainer == 'home' else 1 - home_wp) * 100) if gainer and home_wp is not None else None,
        })
    return cards[::-1][:limit]
