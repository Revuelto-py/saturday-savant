"""Self-check: resizing a headshot must not keep the full-size original in memory.

A stored headshot is a ~275KB 600px PNG. Every explorer face and the boot
warmup go through main._img_variant; when the originals were memoized (600 of
them per worker, ~165MB) two workers outgrew Render's 512MB instance.
Stubs the image download, so it needs no network: python3 tools/check_img_memory.py
(main connects its DB pool on import, so .env must be present).
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402
from PIL import Image  # noqa: E402

buf = io.BytesIO()
Image.new('RGB', (600, 600), (120, 40, 40)).save(buf, 'PNG')
PNG = buf.getvalue()


class Resp:
    status_code = 200
    headers = {'Content-Type': 'image/png'}
    content = PNG


main.req.get = lambda *a, **k: Resp()
main.img_cache.clear()
for i in range(50):
    out = main._img_variant(f'https://example.r2.dev/{i}.png?v=1', 128, True, True)
    assert out and out[1] == 'image/webp' and len(out[0]) < 20000, 'a small WebP comes back'
held = main._img_fetch.cache_info().currsize
assert held == 0, f'{held} full-size originals still held after resizing'
print('img memory: ok (resizes keep no originals)')
