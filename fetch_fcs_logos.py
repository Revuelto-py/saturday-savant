"""Add minimal `teams` rows for the FCS programs that appear as opponents on
FBS schedules, purely so their logo (and colors) render on schedule/game
pages. These are NOT full team entries — no roster, no stats — and they are
kept out of FBS-only pages (Teams grid, Rankings, Leaderboards) by their FCS
`conference` value being listed in main.FCS_CONFS.

Scope: only the FCS teams actually present in our games table with no matching
`teams` row (see the query below) — not all of FCS.

Source: CFBD get_teams(2025), the same provider used for FBS logos in
fetch_dark_logos.py. It returns ESPN-CDN logo URLs for FCS teams in the same
[regular, dark] shape, plus conference/color/abbreviation, so the FBS logo
logic is reused directly. logo_dark falls back to the regular logo when a team
has no dark variant (matching fetch_dark_logos.py's behavior).

Connection: uses main.get_db()/release_db() with try/finally, per project
convention for DB access.
"""

import os

import cfbd
from dotenv import load_dotenv

from main import get_db, release_db, FCS_CONFS, slugify_team

load_dotenv()

MISSING_SQL = '''
    WITH game_teams AS (
        SELECT home_team AS t FROM games WHERE home_team IS NOT NULL
        UNION
        SELECT away_team AS t FROM games WHERE away_team IS NOT NULL
    )
    SELECT gt.t
    FROM game_teams gt
    LEFT JOIN teams te ON te.name = gt.t
    WHERE te.name IS NULL
    ORDER BY gt.t
'''


def main():
    conn = get_db()
    try:
        cursor = conn.cursor()

        cursor.execute(MISSING_SQL)
        missing = [r[0] for r in cursor.fetchall()]
        print(f'{len(missing)} FCS opponents missing from teams:')
        for m in missing:
            print('   ', m)

        # CFBD covers all divisions via get_teams(); index by exact school name.
        # Fetched unconditionally — even with nothing missing, the correction
        # pass below still needs the classification data.
        cfg = cfbd.Configuration(access_token=os.getenv('CFBD_API_KEY'))
        with cfbd.ApiClient(cfg) as api:
            all_teams = cfbd.TeamsApi(api).get_teams(year=2025)
        by_name = {t.school: t for t in all_teams}
        classification = {t.school: (str(t.classification).lower() if t.classification else '')
                          for t in all_teams}

        cursor.execute('SELECT COALESCE(MAX(id), 0) FROM teams')
        next_id = cursor.fetchone()[0] + 1

        inserted, updated, unmatched = 0, 0, []
        for name in missing:
            t = by_name.get(name)
            if not t:
                unmatched.append(name)
                continue
            logos = getattr(t, 'logos', []) or []
            # Store https:// (ESPN serves http://) to avoid mixed-content.
            logos = [l.replace('http://', 'https://') if l else l for l in logos]
            logo = logos[0] if logos else None
            logo_dark = logos[1] if len(logos) > 1 else logo  # fall back to regular

            # Idempotent upsert keyed on name (teams has no unique index on
            # name, so UPDATE first and INSERT only when nothing matched).
            cursor.execute('''
                UPDATE teams
                   SET conference=%s, abbreviation=%s, logo=%s, logo_dark=%s,
                       color=%s, alt_color=%s
                 WHERE name=%s
            ''', (t.conference, t.abbreviation, logo, logo_dark,
                  t.color, t.alternate_color, name))
            if cursor.rowcount == 0:
                cursor.execute('''
                    INSERT INTO teams
                        (id, name, slug, conference, abbreviation, logo, logo_dark, color, alt_color)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ''', (next_id, name, slugify_team(name), t.conference, t.abbreviation,
                      logo, logo_dark, t.color, t.alternate_color))
                next_id += 1
                inserted += 1
            else:
                updated += 1

        # Correct any PRE-EXISTING teams row that CFBD classifies as FCS but
        # that is stored with a non-FCS (FBS) conference — these leak onto
        # FBS-only pages. Found in the wild: North Dakota State ('Mountain
        # West') and Sacramento State ('Mid-American'), both FCS programs that
        # carry only transfer-era player_stats (no games/roster). Reset them to
        # their real FCS conference (authoritative per CFBD) so FCS_CONFS
        # excludes them. Data-driven, so it also handles any future stragglers.
        cursor.execute('SELECT name, conference FROM teams')
        corrected = []
        for name, conf in cursor.fetchall():
            if 'fcs' in classification.get(name, '') and conf not in FCS_CONFS:
                real = by_name[name].conference
                cursor.execute('UPDATE teams SET conference=%s WHERE name=%s', (real, name))
                corrected.append((name, conf, real))

        conn.commit()

        if corrected:
            print('\nCorrected pre-existing FCS teams mislabeled with an FBS conference:')
            for name, old, new in corrected:
                print(f'   {name!r}: {old!r} -> {new!r}')

        confs = {}
        for name in missing:
            t = by_name.get(name)
            if t:
                confs[t.conference] = confs.get(t.conference, 0) + 1

        print(f'\nInserted {inserted}, updated {updated} FCS team rows.')
        print('By conference:', dict(sorted(confs.items(), key=lambda kv: -kv[1])))
        if unmatched:
            print(f'\nUNMATCHED in CFBD ({len(unmatched)}) — need a manual logo source:')
            for n in unmatched:
                print('   ', n)
        # Any FCS conference here that FCS_CONFS doesn't list would leak onto
        # FBS-only pages — surface it so FCS_CONFS can be updated.
        leaky = sorted({c for c in confs if c not in FCS_CONFS})
        if leaky:
            print(f'\n⚠ Conferences NOT in FCS_CONFS (would leak onto FBS pages): {leaky}')
        else:
            print('\nAll inserted conferences are covered by FCS_CONFS ✓')
    finally:
        release_db(conn)


if __name__ == '__main__':
    main()
