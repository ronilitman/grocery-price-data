"""Draw a chain's offers as one table image.

A phone reads a picture of a table faster than it reads sixty lines of text,
and the product shot is what makes a row recognisable - "גבינה לבנה 250 גרם"
is four words that could be any tub until you see the tub.

Two things make this less obvious than it looks.

**Hebrew is right-to-left and Pillow does not reorder it.** Drawn raw, the
string comes out reversed. ``python-bidi`` applies the Unicode bidi algorithm
so mixed Hebrew-and-digits text - which every row here is, because prices and
dates are Latin digits inside Hebrew - lands in the right visual order. Hebrew
needs no cursive shaping, so bidi is the whole job.

**A product picture may not exist.** The image host is keyed by barcode, and
weighed goods carry a short internal barcode it has never heard of. The app has
the same problem and answers it with ``imageId``; here a miss simply draws an
empty frame, which is the app's skeleton by another name. It is never an error.
"""

import io
import os
import urllib.request

from PIL import Image, ImageDraw, ImageFont
from bidi.algorithm import get_display

IMAGE_HOST = "https://m.pricez.co.il/ProductPictures"

# Ubuntu runners ship DejaVu, which covers Hebrew; the others are here so the
# same script renders on a Mac. First one that exists wins.
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansHebrew-Regular.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/SFHebrew.ttf",
]

WIDTH, ROW_H, PAD, THUMB = 880, 92, 20, 72
HEADER_H = 60

INK, MUTED, RULE, ZEBRA = "#16232B", "#7D8D93", "#E8ECEC", "#F4F6F6"
ACCENT, PRICE = "#0E6E6B", "#B23A2E"


def _font_path():
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    raise RuntimeError(
        "no Hebrew-capable font found; install fonts-dejavu-core "
        f"or add a path to FONT_CANDIDATES (tried: {', '.join(FONT_CANDIDATES)})")


def _thumb(barcode, image_id=None):
    """The product shot, or None. A miss is normal and never raises."""
    key = f"Pricez{image_id}" if image_id else barcode
    try:
        req = urllib.request.Request(f"{IMAGE_HOST}/{key}.jpg",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
        shot = Image.open(io.BytesIO(raw)).convert("RGB")
        shot.thumbnail((THUMB, THUMB))
        return shot
    except Exception:
        return None


def render_chain(chain_name, rows):
    """One PNG for one chain.

    ``rows`` are dicts carrying ``unit_price``, ``name``, ``terms`` and
    ``barcode``. Laid out right-to-left - picture at the right edge, text
    running leftward from it - because that is the direction the words read.
    The price sits on the left, in its own colour, so the column can be scanned
    without reading anything else.
    """
    font_path = _font_path()
    f_title = ImageFont.truetype(font_path, 30)
    f_price = ImageFont.truetype(font_path, 30)
    f_name = ImageFont.truetype(font_path, 25)
    f_terms = ImageFont.truetype(font_path, 19)

    height = HEADER_H + ROW_H * len(rows) + 10
    img = Image.new("RGB", (WIDTH, height), "#FFFFFF")
    draw = ImageDraw.Draw(img)

    draw.rectangle([0, 0, WIDTH, HEADER_H], fill=ACCENT)
    heading = f"{chain_name} · {len(rows)}"
    draw.text((WIDTH - PAD, HEADER_H // 2), get_display(heading),
              font=f_title, fill="#FFFFFF", anchor="rm")

    y = HEADER_H
    for i, row in enumerate(rows):
        if i % 2:
            draw.rectangle([0, y, WIDTH, y + ROW_H], fill=ZEBRA)

        shot = _thumb(row["barcode"], row.get("image_id"))
        box_x = WIDTH - PAD - THUMB
        if shot:
            img.paste(shot, (box_x + (THUMB - shot.width) // 2,
                             y + (ROW_H - shot.height) // 2))
        else:
            # The app's skeleton: a frame, not an error badge.
            draw.rectangle([box_x, y + 10, box_x + THUMB, y + 10 + THUMB],
                           outline=RULE, width=2)

        right = box_x - 16
        draw.text((right, y + 32), get_display(row["name"]),
                  font=f_name, fill=INK, anchor="rm")
        draw.text((right, y + 64), get_display(row["terms"]),
                  font=f_terms, fill=MUTED, anchor="rm")
        draw.text((PAD, y + ROW_H // 2), f"₪{row['unit_price']:g}",
                  font=f_price, fill=PRICE, anchor="lm")

        draw.line([0, y + ROW_H, WIDTH, y + ROW_H], fill=RULE)
        y += ROW_H

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()
