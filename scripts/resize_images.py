# scripts/resize_images.py
from PIL import Image, ImageOps
import os

SRC_DIR = 'uploads'            # put originals here
DST_DIR = 'static/images'      # Flask static images
os.makedirs(DST_DIR, exist_ok=True)

TARGET_WIDTH = 1200
TARGET_HEIGHT = 800
QUALITY = 85

def process_image(src_path, dst_path):
    with Image.open(src_path) as im:
        if im.mode in ("RGBA", "P"):
            im = im.convert("RGB")
        im = ImageOps.fit(im, (TARGET_WIDTH, TARGET_HEIGHT), Image.LANCZOS)
        im.save(dst_path, format='JPEG', quality=QUALITY, optimize=True)

if __name__ == '__main__':
    for fname in os.listdir(SRC_DIR):
        src = os.path.join(SRC_DIR, fname)
        if not os.path.isfile(src): continue
        name, _ = os.path.splitext(fname)
        dst = os.path.join(DST_DIR, f"{name}.jpg")
        try:
            process_image(src, dst)
            print("Saved:", dst)
        except Exception as e:
            print("Error processing", src, e)
