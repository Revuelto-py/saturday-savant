from PIL import Image
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
logo_path = os.path.join(BASE_DIR, 'static', 'logo.png')

img = Image.open(logo_path).convert("RGBA")

bbox = img.getbbox()
if bbox:
    img = img.crop(bbox)

sizes = {
    'logo-512.png': 512,
    'logo-256.png': 256,
    'logo-128.png': 128,
    'logo-64.png': 64,
    'logo-32.png': 32,
    'favicon-32.png': 32,
    'favicon-16.png': 16,
}

for filename, size in sizes.items():
    resized = img.copy()
    resized.thumbnail((size, size), Image.LANCZOS)
    resized.save(os.path.join(BASE_DIR, 'static', filename))
    print(f"Saved {filename} ({resized.size})")

print("Done")
