"""App icon: the sidebar's shield-with-check, white and green on a dark tile.
One shape list drives the SVG and the PNGs. The PNGs are drawn with the standard library at image
build time (`python -c "import icon; icon.build()"` in the Dockerfile) into static/icons/."""
import os
import struct
import zlib

TILE, WHITE, GREEN = (30, 41, 59), (255, 255, 255), (34, 197, 94)

SHIELD = [(50, 13), (79, 25), (79, 49), (77, 60), (72, 69), (65, 77), (57, 83), (50, 87),
          (43, 83), (35, 77), (28, 69), (23, 60), (21, 49), (21, 25)]

SHAPES = [
    ("rrect", (0, 0, 100, 100, 22), TILE),
    ("poly", SHIELD, WHITE),
    ("line", (37, 51, 46, 60, 8), GREEN),
    ("line", (46, 60, 64, 41, 8), GREEN),
]

FILES = ("favicon.svg", "favicon.ico", "apple-touch-icon.png", "icon-192.png", "icon-512.png")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "icons")


def _hex(c):
    return "#%02x%02x%02x" % c


def svg():
    out = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">']
    for kind, a, c in SHAPES:
        if kind == "rrect":
            x0, y0, x1, y1, r = a
            out.append(f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" rx="{r}" fill="{_hex(c)}"/>')
        elif kind == "poly":
            pts = " ".join(f"{x},{y}" for x, y in a)
            out.append(f'<polygon points="{pts}" fill="{_hex(c)}" stroke="{_hex(c)}" stroke-width="3" stroke-linejoin="round"/>')
        else:
            x0, y0, x1, y1, w = a
            out.append(f'<line x1="{x0}" y1="{y0}" x2="{x1}" y2="{y1}" stroke="{_hex(c)}" '
                       f'stroke-width="{w}" stroke-linecap="round"/>')
    out.append("</svg>")
    return "".join(out)


def _inside(kind, a, x, y):
    if kind == "rrect":
        x0, y0, x1, y1, r = a
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            return False
        cx = min(max(x, x0 + r), x1 - r)
        cy = min(max(y, y0 + r), y1 - r)
        return (x - cx) ** 2 + (y - cy) ** 2 <= r * r
    if kind == "poly":
        inside, j = False, len(a) - 1
        for i in range(len(a)):
            xi, yi = a[i]
            xj, yj = a[j]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
        return inside
    x0, y0, x1, y1, w = a
    dx, dy = x1 - x0, y1 - y0
    t = max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / (dx * dx + dy * dy)))
    return (x - x0 - t * dx) ** 2 + (y - y0 - t * dy) ** 2 <= (w / 2) ** 2


def png(size, full_bleed=False):
    """full_bleed: square tile with no transparent corners (iOS rounds home-screen icons itself)."""
    ss = 3
    shapes = list(SHAPES)
    if full_bleed:
        shapes[0] = ("rrect", (0, 0, 100, 100, 0), TILE)
    step = 100.0 / (size * ss)
    rows = []
    for py in range(size):
        row = bytearray([0])
        for px in range(size):
            r = g = b = a = 0
            for sy in range(ss):
                y = (py * ss + sy + 0.5) * step
                for sx in range(ss):
                    x = (px * ss + sx + 0.5) * step
                    col = None
                    for kind, args, c in shapes:
                        if _inside(kind, args, x, y):
                            col = c
                    if col:
                        r += col[0]; g += col[1]; b += col[2]; a += 1
            row += bytes((round(r / a), round(g / a), round(b / a), round(255 * a / (ss * ss)))) if a else b"\0\0\0\0"
        rows.append(bytes(row))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


def render(name):
    return {"favicon.svg": lambda: svg().encode(), "favicon.ico": lambda: png(32),
            "apple-touch-icon.png": lambda: png(180, full_bleed=True),
            "icon-192.png": lambda: png(192), "icon-512.png": lambda: png(512)}[name]()


def build(folder=OUT):
    os.makedirs(folder, exist_ok=True)
    for f in FILES:
        data = render(f)
        with open(os.path.join(folder, f), "wb") as fh:
            fh.write(data)


MANIFEST = {"name": "WAN Watchdog", "short_name": "WAN Watchdog", "start_url": "/", "display": "standalone",
            "background_color": "#fafafa", "theme_color": "#1e293b",
            "icons": [{"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                      {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
                      {"src": "/static/icons/favicon.svg", "sizes": "any", "type": "image/svg+xml"}]}
