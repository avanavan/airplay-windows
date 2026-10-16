"""The app icon, drawn in code: a screen outline over a triangle (the AirPlay glyph shape)."""

from __future__ import annotations

from PIL import Image, ImageDraw

BLUE = (10, 132, 255, 255)
GREY = (150, 150, 150, 255)


def icon_image(playing: bool = True, size: int = 64) -> Image.Image:
    """Blue while streaming, grey otherwise. Drawn 4x larger and scaled down for smooth edges."""
    color = BLUE if playing else GREY
    big = size * 4
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = big / 64
    d.rounded_rectangle((4 * s, 6 * s, 60 * s, 44 * s), radius=7 * s, outline=color, width=round(5 * s))
    d.polygon([(32 * s, 24 * s), (8 * s, 62 * s), (56 * s, 62 * s)], fill=(0, 0, 0, 0))  # gap around the triangle
    d.polygon([(32 * s, 32 * s), (15 * s, 59 * s), (49 * s, 59 * s)], fill=color)
    return img.resize((size, size), Image.LANCZOS)


def write_ico(path: str) -> None:
    """Multi-size .ico for the executable and installer."""
    icon_image(True, 256).save(path, sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48),
                                            (64, 64), (128, 128), (256, 256)])
