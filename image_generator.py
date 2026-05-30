"""
image_generator.py — Generate 1080×1080 Instagram slides for Hebrew confessions.

Each confession may span one or more slides:
- Minimum font size: 30px. Text is wrapped at that size.
- If the wrapped text exceeds one slide's text area, it's split across multiple slides.
- Every slide except the last shows a "swipe right" arrow in the bottom-left corner.
- Hebrew RTL support via python-bidi's get_display().
"""
from __future__ import annotations

import io
from typing import Optional

from PIL import Image, ImageDraw, ImageFont
from bidi.algorithm import get_display

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CANVAS  = (1080, 1080)
PAD     = 80           # horizontal padding
TOP_Y   = 160          # y where body text starts (below header)
FOOT_Y  = CANVAS[1] - 90  # y of watermark baseline

BG_COLOR        = (18, 18, 18)
TEXT_COLOR      = (235, 235, 230)
ACCENT_COLOR    = (255, 210, 0)    # gold — post number
DIVIDER_COLOR   = (55, 55, 55)
WATERMARK_COLOR = (130, 130, 130)
ARROW_COLOR     = (180, 180, 180)

FONT_BODY   = '/usr/share/fonts/truetype/noto/NotoSansHebrew-Regular.ttf'
FONT_BOLD   = '/usr/share/fonts/truetype/noto/NotoSansHebrew-Bold.ttf'
FONT_SIZE_BODY    = 44
FONT_SIZE_HEADER  = 52
FONT_SIZE_WM      = 32
FONT_SIZE_ARROW   = 56
FONT_SIZE_MIN     = 30
LINE_SPACING      = 16   # extra pixels between lines


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def _line_height(font: ImageFont.FreeTypeFont, draw: ImageDraw.ImageDraw) -> int:
    bb = draw.textbbox((0, 0), "אבג", font=font)
    return (bb[3] - bb[1]) + LINE_SPACING


def _wrap_rtl(text: str, font: ImageFont.FreeTypeFont,
              max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
    """Word-wrap Hebrew text (logical order) to fit max_width px.
    Returns visual (bidi-reordered) lines ready for PIL right-anchored drawing."""
    paragraphs = text.split('\n')
    visual_lines: list[str] = []
    for para in paragraphs:
        if not para.strip():
            visual_lines.append('')
            continue
        words = para.split()
        current: list[str] = []
        for word in words:
            test = ' '.join(current + [word])
            bb = draw.textbbox((0, 0), get_display(test), font=font)
            if (bb[2] - bb[0]) > max_width and current:
                visual_lines.append(get_display(' '.join(current)))
                current = [word]
            else:
                current.append(word)
        if current:
            visual_lines.append(get_display(' '.join(current)))
    return visual_lines


def _draw_slide(lines: list[str], post_number: int, watermark: str,
                show_arrow: bool,
                body_font: ImageFont.FreeTypeFont,
                bold_font: ImageFont.FreeTypeFont) -> Image.Image:
    """Render a single 1080×1080 slide and return a PIL Image."""
    img  = Image.new('RGB', CANVAS, BG_COLOR)
    draw = ImageDraw.Draw(img)

    usable_w = CANVAS[0] - 2 * PAD
    right_x  = CANVAS[0] - PAD

    # ── Header: post number ──────────────────────────────────────────────────
    header_font = _load_font(FONT_BOLD, FONT_SIZE_HEADER)
    num_text    = get_display(f"#{post_number}")
    draw.text((right_x, 55), num_text,
              font=header_font, fill=ACCENT_COLOR, anchor='ra')

    # Divider under header
    draw.line([(PAD, TOP_Y - 20), (CANVAS[0] - PAD, TOP_Y - 20)],
              fill=DIVIDER_COLOR, width=2)

    # ── Body text ─────────────────────────────────────────────────────────────
    lh  = _line_height(body_font, draw)
    y   = TOP_Y
    for line in lines:
        if line:
            draw.text((right_x, y), line,
                      font=body_font, fill=TEXT_COLOR, anchor='ra')
        y += lh

    # ── Footer divider + watermark ────────────────────────────────────────────
    draw.line([(PAD, FOOT_Y - 20), (CANVAS[0] - PAD, FOOT_Y - 20)],
              fill=DIVIDER_COLOR, width=2)
    wm_font = _load_font(FONT_BOLD, FONT_SIZE_WM)
    draw.text((CANVAS[0] // 2, FOOT_Y + 10),
              get_display(watermark),
              font=wm_font, fill=WATERMARK_COLOR, anchor='mm')

    # ── "Swipe" arrow (non-final slides) ─────────────────────────────────────
    if show_arrow:
        arrow_font = _load_font(FONT_BOLD, FONT_SIZE_ARROW)
        # Draw a left-pointing chevron (visually "swipe left" on Instagram RTL)
        draw.text((PAD + 10, FOOT_Y - 10), '❯',
                  font=arrow_font, fill=ARROW_COLOR, anchor='la')

    return img


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_confession_slides(
    text: str,
    post_number: int,
    watermark: str = "וידויים צבאיים",
    body_font_path: Optional[str] = None,
    bold_font_path: Optional[str] = None,
) -> list[Image.Image]:
    """
    Generate one or more 1080×1080 slides for the given confession text.

    If the text fits at FONT_SIZE_BODY on a single slide it returns a list of
    one image.  Otherwise the text is split across multiple slides (up to 10),
    each with a swipe-arrow except the last.

    Returns a list of PIL Image objects.
    """
    bp = body_font_path or FONT_BODY
    hp = bold_font_path or FONT_BOLD

    usable_w  = CANVAS[0] - 2 * PAD
    text_area = FOOT_Y - 20 - TOP_Y   # max pixel height available for body text

    # Use a temporary draw to measure text
    tmp_img  = Image.new('RGB', CANVAS, BG_COLOR)
    tmp_draw = ImageDraw.Draw(tmp_img)

    # Try the default body font size; shrink down to FONT_SIZE_MIN
    font_size = FONT_SIZE_BODY
    body_font = _load_font(bp, font_size)
    bold_font = _load_font(hp, FONT_SIZE_HEADER)

    all_lines = _wrap_rtl(text, body_font, usable_w, tmp_draw)

    lh = _line_height(body_font, tmp_draw)
    total_h = len(all_lines) * lh

    # Shrink font until min size or fits in one slide
    while total_h > text_area and font_size > FONT_SIZE_MIN:
        font_size -= 2
        body_font  = _load_font(bp, font_size)
        all_lines  = _wrap_rtl(text, body_font, usable_w, tmp_draw)
        lh         = _line_height(body_font, tmp_draw)
        total_h    = len(all_lines) * lh

    # Split lines into pages
    lines_per_page = max(1, text_area // lh)
    pages: list[list[str]] = []
    for i in range(0, len(all_lines), lines_per_page):
        pages.append(all_lines[i:i + lines_per_page])

    # Cap at 10 slides (Instagram carousel limit)
    pages = pages[:10]

    slides = []
    for idx, page_lines in enumerate(pages):
        is_last   = (idx == len(pages) - 1)
        slide = _draw_slide(
            lines=page_lines,
            post_number=post_number,
            watermark=watermark,
            show_arrow=not is_last,
            body_font=body_font,
            bold_font=bold_font,
        )
        slides.append(slide)

    return slides


def slides_to_bytes(slides: list[Image.Image]) -> list[bytes]:
    """Convert each PIL Image to JPEG bytes (quality 92)."""
    result = []
    for img in slides:
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=92)
        buf.seek(0)
        result.append(buf.read())
    return result


# ---------------------------------------------------------------------------
# Quick standalone test
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import os, sys
    sample = (
        "אני חייל ביחידה קרבית ואני רוצה לספר על משהו שקרה לי לפני כמה חודשים. "
        "היינו בסיור לילי כשפתאום שמענו ירי מכיוון לא צפוי. "
        "כולם שכבו על הקרקע ואני פשוט קפאתי על המקום לשנייה. "
        "לא הצלחתי להזיז את הרגליים. המחלקה שלי טיפלה במצב, "
        "ומאז אני חושב על זה כל לילה ותוהה אם אני מתאים לתפקיד."
    )
    slides = generate_confession_slides(sample, post_number=15467,
                                        watermark="וידויים צבאיים")
    out_dir = '/tmp/ig_test'
    os.makedirs(out_dir, exist_ok=True)
    for i, slide in enumerate(slides):
        path = f'{out_dir}/slide_{i+1}.jpg'
        slide.save(path, quality=92)
        print(f'Saved {path}')
    print(f'Generated {len(slides)} slide(s)')
