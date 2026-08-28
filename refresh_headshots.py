"""Refresh player headshots against ESPN, then mirror the changes to R2.

backfill_headshots.py is a BACKFILL: it skips any player that already has a
mirrored file, which is right for filling gaps and wrong once a new season's
photos land. Measured on the 2026 preseason drop, 47% of active players' images
on ESPN differ from what we mirrored, while historical players sit near 1% — so
returning players were showing last season's photo indefinitely.

This closes that. For every player we could have an image for it HEADs the ESPN
CDN and compares Content-Length against the mirrored file, then downloads only
the ones that actually differ. Cheap requests do the searching; bytes move only
where something changed.

Writes are overwrite-in-place at the existing {id}.png key, so no URL changes
and nothing is orphaned — a replaced image IS the old one removed. (Verified
that an R2 overwrite propagates immediately; the only stale copies are in the
browser cache of someone who already loaded that exact player.)

Safety: a file is only ever replaced by a successful, non-empty download. If
ESPN 404s or errors, the existing image is kept — a bad CDN day can degrade
coverage to "unchanged", never to blank.

Run:  python3 refresh_headshots.py --dry-run   # report what would change
      python3 refresh_headshots.py             # apply
      python3 refresh_headshots.py --limit 500 # cap the work (testing)
"""
import os
import sys
import boto3
import psycopg2
import requests
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, '.env'))

HEADSHOTS_DIR = os.path.join(BASE_DIR, 'static', 'headshots')
os.makedirs(HEADSHOTS_DIR, exist_ok=True)
CDN = 'https://a.espncdn.com/i/headshots/college-football/players/full/{}.png'
UA = 'Mozilla/5.0 (compatible; SaturdaySavant/1.0)'
WORKERS = 12          # polite to ESPN's CDN
MIN_PNG_BYTES = 1000  # anything smaller isn't a real headshot

DRY = '--dry-run' in sys.argv
LIMIT = None
if '--limit' in sys.argv:
    LIMIT = int(sys.argv[sys.argv.index('--limit') + 1])


def main():
    conn = psycopg2.connect(os.getenv('DATABASE_URL'))
    cur = conn.cursor()
    # Everyone we could hold an image for: the current roster (where the new
    # season's photos land) plus any past player we already mirrored. Past
    # players with no image are skipped — the backfill already established ESPN
    # purged them, and re-asking 8k times a week earns nothing.
    cur.execute('''
        SELECT id FROM players
        WHERE active_2026 = 1 OR headshot IS NOT NULL
        ORDER BY active_2026 DESC NULLS LAST, id
    ''')
    ids = [r[0] for r in cur.fetchall()]
    if LIMIT:
        ids = ids[:LIMIT]
    print(f'checking {len(ids):,} players against the ESPN CDN', flush=True)

    session = requests.Session()
    session.headers['User-Agent'] = UA

    def local_size(pid):
        p = os.path.join(HEADSHOTS_DIR, f'{pid}.png')
        return os.path.getsize(p) if os.path.exists(p) else None

    def check(pid):
        """(pid, verdict) — 'new', 'changed', 'same', 'absent', 'error'."""
        try:
            r = session.head(CDN.format(pid), timeout=15, allow_redirects=True)
        except Exception:
            return pid, 'error'
        if r.status_code != 200:
            return pid, 'absent'
        remote = int(r.headers.get('content-length') or 0)
        have = local_size(pid)
        if have is None:
            return pid, 'new'
        return pid, ('same' if have == remote else 'changed')

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        verdicts = list(ex.map(check, ids))

    todo = [pid for pid, v in verdicts if v in ('new', 'changed')]
    tally = {}
    for _, v in verdicts:
        tally[v] = tally.get(v, 0) + 1
    print('  ' + '  '.join(f'{k}={v:,}' for k, v in sorted(tally.items())), flush=True)
    print(f'  -> {len(todo):,} images to fetch', flush=True)

    if DRY:
        print('(dry run — nothing downloaded, uploaded or written)')
        conn.close()
        return 0
    if not todo:
        print('everything already current')
        conn.close()
        return 0

    # ── download ────────────────────────────────────────────────────────────
    def fetch(pid):
        try:
            r = session.get(CDN.format(pid), timeout=25)
            if r.status_code != 200 or len(r.content) < MIN_PNG_BYTES:
                return pid, False
            # Written whole, then moved into place, so an interrupted run can't
            # leave a truncated PNG that later looks like a valid mirror.
            dest = os.path.join(HEADSHOTS_DIR, f'{pid}.png')
            tmp = dest + '.part'
            with open(tmp, 'wb') as f:
                f.write(r.content)
            os.replace(tmp, dest)
            return pid, True
        except Exception:
            return pid, False

    done = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for i, (pid, ok) in enumerate(ex.map(fetch, todo), 1):
            if ok:
                done.append(pid)
            if i % 500 == 0:
                print(f'  downloaded {i:,}/{len(todo):,}', flush=True)
    print(f'downloaded {len(done):,} of {len(todo):,}', flush=True)

    # ── upload the changed files to R2 (same key = overwrite) ───────────────
    s3 = boto3.client(
        's3',
        endpoint_url=f"https://{os.getenv('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com",
        aws_access_key_id=os.getenv('R2_ACCESS_KEY_ID'),
        aws_secret_access_key=os.getenv('R2_SECRET_ACCESS_KEY'))
    bucket = os.getenv('R2_BUCKET_NAME')
    public_url = os.getenv('R2_PUBLIC_URL').rstrip('/')

    def push(pid):
        try:
            s3.upload_file(
                os.path.join(HEADSHOTS_DIR, f'{pid}.png'), bucket, f'{pid}.png',
                ExtraArgs={'ContentType': 'image/png',
                           'CacheControl': 'public, max-age=31536000'})
            return pid, True
        except Exception:
            return pid, False

    pushed = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for i, (pid, ok) in enumerate(ex.map(push, done), 1):
            if ok:
                pushed.append(pid)
            if i % 500 == 0:
                print(f'  uploaded {i:,}/{len(done):,}', flush=True)
    print(f'uploaded {len(pushed):,} of {len(done):,}', flush=True)

    # ── point newly-acquired images at their R2 URL ─────────────────────────
    # Players who already had a headshot keep the identical URL; only the bytes
    # behind it changed, so there is nothing to rewrite for them.
    if pushed:
        cur.execute('''
            UPDATE players SET headshot = %s || '/' || id || '.png'
            WHERE id = ANY(%s) AND headshot IS NULL
        ''', (public_url, pushed))
        conn.commit()
        print(f'linked {cur.rowcount:,} newly-available headshots', flush=True)
    conn.close()

    try:
        from cache_notify import notify_cache_clear
        notify_cache_clear()
    except Exception:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
