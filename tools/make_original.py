"""Convert image/1-8_480x384__1_simple.png into image/original.txt.

The source is 480x384; the game image is 960x768, so it is upscaled 2x
(nearest neighbour). Every pixel is mapped to the closest palette colour and
written as its symbol (see backend_design.md section 2.1).

Usage (needs Pillow):  python tools/make_original.py
"""
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "image" / "1-8_480x384__1_simple.png"
TARGET = ROOT / "image" / "original.txt"
SYMBOLS = "123456789abc"
WIDTH, HEIGHT = 960, 768


def main() -> None:
    palette = [
        tuple(int(line.strip().lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))
        for line in (ROOT / "image" / "palette.txt").read_text().split()
    ]
    assert len(palette) == len(SYMBOLS)
    img = Image.open(SOURCE).convert("RGB").resize((WIDTH, HEIGHT), Image.NEAREST)

    cache = {}

    def symbol(rgb):
        if rgb not in cache:
            best = min(range(len(palette)),
                       key=lambda i: sum((a - b) ** 2 for a, b in zip(rgb, palette[i])))
            cache[rgb] = SYMBOLS[best]
        return cache[rgb]

    px = img.load()
    rows = ["".join(symbol(px[x, y]) for x in range(WIDTH)) for y in range(HEIGHT)]
    TARGET.write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"wrote {TARGET} ({len(rows)} rows, {len(cache)} distinct source colours)")


if __name__ == "__main__":
    main()
