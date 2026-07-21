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
