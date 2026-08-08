from PIL import Image, ImageDraw
import math

SS = 4  # supersample factor for anti-aliasing
BASE = 512
SIZE = BASE * SS


def lerp(a, b, t):
    return a + (b - a) * t


def vertical_gradient(size, top_color, bottom_color):
    img = Image.new("RGB", (size, size))
    draw = ImageDraw.Draw(img)
    for y in range(size):
        t = y / (size - 1)
        r = int(lerp(top_color[0], bottom_color[0], t))
        g = int(lerp(top_color[1], bottom_color[1], t))
        b = int(lerp(top_color[2], bottom_color[2], t))
        draw.line([(0, y), (size, y)], fill=(r, g, b))
    return img


def perp(dx, dy):
    n = math.hypot(dx, dy)
    return (-dy / n, dx / n)


def make_icon(circular_mask=True):
    # Background: rounded square with vertical gradient (deep slate-navy -> teal)
    # Chosen to be distinct from typical utility green/blue leaf marks.
    bg = vertical_gradient(SIZE, (18, 42, 63), (22, 99, 112))

    # Rounded-rect mask
    mask = Image.new("L", (SIZE, SIZE), 0)
    mdraw = ImageDraw.Draw(mask)
    radius = int(SIZE * 0.22)
    mdraw.rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=radius, fill=255)

    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    canvas.paste(bg, (0, 0), mask)

    draw = ImageDraw.Draw(canvas)

    # --- W + lightning-bolt monogram ---
    # Base W vertices (top-left, bottom-left, middle-peak, bottom-right, top-right)
    pad = SIZE * 0.16
    top_y = SIZE * 0.24
    bot_y = SIZE * 0.78
    mid_y = SIZE * 0.46

    P0 = (pad, top_y)
    P1 = (SIZE * 0.335, bot_y)
    P2 = (SIZE * 0.50, mid_y)
    P3 = (SIZE * 0.665, bot_y)
    P4 = (SIZE - pad, top_y)

    def kink(a, b, side, amount):
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        px, py = perp(b[0] - a[0], b[1] - a[1])
        return (mx + px * amount * side, my + py * amount * side)

    kink_amount = SIZE * 0.135
    K1 = kink(P1, P2, -1, kink_amount)
    K2 = kink(P2, P3, 1, kink_amount)

    points = [P0, P1, K1, P2, K2, P3, P4]

    stroke_color = (255, 200, 87, 255)  # warm amber, high contrast vs navy/teal
    stroke_width = int(SIZE * 0.105)

    draw.line(points, fill=stroke_color, width=stroke_width, joint="curve")
    r = stroke_width / 2
    for p in points:
        draw.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=stroke_color)

    return canvas


def downsample_save(img, out_path, size):
    img.resize((size, size), Image.LANCZOS).save(out_path)


icon = make_icon()
downsample_save(icon, "icon.png", 256)
downsample_save(icon, "icon@2x.png", 512)
downsample_save(icon, "preview.png", 512)
print("done")
