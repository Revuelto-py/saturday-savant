"""Self-check for /bracket: one layout for every playoff season.

Runs against the database, so it checks invariants, not this year's teams:
python3 tools/check_bracket_views.py (needs .env).
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402

main._RL_MAX = 0
conn = main.get_db()
try:
    cur = conn.cursor()
    cur.execute('SELECT DISTINCT season FROM cfp_seeds')
    seeded = {r[0] for r in cur.fetchall()}
    cur.execute('SELECT DISTINCT season FROM cfp_rankings')
    ranked = {r[0] for r in cur.fetchall()}
finally:
    main.release_db(conn)

c = main.app.test_client()
seasons = sorted(seeded | {main.CURRENT_SEASON}, reverse=True)
problems = []
for s in seasons:
    main.cache.clear()
    html = c.get(f'/bracket?season={s}').get_data(as_text=True)
    four = s < main.FIRST_12_TEAM_SEASON
    desk = html.split('class="bkx-desk"')[1].split('class="bkx-ph"')[0]
    # Same page for every season: the CFP mark, every season in the strip, the
    # source strip, a full bracket on both layouts, the champion card.
    assert 'cfp-mark.webp' in html and html.count('class="rkp-chip') == len(seasons), f'{s}: header and season strip'
    assert 'class="rkp-card bkx-src"' in html, f'{s}: where the field comes from'
    assert html.count('class="bkx-card ') == (6 if four else 22), f'{s}: three or eleven games, desktop and phone'
    assert html.count('class="bkx-champ"') == 2, f'{s}: a champion card on both layouts'
    groups = re.findall(r'<h3 class="bkx-fb-ch">([^<]+)<', html)
    assert groups and groups[0] == ('The top four' if four else 'Automatic bids'), f'{s}: field groups {groups}'
    if s in ranked:
        assert groups[-1] == 'First four out' and 'class="bkx-fb-rank"' in html, f'{s}: ranks and the first four out'
    # Each seed's logo carries one transition name, only while the field is projected.
    names = re.findall(r'view-transition-name: (seed-\d+)', desk)
    if s in seeded:
        assert not names, f'{s}: a played bracket repeats teams, so it names none'
    else:
        assert sorted(names) == sorted(f'seed-{n}' for n in range(1, 13)), f'{s}: twelve seeds, named once'
        continue
    # A played season's title game is two different teams, and its champion is one of them.
    nc = desk.split('National Championship</div>')[1].split('class="bkx-col"')[0]
    finalists = re.findall(r'class="bkx-tn"[^>]*>([^<]+)<', nc)
    champ = re.findall(r'class="bkx-champ-t"[^>]*>([^<]+)<', desk)
    if len(set(finalists)) != 2 or champ[0] not in finalists:
        problems.append(f'{s}: title game {finalists}, champion {champ}')

assert not problems, 'bad brackets (fix the seeds: python3 backfill/backfill_cfp.py <season>): ' + '; '.join(problems)
print(f'bracket views: ok ({len(seasons)} seasons, {len(ranked & set(seasons))} with committee ranks)')
