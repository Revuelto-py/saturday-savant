"""Phase 1b: download historical player headshots and mirror them to R2.

Same pipeline as setup_db.py / upload_headshots_to_r2.py — ESPN CDN
(https://a.espncdn.com/i/headshots/college-football/players/full/{id}.png)
into static/headshots/{id}.png, then uploaded to the R2 bucket with the same
ContentType/CacheControl, then players.headshot set to the same public R2 URL
convention — but scoped to players whose headshot is NULL (the 37k historical
identities plus any current players that never had one), so the existing
mirrored files are never re-downloaded or re-uploaded.

Stages (each resumable — skip-existing at every step):
    1. download   ESPN CDN -> static/headshots/   (~10-15 concurrent, 404s expected:
                  ESPN purged many pre-2017 players)
    2. upload     new local files -> R2
    3. db         players.headshot = R2 URL for every id with a mirrored file

Run:  python3 backfill_headshots.py            # all three stages
      python3 backfill_headshots.py download   # a single stage
"""
import os
import sys
import psycopg2
import requests
import boto3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, '.env'), override=True)

HEADSHOTS_DIR = os.path.join(BASE_DIR, 'static', 'headshots')
os.makedirs(HEADSHOTS_DIR, exist_ok=True)
CDN = "https://a.espncdn.com/i/headshots/college-football/players/full/{}.png"
WORKERS = 12   # polite to ESPN's CDN; ~37k requests ≈ 60-90 min


def target_ids():
    """Player ids that still need a headshot (never mirrored)."""
    conn = psycopg2.connect(os.getenv('DATABASE_URL'))
    cur = conn.cursor()
    cur.execute('SELECT id FROM players WHERE headshot IS NULL ORDER BY id')
    ids = [r[0] for r in cur.fetchall()]
    conn.close()
    return ids


def stage_download():
    ids = target_ids()
    todo = [i for i in ids if not os.path.exists(os.path.join(HEADSHOTS_DIR, f"{i}.png"))]
    print(f"download: {len(ids)} candidates, {len(todo)} not yet on disk", flush=True)

    session = requests.Session()

    def fetch(pid):
        url = CDN.format(pid)
        for attempt in (1, 2):
            try:
                r = session.get(url, timeout=8)
                if r.status_code == 200 and r.content:
                    with open(os.path.join(HEADSHOTS_DIR, f"{pid}.png"), 'wb') as f:
                        f.write(r.content)
                    return 'saved'
                return 'missing'   # 404 — ESPN purged this player
            except Exception:
                if attempt == 2:
                    return 'error'
        return 'error'

    saved = missing = errors = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(fetch, pid): pid for pid in todo}
        for n, fut in enumerate(as_completed(futures), 1):
            res = fut.result()
            if res == 'saved': saved += 1
            elif res == 'missing': missing += 1
            else: errors += 1
            if n % 2000 == 0:
                print(f"  {n}/{len(todo)}  saved={saved} missing={missing} errors={errors}", flush=True)
    print(f"download done: saved={saved} missing={missing} errors={errors}", flush=True)


def _r2_client():
    return boto3.client(
        service_name='s3',
        endpoint_url=f"https://{os.getenv('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com",
        aws_access_key_id=os.getenv('R2_ACCESS_KEY_ID'),
        aws_secret_access_key=os.getenv('R2_SECRET_ACCESS_KEY'),
    )


def stage_upload():
    s3 = _r2_client()
    bucket = os.getenv('R2_BUCKET_NAME')

    # Preflight (same as upload_headshots_to_r2.py)
    s3.put_object(Bucket=bucket, Key='_preflight_test.txt', Body=b'ok')
    s3.delete_object(Bucket=bucket, Key='_preflight_test.txt')
    print("preflight OK", flush=True)

    # Only files for ids whose players.headshot is still NULL — everything
    # already mirrored keeps its existing R2 object untouched.
    ids = set(target_ids())
    files = [f"{i}.png" for i in ids if os.path.exists(os.path.join(HEADSHOTS_DIR, f"{i}.png"))]
    print(f"upload: {len(files)} new files", flush=True)

    def put(filename):
        try:
            s3.upload_file(
                os.path.join(HEADSHOTS_DIR, filename), bucket, filename,
                ExtraArgs={'ContentType': 'image/png', 'CacheControl': 'public, max-age=31536000'})
            return True
        except Exception as e:
            print(f"  failed {filename}: {e}", flush=True)
            return False

    ok = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(put, f) for f in files]
        for n, fut in enumerate(as_completed(futures), 1):
            ok += 1 if fut.result() else 0
            if n % 2000 == 0:
                print(f"  {n}/{len(files)} uploaded", flush=True)
    print(f"upload done: {ok}/{len(files)}", flush=True)


def stage_db():
    public_url = os.getenv('R2_PUBLIC_URL').rstrip('/')
    ids = target_ids()
    mirrored = [i for i in ids if os.path.exists(os.path.join(HEADSHOTS_DIR, f"{i}.png"))]
    conn = psycopg2.connect(os.getenv('DATABASE_URL'))
    cur = conn.cursor()
    cur.execute('''
        UPDATE players SET headshot = %s || '/' || id || '.png'
        WHERE headshot IS NULL AND id = ANY(%s)
    ''', (public_url, mirrored))
    conn.commit()
    print(f"db: headshot URL set for {cur.rowcount} players "
          f"({len(ids) - len(mirrored)} remain placeholder — no ESPN image exists)", flush=True)
    conn.close()


if __name__ == '__main__':
    stages = sys.argv[1:] or ['download', 'upload', 'db']
    for s in stages:
        {'download': stage_download, 'upload': stage_upload, 'db': stage_db}[s]()
