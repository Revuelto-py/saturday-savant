"""Small WebP copies of every mirrored headshot, kept next to the originals in R2.

A stored headshot is a ~275KB 600px PNG, and almost every page draws it at
28-74px: the homepage spent ~8MB on 38 faces, a game page ~8MB on 34. Each
original {id}.png gets t/{id}.webp, 200px wide (~6KB) — twice the largest list
size, so it stays sharp on a 2x screen. Pages reach it through
main.shrink_images, which falls back to the original if a thumbnail is
missing, and the URL keeps the original's ?v= stamp, so a re-shot photo is a
new URL for the thumbnail too.

pipeline/refresh_headshots.py writes the thumbnail whenever it uploads an
original. This file's __main__ backfills the rest (skipping what exists):

    python3 headshot_thumbs.py            # every player with a headshot
    python3 headshot_thumbs.py --force    # rebuild even existing thumbnails
"""
import hashlib
import io
import os
import sys

THUMB_PREFIX = 't/'
THUMB_WIDTH = 200


def make_thumb(png_bytes):
    """WebP bytes for one original. Raises if Pillow cannot read it."""
    from PIL import Image
    im = Image.open(io.BytesIO(png_bytes)).convert('RGBA')
    im.thumbnail((THUMB_WIDTH, THUMB_WIDTH), Image.LANCZOS)   # 600x436 -> 200x145
    buf = io.BytesIO()
    im.save(buf, 'WEBP', quality=80, method=4)
    return buf.getvalue()


def upload_thumb(s3, bucket, pid, png_bytes):
    s3.put_object(Bucket=bucket, Key=f'{THUMB_PREFIX}{pid}.webp', Body=make_thumb(png_bytes),
                  ContentType='image/webp', CacheControl='public, max-age=31536000')


def r2_client():
    import boto3
    return boto3.client(
        's3',
        endpoint_url=f"https://{os.getenv('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com",
        aws_access_key_id=os.getenv('R2_ACCESS_KEY_ID'),
        aws_secret_access_key=os.getenv('R2_SECRET_ACCESS_KEY'))


def backfill(force=False):
    import psycopg2
    import requests
    from concurrent.futures import ThreadPoolExecutor
    root = os.path.dirname(os.path.abspath(__file__))
    local_dir = os.path.join(root, 'static', 'headshots')
    s3, bucket = r2_client(), os.getenv('R2_BUCKET_NAME')

    have = set()
    if not force:
        for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=THUMB_PREFIX):
            have.update(o['Key'][len(THUMB_PREFIX):-5] for o in page.get('Contents', []))
    conn = psycopg2.connect(os.getenv('DATABASE_URL'))
    cur = conn.cursor()
    cur.execute('SELECT id, headshot FROM players WHERE headshot IS NOT NULL')
    todo = [(pid, url) for pid, url in cur.fetchall() if str(pid) not in have]
    conn.close()
    print(f'{len(have):,} thumbnails exist, {len(todo):,} to build', flush=True)

    def source(pid, url):
        # The local mirror when it holds the same bytes as R2 (its md5 is the
        # ?v= stamp); otherwise the bucket, since the weekly refresh runs in a
        # container and never updates this machine's copy.
        stamp = url.split('?v=')[1] if '?v=' in url else None
        path = os.path.join(local_dir, f'{pid}.png')
        if stamp and os.path.exists(path):
            data = open(path, 'rb').read()
            if hashlib.md5(data).hexdigest().startswith(stamp):
                return data
        r = requests.get(url, timeout=20)
        return r.content if r.status_code == 200 else None

    def one(item):
        pid, url = item
        for _ in range(3):           # R2 throttles bursts; retry the network steps
            try:
                data = source(pid, url)
            except Exception:
                continue
            if data is None:
                return 'missing'
            try:
                thumb = make_thumb(data)
            except Exception:
                return 'unreadable'  # left to the original via the page's fallback
            try:
                s3.put_object(Bucket=bucket, Key=f'{THUMB_PREFIX}{pid}.webp', Body=thumb,
                              ContentType='image/webp', CacheControl='public, max-age=31536000')
                return 'ok'
            except Exception:
                continue
        return 'error'

    tally = {}
    with ThreadPoolExecutor(max_workers=24) as ex:
        for i, verdict in enumerate(ex.map(one, todo), 1):
            tally[verdict] = tally.get(verdict, 0) + 1
            if i % 2000 == 0:
                print(f'  {i:,}/{len(todo):,}  {tally}', flush=True)
    print('done', tally, flush=True)


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
    backfill(force='--force' in sys.argv)
