"""Who counts as FBS — the one definition, shared by the site and the pipeline.

Saturday Savant is an FBS site. Every leaderboard, percentile pool, standings
table and rank filters to FBS, and the pages say so in the caption. The upstream
feeds do not: CFBD's season endpoints return every division in one payload, and
nothing in the ingest ever looked at which one a row came from. Half of the
2026 player_stats rows were FCS and D-II players no page can reach — 290k rows
across the loaded seasons, about a quarter of the table, for programs the site
does not cover.

So the same rule now runs on both sides: main.py filters reads with FCS_CONFS,
and the ingest scripts filter writes with keep_stat_row(). A row is stored when

  • the team is FBS that season, or
  • the player already has a page here (an id in `players`, which is built from
    FBS rosters) — that keeps a career log complete for someone who moved down
    a division, and for the FCS seasons of the 685 players now on FBS rosters.

What that costs: an FCS player who transfers up in a LATER season arrives with
no pre-transfer seasons, where today he would carry them. Recovering one is a
re-run of backfill/backfill_history.py for that year — by then he is in
`players`, so the same filter keeps him.
"""

FCS_CONFS = ('CAA','Big Sky','MVFC','SWAC','MEAC','Southland','Big South','OVC',
             'Big South-OVC','Southern','UAC','Patriot','NEC','Pioneer','Ivy',
             'FCS Independents','SIAC')

# A teams table that has lost its rows would make every name look non-FBS and
# quietly reduce an ingest to nothing. Below this count the caller raises
# instead of writing a skeleton season.
MIN_FBS_TEAMS = 100


def fbs_team_names(cursor):
    """Team names outside the FCS conferences, from the teams table.

    Raises if the table is too short to be believable — a filter that silently
    matches nothing is worse than a failed run, because the run that follows it
    would report success over an empty season.
    """
    cursor.execute('SELECT name, conference FROM teams WHERE conference IS NOT NULL')
    names = {n for n, c in cursor.fetchall() if n and c not in FCS_CONFS}
    if len(names) < MIN_FBS_TEAMS:
        raise RuntimeError(
            f'teams table yielded only {len(names)} FBS names — refusing to '
            f'filter an ingest against it')
    return names


def tracked_player_ids(cursor):
    """Every player id the site has a page for, as TEXT.

    `players` is built from FBS rosters and box scores, so membership here is
    the "we show this person" test. player_stats.player_id is TEXT while
    players.id is an integer — the cast belongs in one place, not in each
    caller, because a missed one silently matches nothing.
    """
    cursor.execute('SELECT id::text FROM players')
    return {r[0] for r in cursor.fetchall()}


def keep_stat_row(team, player_id, fbs_names, tracked_ids):
    """Should this upstream stat row be stored? See the module docstring."""
    return team in fbs_names or (player_id is not None and
                                 str(player_id) in tracked_ids)


# keep_stat_row as SQL, for counting what is already stored. The short-payload
# guards compare a filtered read against a stored count, and a stored count
# taken over every division would be up to twice the read — enough to trip the
# guard on every run and quietly stop the refresh. Both sides count the same
# population or neither number means anything.
STORED_ROW_FILTER = """
    (EXISTS (SELECT 1 FROM teams t
              WHERE t.name = {alias}.team AND t.conference IS NOT NULL
                AND t.conference NOT IN %s)
     OR EXISTS (SELECT 1 FROM players pl
                 WHERE pl.id::text = {alias}.player_id::text))
"""


def count_stored(cursor, table, season, alias='x'):
    """How many rows this table already holds for a season that the ingest
    filter would keep."""
    cursor.execute(
        f'SELECT count(*) FROM {table} {alias} WHERE {alias}.season = %s '
        f'AND {STORED_ROW_FILTER.format(alias=alias)}',
        (season, FCS_CONFS))
    return cursor.fetchone()[0]
