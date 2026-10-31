"""Draws the Replay Desk icon (icon.ico) used by the desktop shortcut."""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "icon.ico"
S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((12, 44, S - 12, S - 44), 36, fill=(20, 22, 28, 255), outline=(255, 70, 85, 255), width=16)
d.ellipse((S / 2 - 42, S / 2 - 42, S / 2 + 42, S / 2 + 42), fill=(255, 70, 85, 255))
img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icon written:", out)
