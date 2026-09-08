"""Single source of truth for which season the data pipeline ingests.

College football's "season year" is the year the season kicks off in the fall;
a game played the following January (bowls / CFP) still belongs to that fall's
season. So the active season increments in February, once the prior season's
CFP has concluded — February through December map to that calendar year, and
January maps to the year before.

The weekly fetch/compute scripts call current_cfb_season() so they always ingest
the active season with no hardcoded year: 2026 now, 2027 next year, and so on,
automatically.

Note: the web app's *display* default is deliberately separate. It derives from
which season actually has stats loaded (CURRENT_SEASON in main.py), so it only
advances once the new season has produced data — during the offseason the site
keeps showing the last completed season even though the pipeline has already
rolled over to ingest the next one.
"""
from datetime import date


def current_cfb_season(today=None):
    """The active CFB season year. Feb–Dec -> that year; Jan -> prior year."""
    d = today or date.today()
    return d.year if d.month >= 2 else d.year - 1


# ── Week 0 ──────────────────────────────────────────────────────────────────
# College football opens with a handful of games ("Week 0") the Saturday before
# the real opening weekend. Neither CFBD nor ESPN labels them: CFBD returns them
# as week 1 and ignores a `week=0` argument entirely, so every season in this
# database arrived with those games folded into week 1. A team that played both
# therefore showed two "WK 1" rows on its schedule.
#
# Week 0 has to be derived, and the only reliable signal is the calendar. Every
# season's week-1 games form one tight cluster spanning Thursday to Monday of
# the opening weekend; a Week 0 game sits alone 4-6 days before it. So: sort the
# distinct kickoff dates and split at the first gap of three days or more.
#
#   2026:  08-29 | 09-03 09-04 09-05 09-06 09-07     -> 8 games in week 0
#   2025:  08-23 | 08-28 08-29 08-30 08-31 09-01     -> 5
#   2020:  09-03 09-05 09-07                          -> none (COVID, no week 0)
#
# Three days is comfortably clear of the 1-2 day gaps inside an opening weekend
# and comfortably under the 4-6 day gap that separates a real Week 0.
WEEK_ZERO_GAP_DAYS = 3


def week_zero_dates(kickoff_dates):
    """The subset of `kickoff_dates` (date objects, one season's week-1 games)
    that belong to Week 0 — empty when the season has none.

    Splits at the first gap of WEEK_ZERO_GAP_DAYS or more. Returns a set so the
    caller can partition its own rows by date.
    """
    days = sorted(set(kickoff_dates))
    for i in range(1, len(days)):
        if (days[i] - days[i - 1]).days >= WEEK_ZERO_GAP_DAYS:
            return set(days[:i])
        # Only the FIRST gap can open Week 0. A later gap is just a quiet
        # midweek inside the opening stretch, and splitting there would
        # relabel most of week 1.
        break
    return set()
