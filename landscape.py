"""The Stat Explorer's "landscape": six server-rendered charts of college football
as a whole, one season at a time.

Everything here is query -> numbers -> SVG markup. No client library: the charts
are static pictures with a one-line finding each, and Plotly is already paying
for the interactive scatter above them. `build(cursor, season)` returns a dict
the template drops in with |safe; every finding is computed from the same rows
the chart draws, so the sentence and the picture cannot disagree.

The map outline (us_outline.json) is us-atlas's states-albers-10m, already
projected with d3's geoAlbersUsa (scale 1300, translate [487.5, 305]); stadium
coordinates are pushed through the same projection in albers() so the dots
land on it.
"""
import json
import math
import os
from collections import Counter, defaultdict
from functools import lru_cache

P4 = ('SEC', 'Big Ten', 'Big 12', 'ACC')
G5 = ('American Athletic', 'Mountain West', 'Sun Belt', 'Mid-American', 'Conference USA', 'Pac-12')
SHORT = {'American Athletic': 'American', 'Mid-American': 'MAC', 'Conference USA': 'C-USA',
         'FBS Independents': 'Independents'}
BLUE, ORANGE, GREY = '#1c9cf0', '#f0a868', '#7a8086'


def _esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def ordinal(n):
    return f'{n}' + ('th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th'))


@lru_cache(maxsize=1)
def outline():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'us_outline.json')) as f:
        return json.load(f)


def albers(lon, lat):
    """d3.geoAlbersUsa's lower-48 projection at scale 1300, translate [487.5, 305]."""
    p1, p2 = math.radians(29.5), math.radians(45.5)
    n = (math.sin(p1) + math.sin(p2)) / 2
    c = math.cos(p1) ** 2 + 2 * n * math.sin(p1)
    r0 = math.sqrt(c) / n

    def raw(lam, phi):
        r = math.sqrt(c - 2 * n * math.sin(phi)) / n
        return r * math.sin(lam * n), r0 - r * math.cos(lam * n)
    x, y = raw(math.radians(lon + 96), math.radians(lat))
    cx, cy = raw(math.radians(-0.6), math.radians(38.7))
    return 487.5 + 1300 * (x - cx), 305 - 1300 * (y - cy)


# ── Conference strength ─────────────────────────────────────────────────────
def conf_strips(teams, power):
    by = defaultdict(list)
    for t in teams:
        by[t['conf']].append(t)
    med = {k: sorted(x['net'] for x in v)[len(v) // 2] for k, v in by.items()}
    confs = sorted(by, key=lambda k: (k == 'FBS Independents', -med[k]))
    lo = math.floor(min(t['net'] for t in teams) / 10) * 10
    hi = math.ceil(max(t['net'] for t in teams) / 10) * 10
    W, rowh, L, R = 1160, 46, 150, 120
    H = rowh * len(confs) + 40
    X = lambda v: L + (v - lo) / (hi - lo) * (W - L - R)
    o = [f'<svg class="xl-svg" viewBox="0 0 {W} {H}" role="img" aria-label="Every FBS team&#39;s Savant Net Rating, one row per conference, ordered by the median team.">']
    for v in range(lo, hi + 1, 10):
        o.append(f'<line x1="{X(v):.1f}" x2="{X(v):.1f}" y1="0" y2="{H-30}" stroke="rgba(255,255,255,{.16 if v == 0 else .05})"></line>'
                 f'<text x="{X(v):.1f}" y="{H-8}" fill="#949a9f" font-size="12" text-anchor="middle">{v:+d}</text>')
    for i, k in enumerate(confs):
        cy = 20 + i * rowh
        vals = sorted(x['net'] for x in by[k])
        o.append(f'<text x="{L-16}" y="{cy+5}" fill="#e7e9ea" font-size="14" font-weight="650" text-anchor="end">{_esc(SHORT.get(k, k))}</text>')
        o.append(f'<line x1="{X(vals[0]):.1f}" x2="{X(vals[-1]):.1f}" y1="{cy}" y2="{cy}" stroke="rgba(255,255,255,.12)" stroke-width="2"></line>')
        for t in by[k]:
            o.append(f'<circle cx="{X(t["net"]):.1f}" cy="{cy}" r="6" fill="{BLUE if k in power else GREY}" fill-opacity=".75" stroke="#0a0b0d" stroke-width="1.5"><title>{_esc(t["team"])} {t["net"]:+.1f}</title></circle>')
        o.append(f'<rect x="{X(med[k])-1.5:.1f}" y="{cy-13}" width="3" height="26" rx="1.5" fill="#fff"></rect>')
        top = max(by[k], key=lambda t: t['net'])
        o.append(f'<text x="{X(top["net"])+12:.1f}" y="{cy+4}" fill="#9aa0a6" font-size="12">{_esc(top["team"])}</text>')
    o.append('</svg>')
    full = [k for k in confs if len(by[k]) >= 8]
    g5 = [t for t in teams if t['conf'] not in power and t['conf'] != 'FBS Independents']
    out = {'svg': ''.join(o), 'top_conf': SHORT.get(full[0], full[0]) if full else None}
    if g5:
        best = max(g5, key=lambda t: t['net'])
        out.update(g5_best=best['team'], g5_net=best['net'],
                   g5_rank=ordinal(1 + sum(1 for t in teams if t['conf'] in power and t['net'] > best['net'])),
                   g5_above=sum(1 for t in teams if t['conf'] in power and t['net'] < best['net']))
    return out


# ── Rank river ──────────────────────────────────────────────────────────────
def river(weekly):
    rank = defaultdict(dict)
    for week, team, rk in weekly:
        rank[team][week] = rk
    weeks = sorted({w for w, _, _ in weekly})
    if len(weeks) < 2:
        return None
    last = weeks[-1]
    final10 = sorted((t for t, r in rank.items() if r.get(last, 999) <= 10), key=lambda t: rank[t][last])
    dropped = [t for t, r in rank.items() if t not in final10 and any(v <= 10 for v in r.values())]
    W, H, L, R, TP, B = 560, 420, 40, 150, 16, 34
    cap = 25
    X = lambda w: L + (w - weeks[0]) / (weeks[-1] - weeks[0]) * (W - L - R)
    Y = lambda rk: TP + (min(rk, cap) - 1) / (cap - 1) * (H - TP - B)
    o = [f'<svg class="xl-svg" viewBox="0 0 {W} {H}" role="img" aria-label="Savant Rating rank by week for every team that has been in the top 10 this season.">']
    for rk in (1, 5, 10, 15, 20, 25):
        o.append(f'<line x1="{L}" x2="{W-R}" y1="{Y(rk):.1f}" y2="{Y(rk):.1f}" stroke="rgba(255,255,255,{.14 if rk == 10 else .05})"></line>'
                 f'<text x="{L-10}" y="{Y(rk)+4:.1f}" fill="#949a9f" font-size="12" text-anchor="end">{"25+" if rk == cap else rk}</text>')
    step = max(1, math.ceil(len(weeks) / 8))
    for w in weeks[::step]:
        o.append(f'<text x="{X(w):.1f}" y="{H-10}" fill="#949a9f" font-size="12" text-anchor="middle">Wk {w}</text>')

    def line(team, hot):
        pts = ' '.join(f'{X(w):.1f},{Y(rank[team][w]):.1f}' for w in weeks if w in rank[team])
        return (f'<polyline points="{pts}" fill="none" stroke="{BLUE if hot else "rgba(231,233,234,.28)"}" '
                f'stroke-width="{2.4 if hot else 1.5}" stroke-linejoin="round" stroke-linecap="round"><title>{_esc(team)}</title></polyline>')
    o += [line(t, False) for t in dropped] + [line(t, True) for t in final10]
    for t in final10:
        for w in weeks:
            if w in rank[t]:
                o.append(f'<circle cx="{X(w):.1f}" cy="{Y(rank[t][w]):.1f}" r="3.5" fill="#0a0b0d" stroke="{BLUE}" stroke-width="2"></circle>')
    labels = [(t, Y(rank[t][last]), True) for t in final10]
    if len(dropped) <= 8:   # beyond that the grey names only collide; the lines still show
        labels += [(t, Y(rank[t][last]), False) for t in dropped if last in rank[t]]
    labels.sort(key=lambda p: p[1])
    prev = -99
    for t, y, hot in labels:
        y = max(y, prev + 15)
        prev = y
        o.append(f'<text x="{W-R+10}" y="{y+4:.1f}" fill="{"#e7e9ea" if hot else "#949a9f"}" font-size="12" font-weight="{650 if hot else 400}">{_esc(t)}</text>')
    o.append('</svg>')
    movers = [t for t in final10 if weeks[0] in rank[t]]
    climb = max(movers, key=lambda t: rank[t][weeks[0]] - rank[t][last]) if movers else None
    return {'svg': ''.join(o), 'n': len(final10) + len(dropped), 'weeks': len(weeks), 'last': last,
            'climb': climb, 'from': rank[climb][weeks[0]] if climb else None, 'to': rank[climb][last] if climb else None}


# ── Portal flow ─────────────────────────────────────────────────────────────
GCOL = {'SEC': BLUE, 'Big Ten': '#4fb3f5', 'Big 12': '#7cc9fb', 'ACC': '#a9dcfc', 'Pac-12': '#cfe9fc', 'Group of 5': ORANGE, 'FCS & below': GREY}


def portal(moves, power):
    """moves: (origin conference, destination conference) for that season, None = not FBS."""
    def grp(conf):
        if conf in power:
            return conf
        return 'Group of 5' if conf else 'FCS & below'   # any other FBS conference, independents included
    GROUPS = list(power) + ['Group of 5', 'FCS & below']
    flows = Counter((grp(oc), grp(dc)) for oc, dc in moves)
    total = sum(flows.values())
    if not total:
        return None
    W, H, TP, B, nodew, gap = 560, 440, 10, 10, 14, 10
    out_tot = {g: sum(v for (a, _), v in flows.items() if a == g) for g in GROUPS}
    in_tot = {g: sum(v for (_, b), v in flows.items() if b == g) for g in GROUPS}
    k = (H - TP - B - gap * (len(GROUPS) - 1)) / total
    lx, rx = 120, W - 120

    def stack(tot):
        y, pos = TP, {}
        for g in GROUPS:
            pos[g] = y
            y += tot[g] * k + gap
        return pos
    ly, ry = stack(out_tot), stack(in_tot)
    lcur, rcur = dict(ly), dict(ry)
    o = [f'<svg class="xl-svg" viewBox="0 0 {W} {H}" role="img" aria-label="Where {total:,} transfers left from, on the left, and where they landed, on the right, by conference group.">']
    mx = (lx + nodew + rx) / 2
    for a in GROUPS:
        for b in GROUPS:
            v = flows[(a, b)]
            if not v:
                continue
            h = v * k
            y0, y1 = lcur[a], rcur[b]
            lcur[a] += h
            rcur[b] += h
            up = a not in power and b in power
            o.append(f'<path d="M{lx+nodew},{y0:.1f} C{mx:.1f},{y0:.1f} {mx:.1f},{y1:.1f} {rx},{y1:.1f} L{rx},{y1+h:.1f} '
                     f'C{mx:.1f},{y1+h:.1f} {mx:.1f},{y0+h:.1f} {lx+nodew},{y0+h:.1f} Z" '
                     f'fill="{BLUE if up else "rgba(231,233,234,.16)"}" fill-opacity="{.55 if up else 1}"><title>{a} to {b}: {v:,}</title></path>')
    for g in GROUPS:
        for x, tot, pos, anchor, dx, word in ((lx, out_tot, ly, 'end', -10, 'out'), (rx, in_tot, ry, 'start', nodew + 10, 'in')):
            if not tot[g]:
                continue
            mid = pos[g] + tot[g] * k / 2
            o.append(f'<rect x="{x}" y="{pos[g]:.1f}" width="{nodew}" height="{max(tot[g]*k, 1):.1f}" rx="3" fill="{GCOL[g]}"></rect>'
                     f'<text x="{x+dx}" y="{mid+4:.1f}" fill="#e7e9ea" font-size="13" font-weight="650" text-anchor="{anchor}">{g}</text>'
                     f'<text x="{x+dx}" y="{mid+19:.1f}" fill="#949a9f" font-size="11" text-anchor="{anchor}">{tot[g]:,} {word}</text>')
    o.append('</svg>')
    p4_in = sum(in_tot[g] for g in power)
    up = sum(v for (a, b), v in flows.items() if b in power and a not in power)
    return {'svg': ''.join(o), 'total': total, 'p4_in': p4_in, 'up': up,
            'up_pct': round(up / p4_in * 100) if p4_in else 0}


# ── Stadium map ─────────────────────────────────────────────────────────────
def stadiums(venues, net):
    geo = outline()
    pts = []
    hb = geo['hawaii_bbox']
    for team, lat, lon, cap in venues:
        if team not in net or lat is None or lon is None:
            continue
        if lon < -150:   # Hawaii sits in the drawn inset, not at its true longitude
            x = hb[0] + (lon + 160.25) / 5.45 * (hb[1] - hb[0])
            y = hb[2] + (22.25 - lat) / 3.35 * (hb[3] - hb[2])
        elif lon < -130:
            continue
        else:
            x, y = albers(lon, lat)
        pts.append((2.5 + math.sqrt((cap or 20000) / 107000) * 10.5, x, y, team, cap or 0))
    if not pts:
        return None
    vals = [net[p[3]] for p in pts]
    lo, hi = min(vals), max(vals)

    def col(v):
        f = (v - lo) / (hi - lo) if hi > lo else .5
        a, b, c = (58, 63, 69), (28, 156, 240), (207, 233, 252)
        p, q, g = (a, b, f / .7) if f < .7 else (b, c, (f - .7) / .3)
        return '#%02x%02x%02x' % tuple(round(p[i] + (q[i] - p[i]) * g) for i in range(3))
    o = ['<svg class="xl-svg" viewBox="0 0 975 610" role="img" aria-label="Map of the United States with every FBS home stadium placed by location, sized by capacity and shaded by the home team&#39;s Savant Net Rating.">',
         f'<path d="{geo["land"]}" fill="#101215" stroke="rgba(231,233,234,.42)" stroke-width="1.1" stroke-linejoin="round"></path>',
         f'<path d="{geo["borders"]}" fill="none" stroke="rgba(255,255,255,.1)" stroke-width=".6"></path>']
    for r, x, y, team, cap in sorted(pts, reverse=True):
        o.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" fill="{col(net[team])}" fill-opacity=".88" stroke="#000" stroke-width="1"><title>{_esc(team)}: {cap:,} seats, {net[team]:+.1f}</title></circle>')
    for r, x, y, team, cap in sorted(pts, key=lambda p: -p[4])[:6]:
        o.append(f'<text x="{x+r+5:.1f}" y="{y+4:.1f}" fill="#fff" font-size="11" font-weight="650" stroke="#000" stroke-width="3.5" paint-order="stroke">{_esc(team)} {cap/1000:.0f}k</text>')
    o.append('</svg>')
    return {'svg': ''.join(o), 'n': len(pts), 'n100': sum(1 for p in pts if p[4] >= 100000)}


# ── The era ─────────────────────────────────────────────────────────────────
def era(rows, partial_season):
    rows = [(int(s), float(p), float(c) * 100) for s, p, c in rows]
    if len(rows) < 2:
        return None
    s0, s1 = rows[0][0], rows[-1][0]
    full = [r for r in rows if r[0] != partial_season] or rows
    lo = math.floor(min(p for _, p, _ in rows) / 2) * 2 - 2
    hi = math.ceil(max(p for _, p, _ in rows) / 2) * 2 + 2
    W, H, L, R, TP, B = 560, 360, 44, 20, 20, 34
    X = lambda s: L + (s - s0) / max(s1 - s0, 1) * (W - L - R)
    Y = lambda v: TP + (hi - v) / (hi - lo) * (H - TP - B)
    o = [f'<svg class="xl-svg" viewBox="0 0 {W} {H}" role="img" aria-label="Average combined points in FBS-vs-FBS games by season, {s0} to {s1}.">',
         '<defs><linearGradient id="xl-era" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#1c9cf0" stop-opacity=".3"></stop><stop offset="1" stop-color="#1c9cf0" stop-opacity="0"></stop></linearGradient></defs>']
    for v in range(lo, hi + 1, 2):
        o.append(f'<line x1="{L}" x2="{W-R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="rgba(255,255,255,.05)"></line>'
                 f'<text x="{L-10}" y="{Y(v)+4:.1f}" fill="#949a9f" font-size="12" text-anchor="end">{v}</text>')
    for s, _, _ in rows:
        if (s - s0) % 2 == 0:
            o.append(f'<text x="{X(s):.1f}" y="{H-10}" fill="#949a9f" font-size="12" text-anchor="middle">{s}</text>')
    o.append(f'<path d="M{X(full[0][0]):.1f},{Y(lo):.1f} ' + ' '.join(f'L{X(s):.1f},{Y(p):.1f}' for s, p, _ in full)
             + f' L{X(full[-1][0]):.1f},{Y(lo):.1f} Z" fill="url(#xl-era)"></path>')
    o.append('<polyline points="' + ' '.join(f'{X(s):.1f},{Y(p):.1f}' for s, p, _ in full) + f'" fill="none" stroke="{BLUE}" stroke-width="2.5" stroke-linejoin="round"></polyline>')
    for s, p, _ in rows:
        o.append(f'<circle cx="{X(s):.1f}" cy="{Y(p):.1f}" r="{5 if s in (s0, full[-1][0]) else 3.5}" fill="{"#0a0b0d" if s == partial_season else BLUE}" stroke="{BLUE}" stroke-width="2"><title>{s}: {p:.1f}</title></circle>')
    first, lastf = full[0], full[-1]
    o.append(f'<text x="{X(first[0])+10:.1f}" y="{Y(first[1])-10:.1f}" fill="#e7e9ea" font-size="13" font-weight="650">{first[1]:.1f}</text>'
             f'<text x="{X(lastf[0]):.1f}" y="{Y(lastf[1])+24:.1f}" fill="#e7e9ea" font-size="13" font-weight="650" text-anchor="middle">{lastf[1]:.1f}</text>')
    if partial_season == s1:
        o.append(f'<text x="{X(s1)-8:.1f}" y="{Y(rows[-1][1])-12:.1f}" fill="#9aa0a6" font-size="12" text-anchor="end">{s1} so far</text>')
    o.append('</svg>')
    bars = [{'season': s, 'pct': c, 'h': max(0, min(100, (c - 30) / 15 * 100))} for s, _, c in rows]
    return {'svg': ''.join(o), 'first': first, 'last': lastf, 'bars': bars,
            'close_lo': min(c for _, _, c in rows), 'close_hi': max(c for _, _, c in rows)}


# ── Where the talent lives ──────────────────────────────────────────────────
def talent(rows):
    by = defaultdict(dict)
    for season, g, avg in rows:
        by[g][int(season)] = float(avg)
    if 'SEC' not in by or 'Group of 5' not in by or len(by['SEC']) < 2:
        return None
    seasons = sorted(set(by['SEC']) & set(by['Group of 5']))
    G = ['SEC', 'Big Ten', 'ACC', 'Big 12', 'Group of 5']
    col = {'SEC': BLUE, 'Big Ten': '#7cc9fb', 'ACC': 'rgba(231,233,234,.45)', 'Big 12': 'rgba(231,233,234,.3)', 'Group of 5': ORANGE}
    allv = [by[g][s] for g in G for s in seasons if s in by[g]]
    lo, hi = math.floor(min(allv) / 100) * 100, math.ceil(max(allv) / 100) * 100
    W, H, L, R, TP, B = 560, 360, 44, 110, 14, 34
    X = lambda s: L + (s - seasons[0]) / max(seasons[-1] - seasons[0], 1) * (W - L - R)
    Y = lambda v: TP + (hi - v) / (hi - lo) * (H - TP - B)
    o = [f'<svg class="xl-svg" viewBox="0 0 {W} {H}" role="img" aria-label="Average roster talent per team by conference group, {seasons[0]} to {seasons[-1]}.">']
    for v in range(lo, hi + 1, 100):
        o.append(f'<line x1="{L}" x2="{W-R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="rgba(255,255,255,.05)"></line>'
                 f'<text x="{L-10}" y="{Y(v)+4:.1f}" fill="#949a9f" font-size="12" text-anchor="end">{v}</text>')
    for s in seasons:
        if (s - seasons[0]) % 3 == 0 or s == seasons[-1]:
            o.append(f'<text x="{X(s):.1f}" y="{H-10}" fill="#949a9f" font-size="12" text-anchor="middle">{s}</text>')
    if seasons[0] < 2021 <= seasons[-1]:
        o.append(f'<rect x="{X(2021):.1f}" y="{TP}" width="{X(seasons[-1]) - X(2021):.1f}" height="{H-TP-B}" fill="rgba(240,168,104,.05)"></rect>'
                 f'<text x="{X(2021)+8:.1f}" y="{TP+16}" fill="#c9a07a" font-size="12">Portal era</text>')
    ends = []
    for g in G:
        ss = [s for s in seasons if s in by[g]]
        if not ss:
            continue
        pts = ' '.join(f'{X(s):.1f},{Y(by[g][s]):.1f}' for s in ss)
        hot = g in ('SEC', 'Group of 5')
        o.append(f'<polyline points="{pts}" fill="none" stroke="{col[g]}" stroke-width="{2.6 if hot else 1.8}" stroke-linejoin="round"><title>{g}</title></polyline>')
        ends.append([Y(by[g][ss[-1]]), g, by[g][ss[-1]]])
    ends.sort()
    for i in range(1, len(ends)):
        ends[i][0] = max(ends[i][0], ends[i - 1][0] + 15)
    for y, g, v in ends:
        o.append(f'<text x="{W-R+10}" y="{y+4:.1f}" fill="#e7e9ea" font-size="12" font-weight="650">{g} {v:.0f}</text>')
    o.append('</svg>')
    g5 = (by['Group of 5'][seasons[0]], by['Group of 5'][seasons[-1]])
    sec = (by['SEC'][seasons[0]], by['SEC'][seasons[-1]])
    return {'svg': ''.join(o), 'first': seasons[0], 'last': seasons[-1], 'g5': g5, 'sec': sec,
            'g5_pct': round((g5[1] / g5[0] - 1) * 100), 'sec_pct': round((sec[1] / sec[0] - 1) * 100)}


# ── Queries ─────────────────────────────────────────────────────────────────
def power_confs(season):
    """The power conferences that season: the Pac-12 counts through 2023, its last as one."""
    return P4 + (('Pac-12',) if season <= 2023 else ())


def build(cursor, season, conf_map):
    """Every landscape chart for one season, or None if the season has no ratings.

    conf_map: {team: conference} for that season's FBS teams, realignment-correct
    (main._team_confs_for_season) — today's `teams.conference` would put 2023
    Oregon State in the Group of 5."""
    cursor.execute('SELECT team, net_rating FROM savant_ratings WHERE season = %s AND net_rating IS NOT NULL', (season,))
    teams = [{'team': t, 'conf': conf_map[t], 'net': float(v)} for t, v in cursor.fetchall() if t in conf_map]
    if not teams:
        return None
    net = {t['team']: t['net'] for t in teams}
    power = power_confs(season)
    out = {'season': season, 'power': power, 'power_label': 'power four' if len(power) == 4 else 'power five',
           'conf': conf_strips(teams, power)}

    cursor.execute('SELECT week, team, net_ranking FROM savant_weekly WHERE season = %s AND net_ranking IS NOT NULL', (season,))
    out['river'] = river(cursor.fetchall())

    cursor.execute('SELECT origin, destination FROM transfers WHERE year = %s AND destination IS NOT NULL', (season,))
    out['portal'] = portal([(conf_map.get(o), conf_map.get(d)) for o, d in cursor.fetchall()], power)

    # Each team's most-used home venue over the last two seasons (neutral sites out).
    cursor.execute('''WITH hv AS (
                          SELECT g.home_team AS team, w.venue_id, COUNT(*) AS n
                          FROM games g JOIN game_weather w ON w.game_id = g.id
                          WHERE g.season BETWEEN %s AND %s AND COALESCE(g.neutral_site, 0) = 0
                          GROUP BY 1, 2),
                      best AS (SELECT DISTINCT ON (team) team, venue_id FROM hv ORDER BY team, n DESC)
                      SELECT b.team, v.latitude, v.longitude, v.capacity
                      FROM best b JOIN venues v ON v.id = b.venue_id''', (season - 1, season))
    out['map'] = stadiums(cursor.fetchall(), net)

    fbs = '(SELECT DISTINCT team AS t, season AS s FROM sp_ratings)'
    cursor.execute(f'''SELECT g.season, AVG(g.home_points + g.away_points),
                              AVG((ABS(g.home_points - g.away_points) <= 8)::int)
                       FROM games g
                       JOIN {fbs} h ON h.t = g.home_team AND h.s = g.season
                       JOIN {fbs} a ON a.t = g.away_team AND a.s = g.season
                       WHERE g.home_points IS NOT NULL AND g.season <= %s
                       GROUP BY 1 ORDER BY 1''', (season,))
    era_rows = cursor.fetchall()
    cursor.execute('SELECT 1 FROM games WHERE season = %s AND home_points IS NULL LIMIT 1', (season,))
    out['era'] = era(era_rows, season if cursor.fetchone() else None)

    cursor.execute('''SELECT tt.season,
                             CASE WHEN t.conference IN ('SEC', 'Big Ten', 'Big 12', 'ACC') THEN t.conference
                                  ELSE 'Group of 5' END,
                             AVG(tt.talent)
                      FROM team_talent tt JOIN teams t ON t.name = tt.team
                      WHERE tt.season <= %s AND t.conference IN %s
                      GROUP BY 1, 2''', (season, P4 + G5))
    out['talent'] = talent(cursor.fetchall())
    return out


# ── Transfer portal: when the class moved (/transfers) ──────────────────────
def portal_weeks(rows, year, total):
    """Entries per week for one transfer class, as an SVG bar chart.

    rows: (week start date, count). Returns None for a class with no entry
    dates (the pre-portal years are reconstructed from rosters). The finding is
    the peak week and the share that came in the busiest calendar month.
    """
    import datetime as dt
    wk = [(w, n) for w, n in rows if w is not None]
    if len(wk) < 3 or not total:
        return None
    d0, d1 = wk[0][0], wk[-1][0]
    span = max((d1 - d0).days, 7)
    W, H, L, R, T, B = 1180, 300, 48, 20, 24, 40
    X = lambda d: L + (d - d0).days / span * (W - L - R)
    mx = max(n for _, n in wk)
    step = 500 if mx > 1500 else (100 if mx > 300 else 20)
    top = math.ceil(mx / step) * step
    Y = lambda n: T + (1 - n / top) * (H - T - B)
    bw = max(4, (W - L - R) / (span / 7) * .78)
    peak = max(wk, key=lambda p: p[1])
    o = [f'<svg class="xl-svg" viewBox="0 0 {W} {H}" role="img" aria-label="Portal entries per week for the {year} class, peaking at {peak[1]:,} in the week of {peak[0].strftime("%B %-d")}.">']
    for v in range(0, top + 1, step):
        o.append(f'<line x1="{L}" x2="{W-R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="rgba(255,255,255,{.12 if v == 0 else .05})"></line>'
                 f'<text x="{L-10}" y="{Y(v)+4:.1f}" fill="#949a9f" font-size="12" text-anchor="end">{v:,}</text>')
    m = dt.date(d0.year, d0.month, 1)
    while m <= d1:
        if m >= d0:
            o.append(f'<text x="{X(m):.1f}" y="{H-12}" fill="#949a9f" font-size="12" text-anchor="middle">{m.strftime("%b")}</text>')
        m = dt.date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    hot = peak[1] * .2
    for d, n in wk:
        o.append(f'<rect x="{X(d)-bw/2:.1f}" y="{Y(n):.1f}" width="{bw:.1f}" height="{max(Y(0)-Y(n), 1):.1f}" rx="3" '
                 f'fill="{BLUE if n >= hot else "rgba(28,156,240,.35)"}"><title>Week of {d.strftime("%b %-d")}: {n:,}</title></rect>')
    anchor = 'end' if X(peak[0]) > W * .7 else 'start'
    dx = -(bw / 2 + 10) if anchor == 'end' else bw / 2 + 10
    o.append(f'<text x="{X(peak[0])+dx:.1f}" y="{Y(peak[1])+14:.1f}" fill="#e7e9ea" font-size="14" font-weight="700" text-anchor="{anchor}">'
             f'{peak[1]:,} entered the week of {peak[0].strftime("%b %-d")}</text>')
    o.append('</svg>')
    months = Counter()
    for d, n in wk:
        months[(d + dt.timedelta(days=3)).strftime('%B')] += n   # a week counts toward the month its midpoint falls in
    month, in_month = months.most_common(1)[0]
    return {'svg': ''.join(o), 'peak_n': peak[1], 'peak_week': peak[0].strftime('%b %-d'),
            'month': month, 'month_pct': round(in_month / total * 100)}
