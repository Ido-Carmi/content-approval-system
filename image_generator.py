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
import os
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CANVAS  = (1080, 1080)
PAD     = 80           # horizontal padding
TOP_Y   = 160          # y where body text starts (below header)
FOOT_Y  = CANVAS[1] - 90  # y of watermark baseline

BG_COLOR        = (40, 55, 30)      # dark army green
TEXT_COLOR      = (255, 210, 0)     # yellow
ACCENT_COLOR    = (255, 210, 0)     # gold — post number
DIVIDER_COLOR   = (70, 90, 50)      # slightly lighter green
WATERMARK_COLOR = (150, 170, 120)   # muted olive
ARROW_COLOR     = (180, 200, 150)   # light olive

def _find_font(candidates: list[str]) -> str:
    """Return the first font path that exists on this system."""
    for path in candidates:
        if os.path.exists(path):
            print(f"   [imggen] ✓ font found: {path}")
            return path
    print(f"   [imggen] ⚠️  none of these fonts found: {candidates}")
    return candidates[0]  # will trigger fallback in _load_font

FONT_BODY = _find_font([
    '/usr/share/fonts/truetype/noto/NotoSansHebrew-Regular.ttf',
    '/usr/share/fonts/truetype/noto/NotoSansHebrew[wdth,wght].ttf',
    '/usr/share/fonts/noto/NotoSansHebrew-Regular.ttf',
    '/usr/share/fonts/truetype/culmus/MiriamCLM-Book.ttf',
    '/usr/share/fonts/truetype/freefont/FreeSans.ttf',
])
FONT_BOLD = _find_font([
    '/usr/share/fonts/truetype/noto/NotoSansHebrew-Bold.ttf',
    '/usr/share/fonts/truetype/noto/NotoSansHebrew[wdth,wght].ttf',
    '/usr/share/fonts/noto/NotoSansHebrew-Bold.ttf',
    '/usr/share/fonts/truetype/culmus/MiriamCLM-Bold.ttf',
    '/usr/share/fonts/truetype/freefont/FreeSansBold.ttf',
])
FONT_SIZE_BODY    = 58
FONT_SIZE_HEADER  = 56
FONT_SIZE_WM      = 36
FONT_SIZE_ARROW   = 60
FONT_SIZE_MIN     = 40
LINE_SPACING      = 20   # extra pixels between lines


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_font(path: str, size: int):
    from PIL import ImageFont
    print(f"   [imggen] loading font: {path} size={size}")
    if not os.path.exists(path):
        print(f"   [imggen] ⚠️  font not found at {path}, falling back to default")
        return ImageFont.load_default()
    try:
        font = ImageFont.truetype(path, size)
        print(f"   [imggen] ✓ font loaded: {os.path.basename(path)} {size}px")
        return font
    except Exception as e:
        print(f"   [imggen] ❌ font load error: {e} — falling back to default")
        return ImageFont.load_default()


def _line_height(font, draw) -> int:
    bb = draw.textbbox((0, 0), "אבג", font=font)
    lh = (bb[3] - bb[1]) + LINE_SPACING
    return lh


def _wrap_rtl(text: str, font, max_width: int, draw) -> list[str]:
    """Word-wrap Hebrew text (logical order) to fit max_width px.
    Forces RTL base direction so Hebrew is always rendered right-to-left.
    Returns visual (bidi-reordered) lines ready for PIL center-anchored drawing."""
    from bidi.algorithm import get_display

    paragraphs = text.split('\n')
    print(f"   [imggen] wrapping {len(paragraphs)} paragraph(s), max_width={max_width}px")
    visual_lines: list[str] = []

    for para in paragraphs:
        if not para.strip():
            visual_lines.append('')
            continue
        words = para.split()
        current: list[str] = []
        for word in words:
            test = ' '.join(current + [word])
            # Force RTL base direction — critical for Hebrew starting with punctuation/numbers
            visual_test = get_display(test, base_dir='R')
            bb = draw.textbbox((0, 0), visual_test, font=font)
            word_w = bb[2] - bb[0]
            if word_w > max_width and current:
                visual_lines.append(get_display(' '.join(current), base_dir='R'))
                current = [word]
            else:
                current.append(word)
        if current:
            visual_lines.append(get_display(' '.join(current), base_dir='R'))

    print(f"   [imggen] wrap result: {len(visual_lines)} visual line(s)")
    for i, l in enumerate(visual_lines):
        print(f"   [imggen]   line {i+1}: '{l[:60]}{'...' if len(l)>60 else ''}'")
    return visual_lines


def _draw_slide(lines: list[str], post_number: int, watermark: str,
                show_arrow: bool, body_font, bold_font):
    """Render a single 1080×1080 slide and return a PIL Image."""
    from PIL import Image, ImageDraw
    from bidi.algorithm import get_display

    img  = Image.new('RGB', CANVAS, BG_COLOR)
    draw = ImageDraw.Draw(img)

    right_x  = CANVAS[0] - PAD
    center_x = CANVAS[0] // 2

    print(f"   [imggen] drawing slide: {len(lines)} line(s), show_arrow={show_arrow}")

    # ── Header: post number — right-aligned, use body font (bold may be missing) ──
    draw.text((right_x, 55), f"#{post_number}",
              font=body_font, fill=ACCENT_COLOR, anchor='ra')
    print(f"   [imggen] header: #{post_number}")

    # Divider under header
    draw.line([(PAD, TOP_Y - 20), (CANVAS[0] - PAD, TOP_Y - 20)],
              fill=DIVIDER_COLOR, width=2)

    # ── Body text — right-aligned (correct for Hebrew RTL) ───────────────────
    lh = _line_height(body_font, draw)
    # Vertically center the text block in the available area
    text_area_h = FOOT_Y - 20 - TOP_Y
    total_text_h = len(lines) * lh
    y = TOP_Y + max(0, (text_area_h - total_text_h) // 2)
    for line in lines:
        if line:
            draw.text((right_x, y), line,
                      font=body_font, fill=TEXT_COLOR, anchor='ra')
        y += lh

    print(f"   [imggen] body text drawn, final y={y}")

    # ── Footer divider + watermark ────────────────────────────────────────────
    draw.line([(PAD, FOOT_Y - 20), (CANVAS[0] - PAD, FOOT_Y - 20)],
              fill=DIVIDER_COLOR, width=2)
    wm_text = get_display(watermark, base_dir='R')
    draw.text((center_x, FOOT_Y + 10), wm_text,
              font=body_font, fill=WATERMARK_COLOR, anchor='mm')
    print(f"   [imggen] watermark: '{watermark}'")

    # ── "Swipe" arrow (non-final slides) ─────────────────────────────────────
    if show_arrow:
        draw.text((PAD + 10, FOOT_Y - 10), '❯',
                  font=body_font, fill=ARROW_COLOR, anchor='la')
        print(f"   [imggen] swipe arrow drawn")

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
) -> list:
    """
    Generate one or more 1080×1080 slides for the given confession text.
    Returns a list of PIL Image objects.
    """
    from PIL import Image, ImageDraw

    print(f"\n[imggen] === generate_confession_slides ===")
    print(f"[imggen] post_number={post_number}, watermark='{watermark}'")
    print(f"[imggen] text length={len(text)} chars")
    print(f"[imggen] text preview: '{text[:80]}{'...' if len(text)>80 else ''}'")
    print(f"[imggen] body_font_path override: {body_font_path or 'none (using default)'}")
    print(f"[imggen] bold_font_path override: {bold_font_path or 'none (using default)'}")

    bp = body_font_path or FONT_BODY
    hp = bold_font_path or FONT_BOLD

    usable_w  = CANVAS[0] - 2 * PAD
    text_area = FOOT_Y - 20 - TOP_Y
    print(f"[imggen] canvas={CANVAS}, usable_w={usable_w}px, text_area_h={text_area}px")

    # Temp image for measurement
    tmp_img  = Image.new('RGB', CANVAS, BG_COLOR)
    tmp_draw = ImageDraw.Draw(tmp_img)

    font_size = FONT_SIZE_BODY
    print(f"[imggen] starting font size: {font_size}px, min: {FONT_SIZE_MIN}px")

    body_font = _load_font(bp, font_size)
    all_lines = _wrap_rtl(text, body_font, usable_w, tmp_draw)

    lh      = _line_height(body_font, tmp_draw)
    total_h = len(all_lines) * lh

    print(f"[imggen] at {font_size}px: {len(all_lines)} lines, "
          f"line_height={lh}px, total_h={total_h}px, text_area={text_area}px")

    # Shrink font until min size or fits in one slide
    shrink_steps = 0
    while total_h > text_area and font_size > FONT_SIZE_MIN:
        font_size  -= 2
        shrink_steps += 1
        body_font  = _load_font(bp, font_size)
        all_lines  = _wrap_rtl(text, body_font, usable_w, tmp_draw)
        lh         = _line_height(body_font, tmp_draw)
        total_h    = len(all_lines) * lh
        print(f"[imggen]   shrink → {font_size}px: {len(all_lines)} lines, "
              f"total_h={total_h}px")

    if shrink_steps:
        print(f"[imggen] font shrunk {shrink_steps}x to {font_size}px")
    else:
        print(f"[imggen] no shrinking needed at {font_size}px")

    # Split lines into pages
    lines_per_page = max(1, text_area // lh)
    print(f"[imggen] lines_per_page={lines_per_page} "
          f"(text_area={text_area} // lh={lh})")

    pages: list[list[str]] = []
    for i in range(0, len(all_lines), lines_per_page):
        pages.append(all_lines[i:i + lines_per_page])

    if len(pages) > 10:
        print(f"[imggen] ⚠️  {len(pages)} pages exceeds IG limit of 10 — truncating")
        pages = pages[:10]

    print(f"[imggen] will generate {len(pages)} slide(s)")

    bold_font = _load_font(hp, FONT_SIZE_HEADER)
    slides = []
    for idx, page_lines in enumerate(pages):
        is_last = (idx == len(pages) - 1)
        print(f"\n[imggen] --- slide {idx+1}/{len(pages)} "
              f"({'last' if is_last else 'has arrow'}) ---")
        slide = _draw_slide(
            lines=page_lines,
            post_number=post_number,
            watermark=watermark,
            show_arrow=not is_last,
            body_font=body_font,
            bold_font=bold_font,
        )
        slides.append(slide)

    print(f"\n[imggen] ✅ done — {len(slides)} slide(s) generated")
    return slides


def slides_to_bytes(slides: list) -> list[bytes]:
    """Convert each PIL Image to JPEG bytes (quality 92)."""
    print(f"[imggen] converting {len(slides)} slide(s) to JPEG bytes")
    result = []
    for i, img in enumerate(slides):
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=92)
        size_kb = buf.tell() // 1024
        buf.seek(0)
        data = buf.read()
        result.append(data)
        print(f"[imggen]   slide {i+1}: {size_kb} KB")
    return result


# ---------------------------------------------------------------------------
# Quick standalone test
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import sys
    sample = (
        "אני חייל ביחידה קרבית ואני רוצה לספר על משהו שקרה לי לפני כמה חודשים. "
        "היינו בסיור לילי כשפתאום שמענו ירי מכיוון לא צפוי. "
        "כולם שכבו על הקרקע ואני פשוט קפאתי על המקום לשנייה. "
        "לא הצלחתי להזיז את הרגליים. המחלקה שלי טיפלה במצב, "
        "ומאז אני חושב על זה כל לילה ותוהה אם אני מתאים לתפקיד."
    )
    if len(sys.argv) > 1:
        sample = sys.argv[1]

    slides = generate_confession_slides(sample, post_number=15467,
                                        watermark="וידויים צבאיים")
    out_dir = '/tmp/ig_test'
    os.makedirs(out_dir, exist_ok=True)
    for i, slide in enumerate(slides):
        path = f'{out_dir}/slide_{i+1}.jpg'
        slide.save(path, quality=92)
        print(f'Saved {path}')
    print(f'\n✅ Generated {len(slides)} slide(s) in {out_dir}/')
