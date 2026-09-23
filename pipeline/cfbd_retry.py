"""Retry wrapper for CFBD reads.

CFBD sits behind Cloudflare, and whenever its origin restarts Cloudflare answers
with a 5xx instead. The body says as much itself — `"retryable": true` with a
`Retry-After` — but a single one of those killed a whole cron run, because the
scripts call the generated client directly and it raises on the first failure.

That is what made the scores job fail every night: it runs every ten minutes
(144 times a day) and only the 05:00 UTC run died, landing on the same upstream
window each time. One retry rides straight through it.

What is and is not retried matters. A 5xx, a 429 or a timeout is worth asking
again for; a 401 or a 404 will answer the same way forever, so those are raised
immediately rather than slept on four times. Exhausting the attempts raises
`UpstreamUnavailable`, which callers can treat as "skip this run" rather than
crash — the distinction a bare `except Exception` cannot make.

Delays stay inside the caller's schedule on purpose: four attempts at 15s, 30s
and 60s is at most ~105s of sleeping, well under the ten minutes before the
scores job runs again.

    from cfbd_retry import call_with_retry, UpstreamUnavailable

    try:
        games = call_with_retry('games', games_api.get_games, SEASON)
    except UpstreamUnavailable as exc:
        print(exc)          # next run picks it up

That is a sibling import, and it resolves because every one of these scripts is
run as a file — `python3 pipeline/fetch_data.py`, the form run_weekly.sh and
both cron jobs use — which puts this directory on sys.path. Running one as
`python3 -m pipeline.fetch_data` instead would not find it; pipeline/ is a
directory of scripts, not a package, and nothing invokes it that way.
"""
import random
import time

from cfbd.exceptions import ApiException

# Worth asking again for: the origin is busy, restarting, or rate-limiting.
# Anything else (401 bad key, 403, 404, 400) answers identically every time.
TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

DEFAULT_ATTEMPTS = 4
DEFAULT_BASE_DELAY = 15      # seconds; doubles each attempt
DEFAULT_MAX_DELAY = 60       # ceiling, and also the cap on a server Retry-After

# Connection-level failures never reach ApiException — they come out of urllib3
# directly. Imported defensively so this module still loads if that changes.
try:                                              # pragma: no cover
    from urllib3.exceptions import HTTPError as _Urllib3HTTPError
    _RETRYABLE = (ApiException, _Urllib3HTTPError)
except Exception:                                 # pragma: no cover
    _RETRYABLE = (ApiException,)


class UpstreamUnavailable(RuntimeError):
    """CFBD stayed unreachable for every attempt.

    Distinct from a programming error so a caller can decide that a run with no
    upstream is a skip, not a crash.
    """

    def __init__(self, label, attempts, last):
        super().__init__(
            f'{label}: CFBD unavailable after {attempts} attempt(s) — {last}')
        self.label = label
        self.attempts = attempts
        self.last = last


def _status_of(exc):
    """HTTP status if the exception carries one, else None (connection error)."""
    status = getattr(exc, 'status', None)
    try:
        return int(status) if status else None
    except (TypeError, ValueError):
        return None


def _retry_after(exc):
    """The server's own Retry-After in seconds, when it sent one."""
    headers = getattr(exc, 'headers', None)
    if not headers:
        return None
    try:
        value = headers.get('Retry-After')
    except AttributeError:
        return None
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return None          # HTTP-date form: fall back to our own backoff


def _describe(exc):
    status = _status_of(exc)
    reason = (getattr(exc, 'reason', '') or '').strip()
    name = exc.__class__.__name__
    return f'{name} {status} {reason}'.strip() if status else f'{name}: {exc}'


def call_with_retry(label, fn, *args, attempts=DEFAULT_ATTEMPTS,
                    base_delay=DEFAULT_BASE_DELAY, max_delay=DEFAULT_MAX_DELAY,
                    sleep=time.sleep, log=print, **kwargs):
    """Call `fn(*args, **kwargs)`, retrying only what a retry can fix.

    Raises the original exception immediately for a status a retry cannot help,
    and `UpstreamUnavailable` once the transient attempts are spent.
    """
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except _RETRYABLE as exc:
            status = _status_of(exc)
            if status is not None and status not in TRANSIENT_STATUS:
                raise                       # permanent: let the caller see it
            last = exc
            if attempt == attempts:
                break
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            server_wait = _retry_after(exc)
            if server_wait is not None:
                # Honour the server's number, but never sleep past our ceiling —
                # this runs inside a ten-minute cron slot.
                delay = min(max_delay, max(delay, server_wait))
            delay += random.uniform(0, 1.5)     # de-sync concurrent jobs
            log(f'{label}: {_describe(exc)} — retrying in {delay:.0f}s '
                f'({attempt}/{attempts - 1})', flush=True)
            sleep(delay)
    raise UpstreamUnavailable(label, attempts, _describe(last))
