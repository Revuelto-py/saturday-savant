"""Self-check for the CFP committee ranking on /rankings and /bracket.

Runs against the database, so it holds whatever week it is: invariants, not
this week's teams. python3 tools/check_cfp_views.py (needs .env).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402

main._RL_MAX = 0
conn = main.get_db()
try:
    cur = conn.cursor()
    cur.execute('SELECT MAX(season) FROM cfp_rankings')
    season = cur.fetchone()[0]
    seeds, entries, board = main._project_cfp_field(cur, season)
finally:
    main.release_db(conn)

# The board: the committee's top 25, twelve seeds among them or below them.
assert board and [b['rank'] for b in board] == sorted(b['rank'] for b in board), 'board is in rank order'
seeded = [b for b in board if b['seed']]
assert len(seeded) <= 12 and len(seeds) == 12, 'twelve seeds'
first_out = [b for b in board if b['first_out']]
assert len(first_out) <= 4 and all(not b['seed'] for b in first_out), 'first four out are out'
assert [b['rank'] for b in first_out] == sorted(b['rank'] for b in board if not b['seed'])[:len(first_out)]
last_in = [b for b in board if b['last_in']]
at_large = {e['team'] for e in entries if not e['auto']}
assert len(last_in) <= 4 and all(b['team'] in at_large for b in last_in), 'last four in are at-large'
assert all(b['seed'] and b['rank'] > 12 for b in board if b['auto']), 'auto tag only on a bid the ranking missed'

# /rankings: the committee's ranking leads while it is the live one; an old ?w= link stays AP.
c = main.app.test_client()
body = c.get(f'/rankings?season={season}&poll=cfp').get_data(as_text=True)
assert 'CFP Rankings' in body and body.count('rkp-pts is-ap') == len(board), 'committee view, AP beside each team'
assert 'Others receiving votes' not in body, 'AP-only section stays out'
ap = c.get(f'/rankings?season={season}&w=regular-1').get_data(as_text=True)
assert '<h1 class="rkp-h1">AP Top 25' in ap, 'an old ?w= link is an AP link'
print(f'cfp views: ok ({season}, {len(board)} ranked, {len(first_out)} first out)')
