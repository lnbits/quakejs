"""Public social cards with the arena's stored entry price; no network I/O."""

from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ASSETS = Path(__file__).resolve().parent / "static" / "share"
WIDTH, HEIGHT = 1200, 675


@lru_cache(maxsize=32)
def render_share_image(entry_amount: int) -> bytes:
    if not isinstance(entry_amount, int) or not 50 <= entry_amount <= 1_000_000:
        raise ValueError("Invalid arena entry amount.")
    with Image.open(ASSETS / "template.png") as source:
        image = source.convert("RGB").resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(image)
    text = f"{entry_amount} SATS TO JOIN"
    for size in range(92, 31, -2):
        font = ImageFont.truetype(str(ASSETS / "DejaVuSansCondensed-Bold.ttf"), size)
        if draw.textlength(text, font=font) <= WIDTH - 140:
            break
    draw.text(
        (WIDTH // 2, 500),
        text,
        font=font,
        anchor="mm",
        fill="white",
        stroke_width=6,
        stroke_fill="black",
    )
    output = BytesIO()
    image.save(output, "JPEG", quality=88, optimize=True)
    return output.getvalue()
