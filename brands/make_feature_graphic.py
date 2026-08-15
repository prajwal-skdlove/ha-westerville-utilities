"""Generates the Play Store feature graphic (1024x500) for the Android app,
reusing the same navy->teal gradient and W-bolt monogram as the app icon
(see make_adaptive_icon.py) so store assets stay visually consistent with
the actual app.

Run from this directory: python make_feature_graphic.py
Writes feature_graphic_1024x500.png here, and a copy into the Android
project's root for easy upload.
"""

import math
import os

from PIL import Image, ImageDraw, ImageFont

SS = 4
W, H = 1024, 500
SIZE = (W * SS, H * SS)

NAVY = (18, 42, 63)
TEAL = (22, 99, 112)
AMBER = (255, 200, 87, 255)
CREAM = (245, 240, 230, 255)

FONT_BOLD = "C:/Windows/Fonts/segoeuib.ttf"
FONT_REGULAR = "C:/Windows/Fonts/segoeui.ttf"

OUT_PATHS = [
    "feature_graphic_1024x500.png",
    os.path.join("..", "..", "wv-utility-android", "feature_graphic_1024x500.png"),
]


def lerp(a, b, t):
    return a + (b - a) * t


def vertical_gradient(size, top_color, bottom_color):
    w, h = size
    img = Image.new("RGBA", (w, h))
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / (h - 1)
        r = int(lerp(top_color[0], bottom_color[0], t))
        g = int(lerp(top_color[1], bottom_color[1], t))
        b = int(lerp(top_color[2], bottom_color[2], t))
        draw.line([(0, y), (w, y)], fill=(r, g, b, 255))
    return img


def perp(dx, dy):
    n = math.hypot(dx, dy)
    return (-dy / n, dx / n)


def draw_monogram(canvas_size):
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


def make_monogram_badge(diameter):
    """Monogram auto-cropped and centered in a soft rounded-square badge,
    matching the app icon's look, sized for the feature graphic's left side."""
    raw = draw_monogram(diameter * 2)
    bbox = raw.getbbox()
    cropped = raw.crop(bbox)
    cw, ch = cropped.size
    scale = (diameter * 0.66) / max(cw, ch)
    new_w, new_h = round(cw * scale), round(ch * scale)
    resized = cropped.resize((new_w, new_h), Image.LANCZOS)

    badge = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    mask = Image.new("L", (diameter, diameter), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.rounded_rectangle([0, 0, diameter - 1, diameter - 1], radius=int(diameter * 0.22), fill=255)
    bg = Image.new("RGBA", (diameter, diameter), (255, 255, 255, 30))
    badge.paste(bg, (0, 0), mask)
    badge.paste(resized, ((diameter - new_w) // 2, (diameter - new_h) // 2), resized)
    return badge


def main():
    canvas = vertical_gradient(SIZE, NAVY, TEAL)
    draw = ImageDraw.Draw(canvas)

    badge_d = int(H * SS * 0.62)
    badge = make_monogram_badge(badge_d)
    badge_x = int(W * SS * 0.06)
    badge_y = (H * SS - badge_d) // 2
    canvas.alpha_composite(badge, (badge_x, badge_y))

    text_x = badge_x + badge_d + int(W * SS * 0.055)

    title_font = ImageFont.truetype(FONT_BOLD, int(H * SS * 0.155))
    tag_font = ImageFont.truetype(FONT_BOLD, int(H * SS * 0.06))

    title_y = int(H * SS * 0.26)
    draw.text((text_x, title_y), "Westerville", font=title_font, fill=CREAM)
    draw.text((text_x, title_y + int(H * SS * 0.165)), "Utility Usage", font=title_font, fill=CREAM)

    # Small pill tag: "UNOFFICIAL" -- keeps the disclaimer visible even at
    # store-listing thumbnail size, not just buried in the description.
    tag_text = "UNOFFICIAL"
    tag_bbox = draw.textbbox((0, 0), tag_text, font=tag_font)
    tag_w = tag_bbox[2] - tag_bbox[0]
    tag_h = tag_bbox[3] - tag_bbox[1]
    pad_x, pad_y = int(H * SS * 0.035), int(H * SS * 0.02)
    tag_y = int(H * SS * 0.13)
    draw.rounded_rectangle(
        [text_x, tag_y, text_x + tag_w + pad_x * 2, tag_y + tag_h + pad_y * 2],
        radius=(tag_h + pad_y * 2) // 2,
        fill=AMBER,
    )
    draw.text((text_x + pad_x, tag_y + pad_y - tag_bbox[1]), tag_text, font=tag_font, fill=NAVY)

    sub_y = int(H * SS * 0.70)
    sub_text = "Track usage & bills on your phone"
    available_w = (W * SS) - text_x - int(W * SS * 0.03)
    sub_size = int(H * SS * 0.075)
    while sub_size > 10:
        sub_font = ImageFont.truetype(FONT_REGULAR, sub_size)
        bbox = draw.textbbox((0, 0), sub_text, font=sub_font)
        if bbox[2] - bbox[0] <= available_w:
            break
        sub_size -= 2
    draw.text((text_x, sub_y), sub_text, font=sub_font, fill=(255, 255, 255, 235))

    final = canvas.resize((W, H), Image.LANCZOS).convert("RGB")
    for path in OUT_PATHS:
        final.save(path)
    print("done")


if __name__ == "__main__":
    main()
