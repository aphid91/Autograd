"""Draw the web app's install icons into web/icons/ (run by hand; the PNGs
are committed). A belt with two items on it, on the shell's dark ground.

    python tools/make_web_icons.py
"""
from pathlib import Path
from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "web" / "icons"
BG, BELT, CHEV = (11, 12, 15), (117, 120, 133), (189, 194, 209)
GREEN, RED, FILL = (97, 209, 107), (230, 92, 87), (36, 38, 46)


def draw(size, safe):
    """`safe` is the fraction of the canvas the art may use: a maskable icon
    keeps its content inside the central 80% circle the launcher crops to."""
    s = size * 4                                  # supersample, then shrink
    im = Image.new("RGB", (s, s), BG)
    d = ImageDraw.Draw(im)
    u = s * safe / 10                             # art in a 10x10 grid
    o = (s - 10 * u) / 2
    P = lambda x, y: (o + x * u, o + y * u)
    # an L-shaped belt: in from the left, turning up
    w = 1.7 * u
    d.rectangle([*P(0.5, 6.15), *P(6.85, 7.85)], fill=BELT)
    d.rectangle([*P(5.15, 1.5), *P(6.85, 7.85)], fill=BELT)
    for cx in (1.6, 3.4):                         # chevrons pointing right
        d.line([P(cx, 6.55), P(cx + 0.45, 7.0), P(cx, 7.45)], fill=CHEV, width=int(u * 0.28), joint="curve")
    d.line([P(5.55, 3.9), P(6.0, 3.45), P(6.45, 3.9)], fill=CHEV, width=int(u * 0.28), joint="curve")
    for (cx, cy), ring in (((4.2, 7.0), GREEN), ((6.0, 2.4), RED)):
        r = 1.15 * u
        x, y = P(cx, cy)
        d.ellipse([x - r, y - r, x + r, y + r], fill=ring)
        r2 = r * 0.72
        d.ellipse([x - r2, y - r2, x + r2, y + r2], fill=FILL)
    return im.resize((size, size), Image.LANCZOS)


OUT.mkdir(exist_ok=True)
draw(192, 0.86).save(OUT / "icon-192.png")
draw(512, 0.86).save(OUT / "icon-512.png")
draw(512, 0.62).save(OUT / "maskable-512.png")
draw(180, 0.80).save(OUT / "apple-touch-icon.png")
print("wrote", *sorted(p.name for p in OUT.glob("*.png")))
