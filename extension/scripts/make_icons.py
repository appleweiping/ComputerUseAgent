"""Regenerate the extension toolbar/store icons in ``extension/app/icons``.

The icon is a rounded green tile with a white ring and a red "record" dot. Each
size is drawn at 16x resolution and downsampled with a Lanczos filter, so small
sizes stay crisp without hand-tuned pixel art. Requires Pillow (development
only; the extension itself ships the PNG files):

    python extension/scripts/make_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

SIZES = (16, 32, 48, 128)
SUPERSAMPLE = 16
BACKGROUND = (8, 127, 91, 255)  # matches the popup accent colour (#087f5b)
RING = (255, 255, 255, 255)
DOT = (216, 74, 50, 255)  # matches the recording indicator (#d84a32)
ICON_DIR = Path(__file__).resolve().parents[1] / "app" / "icons"


def render(size: int) -> Image.Image:
    canvas = size * SUPERSAMPLE
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    inset = round(canvas * 0.04)
    draw.rounded_rectangle(
        (inset, inset, canvas - inset - 1, canvas - inset - 1),
        radius=round(canvas * 0.22),
        fill=BACKGROUND,
    )
    centre = canvas / 2
    ring_radius = canvas * 0.30
    ring_width = max(SUPERSAMPLE, round(canvas * 0.075))
    draw.ellipse(
        (
            centre - ring_radius,
            centre - ring_radius,
            centre + ring_radius,
            centre + ring_radius,
        ),
        outline=RING,
        width=ring_width,
    )
    dot_radius = canvas * 0.15
    draw.ellipse(
        (centre - dot_radius, centre - dot_radius, centre + dot_radius, centre + dot_radius),
        fill=DOT,
    )
    return image.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        path = ICON_DIR / f"{size}.png"
        render(size).save(path, format="PNG", optimize=True)
        print(f"{path.relative_to(ICON_DIR.parents[1]).as_posix()} {size}x{size}")


if __name__ == "__main__":
    main()
