"""Make the PNG icons from static/icon.svg (run again after changing it).

Phones don't use an SVG for the home screen: Chrome on Android makes a plain
letter icon without a PNG, an iPhone wants a 180 px PNG. The "maskable" icon fills
its square and keeps the drawing in the middle 80%: Android crops it to its own
shape (circle, squircle…). Needs `rsvg-convert` (librsvg).
"""

import subprocess
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"
TILE = 'rx="112"'
DRAWING = "translate(46 30) scale(0.7)"  # the bag, in icon.svg
# The same, smaller and centred: inside the safe circle (80% of the side) once cropped
SAFE_DRAWING = "translate(88 71) scale(0.56)"


def maskable_svg() -> str:
    svg = (STATIC / "icon.svg").read_text(encoding="utf-8")
    assert TILE in svg and DRAWING in svg, "icon.svg changed: update make_icons.py"
    return svg.replace(TILE, 'rx="0"').replace(DRAWING, SAFE_DRAWING)


def render(svg: Path, png: Path, size: int) -> None:
    subprocess.run(["rsvg-convert", "-w", str(size), "-h", str(size), "-o", str(png), str(svg)], check=True)


def main() -> None:
    maskable = STATIC / "icon-maskable.svg"
    maskable.write_text(maskable_svg(), encoding="utf-8")
    render(STATIC / "icon.svg", STATIC / "icon-192.png", 192)
    render(STATIC / "icon.svg", STATIC / "icon-512.png", 512)
    render(maskable, STATIC / "icon-maskable-512.png", 512)
    render(maskable, STATIC / "apple-touch-icon.png", 180)  # iOS rounds the corners itself


if __name__ == "__main__":
    main()
