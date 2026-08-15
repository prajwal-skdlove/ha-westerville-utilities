"""Generates a proper Android adaptive-icon asset set (separate foreground
and background layers) from the same W-bolt monogram drawn in make_icon.py,
instead of the flat pre-flattened square/round PNGs that were used before.

The original monogram was designed to fill a rounded-*square* canvas and
touches close enough to the edges/corners that a circular or squircle
launcher mask (what Android actually applies on real devices) would clip
the tips of the W's outer strokes. This script re-draws the same shape,
auto-crops it, then rescales it to fit inside the adaptive-icon "safe zone"
(a 66dp-diameter circle centered in the 108dp canvas) so it survives every
launcher's mask shape.

Run from this directory: python make_adaptive_icon.py
Writes directly into the Android app's res/ folders.
"""

import math
import os

from PIL import Image, ImageDraw

SS = 4  # supersample factor for anti-aliasing
BASE = 432  # baseline px for a 108dp asset at xxxhdpi (4x)
SIZE = BASE * SS

NAVY = (18, 42, 63)
TEAL = (22, 99, 112)
AMBER = (255, 200, 87, 255)

ANDROID_RES = os.path.join(
    "..", "..", "wv-utility-android", "app", "src", "main", "res"
)

# (density folder, px size for a 108dp asset)
DENSITIES = [
    ("mipmap-mdpi", 108),
    ("mipmap-hdpi", 162),
    ("mipmap-xhdpi", 216),
    ("mipmap-xxhdpi", 324),
    ("mipmap-xxxhdpi", 432),
]
# Legacy (pre-adaptive-icon) launcher sizes, for API <26 fallback.
LEGACY_DENSITIES = [
    ("mipmap-mdpi", 48),
    ("mipmap-hdpi", 72),
    ("mipmap-xhdpi", 96),
    ("mipmap-xxhdpi", 144),
    ("mipmap-xxxhdpi", 192),
]


def lerp(a, b, t):
    return a + (b - a) * t


def vertical_gradient_rgba(size, top_color, bottom_color):
    img = Image.new("RGBA", (size, size))
    draw = ImageDraw.Draw(img)
    for y in range(size):
        t = y / (size - 1)
        r = int(lerp(top_color[0], bottom_color[0], t))
        g = int(lerp(top_color[1], bottom_color[1], t))
        b = int(lerp(top_color[2], bottom_color[2], t))
        draw.line([(0, y), (size, y)], fill=(r, g, b, 255))
    return img


def perp(dx, dy):
    n = math.hypot(dx, dy)
    return (-dy / n, dx / n)


def draw_monogram(canvas_size):
    """Draws just the amber W-bolt stroke on a transparent canvas, using the
    exact same proportions as make_icon.py's monogram (not yet safe-zoned)."""
    canvas = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    pad = canvas_size * 0.16
    top_y = canvas_size * 0.24
    bot_y = canvas_size * 0.78
    mid_y = canvas_size * 0.46

    P0 = (pad, top_y)
    P1 = (canvas_size * 0.335, bot_y)
    P2 = (canvas_size * 0.50, mid_y)
    P3 = (canvas_size * 0.665, bot_y)
    P4 = (canvas_size - pad, top_y)

    def kink(a, b, side, amount):
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        px, py = perp(b[0] - a[0], b[1] - a[1])
        return (mx + px * amount * side, my + py * amount * side)

    kink_amount = canvas_size * 0.135
    K1 = kink(P1, P2, -1, kink_amount)
    K2 = kink(P2, P3, 1, kink_amount)

    points = [P0, P1, K1, P2, K2, P3, P4]
    stroke_width = int(canvas_size * 0.105)

    draw.line(points, fill=AMBER, width=stroke_width, joint="curve")
    r = stroke_width / 2
    for p in points:
        draw.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=AMBER)

    return canvas


def make_foreground(safe_zone_fraction=0.60):
    """Auto-crops the raw monogram to its actual ink, then rescales +
    recenters it so its bounding circle fits within safe_zone_fraction of
    the full adaptive-icon canvas (default 60%, inside the 66/108=61.1%
    guaranteed-safe zone with a small margin)."""
    raw = draw_monogram(SIZE)
    bbox = raw.getbbox()
    cropped = raw.crop(bbox)

    cw, ch = cropped.size
    content_diameter = math.hypot(cw, ch)
    target_diameter = SIZE * safe_zone_fraction
    scale = target_diameter / content_diameter

    new_w, new_h = max(1, round(cw * scale)), max(1, round(ch * scale))
    resized = cropped.resize((new_w, new_h), Image.LANCZOS)

    fg = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    fg.paste(resized, ((SIZE - new_w) // 2, (SIZE - new_h) // 2), resized)
    return fg


def make_background():
    return vertical_gradient_rgba(SIZE, NAVY, TEAL)


def make_legacy(mask_shape, foreground, background):
    """Flattens background + safe-zoned foreground for pre-API26 fallback
    icons, masked to either a rounded square or a circle."""
    canvas = Image.alpha_composite(background, foreground)

    mask = Image.new("L", (SIZE, SIZE), 0)
    mdraw = ImageDraw.Draw(mask)
    if mask_shape == "square":
        radius = int(SIZE * 0.22)
        mdraw.rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=radius, fill=255)
    else:
        mdraw.ellipse([0, 0, SIZE - 1, SIZE - 1], fill=255)

    out = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    out.paste(canvas, (0, 0), mask)
    return out


def save_scaled(img, path, size):
    img.resize((size, size), Image.LANCZOS).save(path)


def main():
    foreground = make_foreground()
    background = make_background()
    legacy_square = make_legacy("square", foreground, background)
    legacy_round = make_legacy("round", foreground, background)

    for folder, px in DENSITIES:
        out_dir = os.path.join(ANDROID_RES, folder)
        os.makedirs(out_dir, exist_ok=True)
        save_scaled(foreground, os.path.join(out_dir, "ic_launcher_foreground.png"), px)

    for folder, px in LEGACY_DENSITIES:
        out_dir = os.path.join(ANDROID_RES, folder)
        os.makedirs(out_dir, exist_ok=True)
        save_scaled(legacy_square, os.path.join(out_dir, "ic_launcher.png"), px)
        save_scaled(legacy_round, os.path.join(out_dir, "ic_launcher_round.png"), px)

    anydpi_dir = os.path.join(ANDROID_RES, "mipmap-anydpi-v26")
    os.makedirs(anydpi_dir, exist_ok=True)
    adaptive_xml = """<?xml version="1.0" encoding="utf-8"?>
<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">
    <background android:drawable="@drawable/ic_launcher_background"/>
    <foreground android:drawable="@mipmap/ic_launcher_foreground"/>
</adaptive-icon>
"""
    for name in ("ic_launcher.xml", "ic_launcher_round.xml"):
        with open(os.path.join(anydpi_dir, name), "w", encoding="utf-8") as f:
            f.write(adaptive_xml)

    drawable_dir = os.path.join(ANDROID_RES, "drawable")
    os.makedirs(drawable_dir, exist_ok=True)
    gradient_xml = """<?xml version="1.0" encoding="utf-8"?>
<shape xmlns:android="http://schemas.android.com/apk/res/android" android:shape="rectangle">
    <gradient
        android:type="linear"
        android:angle="270"
        android:startColor="#122A3F"
        android:endColor="#166370"/>
</shape>
"""
    with open(os.path.join(drawable_dir, "ic_launcher_background.xml"), "w", encoding="utf-8") as f:
        f.write(gradient_xml)

    # Play Store hi-res listing icon: flattened, fully opaque, 512x512.
    play_store = Image.alpha_composite(background, foreground).convert("RGB")
    play_store.resize((512, 512), Image.LANCZOS).save("play_store_icon_512.png")

    print("done")


if __name__ == "__main__":
    main()
