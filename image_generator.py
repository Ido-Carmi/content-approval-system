"""
image_generator.py — Generate 1080×1080 Instagram slides for Hebrew confessions.

Uses Pillow for image generation. Hebrew RTL is handled by reversing each line
(line[::-1]) so the first Hebrew character ends up at the rightmost position
when drawn right-aligned. Readers start from the right — correct for Hebrew.

Font: NotoSansHebrew (apt-get install fonts-noto-core fonts-noto).
"""
from __future__ import annotations
import io
import os
import re
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CANVAS  = (1080, 1080)
PAD     = 80
TOP_Y   = 150       # y where body text starts
FOOT_Y  = CANVAS[1] - 90

BG_COLOR        = (40,  55,  30)    # dark army green
TEXT_COLOR      = (255, 210,   0)   # yellow body text
ACCENT_COLOR    = (255, 210,   0)   # gold post number
DIVIDER_COLOR   = (70,  90,  50)
WATERMARK_COLOR = (150, 170, 120)
ARROW_COLOR     = (180, 200, 150)

FONT_SIZE_BODY   = 58
FONT_SIZE_HEADER = 58
FONT_SIZE_WM     = 34
FONT_SIZE_ARROW  = 60
FONT_SIZE_MIN    = 40
LINE_SPACING     = 18


# ---------------------------------------------------------------------------
# Font discovery
# ---------------------------------------------------------------------------

def _find_font(candidates: list[str]) -> str:
    for path in candidates:
        if os.path.exists(path):
            print(f"   [imggen] font found: {path}")
            return path
    print(f"   [imggen] ⚠️  no font found in candidates, using default")
    return candidates[0]


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
    '/usr/share/fonts/truetype/noto/NotoSansHebrew-Regular.ttf',
    '/usr/share/fonts/truetype/freefont/FreeSans.ttf',
])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_font(path: str, size: int):
    from PIL import ImageFont
    if not os.path.exists(path):
        print(f"   [imggen] ⚠️  font missing: {path}, using default")
        return ImageFont.load_default()
    try:
        f = ImageFont.truetype(path, size)
        print(f"   [imggen] ✓ {os.path.basename(path)} {size}px")
        return f
    except Exception as e:
        print(f"   [imggen] ❌ font error: {e}")
        return ImageFont.load_default()


def _line_height(font, draw) -> int:
    from PIL import ImageDraw
    bb = draw.textbbox((0, 0), "אבג", font=font)
    return (bb[3] - bb[1]) + LINE_SPACING


def _clean_text(text: str) -> str:
    """Strip emoji and non-Hebrew/ASCII characters that PIL can't render."""
    result = []
    for ch in text:
        cp = ord(ch)
        if (0x0020 <= cp <= 0x007E or 0x05B0 <= cp <= 0x05FF or ch == '\n'):
            result.append(ch)
        else:
            result.append(' ')
    return re.sub(r'  +', ' ', ''.join(result)).strip()


def _wrap_rtl(text: str, font, max_width: int, draw) -> list[str]:
    """
    Wrap Hebrew text into lines that fit max_width pixels.
    Each line is returned REVERSED (line[::-1]) so that when PIL draws it
    right-aligned, the FIRST Hebrew character sits at the right edge —
    exactly where a right-to-left reader starts.
    """
    paragraphs = text.split('\n')
    visual_lines: list[str] = []

    for para in paragraphs:
        if not para.strip():
            visual_lines.append('')
            continue
        words = para.split()
        current: list[str] = []
        for word in words:
            test    = ' '.join(current + [word])
            bb      = draw.textbbox((0, 0), test[::-1], font=font)
            line_w  = bb[2] - bb[0]
            if line_w > max_width and current:
                visual_lines.append(' '.join(current)[::-1])
                current = [word]
            else:
                current.append(word)
        if current:
            visual_lines.append(' '.join(current)[::-1])

    print(f"   [imggen] {len(visual_lines)} line(s) after wrap")
    for i, l in enumerate(visual_lines):
        print(f"   [imggen]   [{i+1}] '{l[:50]}'")
    return visual_lines


# ---------------------------------------------------------------------------
# Slide renderer
# ---------------------------------------------------------------------------

def _draw_slide(lines: list[str], post_number: int, watermark: str,
                show_arrow: bool, body_font, bold_font):
    from PIL import Image, ImageDraw

    img  = Image.new('RGB', CANVAS, BG_COLOR)
    draw = ImageDraw.Draw(img)

    right_x  = CANVAS[0] - PAD

    # ── Header: post number (right-aligned, plain ASCII — no reversal needed) ─
    draw.text((right_x, 45), f"#{post_number}",
              font=bold_font, fill=ACCENT_COLOR, anchor='ra')

    # ── Dividers ──────────────────────────────────────────────────────────────
    draw.line([(PAD, TOP_Y - 12), (CANVAS[0] - PAD, TOP_Y - 12)],
              fill=DIVIDER_COLOR, width=2)
    draw.line([(PAD, FOOT_Y - 20), (CANVAS[0] - PAD, FOOT_Y - 20)],
              fill=DIVIDER_COLOR, width=2)

    # ── Body text — RIGHT-ALIGNED so Hebrew readers start from the right ──────
    lh           = _line_height(body_font, draw)
    text_area_h  = FOOT_Y - 20 - TOP_Y
    total_text_h = len(lines) * lh
    y = TOP_Y + max(0, (text_area_h - total_text_h) // 2)

    for line in lines:
        if line:
            draw.text((right_x, y), line,
                      font=body_font, fill=TEXT_COLOR, anchor='ra')
        y += lh

    # ── Watermark (centered) ──────────────────────────────────────────────────
    wm_font = _load_font(FONT_BODY, FONT_SIZE_WM)
    draw.text((CANVAS[0] // 2, FOOT_Y + 10),
              watermark[::-1],
              font=wm_font, fill=WATERMARK_COLOR, anchor='mm')

    # ── Swipe arrow on non-final slides ───────────────────────────────────────
    if show_arrow:
        draw.text((PAD + 10, FOOT_Y - 10), '❮',
                  font=body_font, fill=ARROW_COLOR, anchor='la')

    return img


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_confession_slides(
    text: str,
    post_number: int,
    watermark: str = "וידויים צבאיים",
    body_font_path: Optional[str] = None,
    bold_font_path:  Optional[str] = None,
) -> list:
    """Generate 1080×1080 PIL Image slides. Returns list of PIL Images."""
    from PIL import Image, ImageDraw

    print(f"\n[imggen] === generate_confession_slides ===")
    print(f"[imggen] post #{post_number}  |  font: {os.path.basename(FONT_BODY)}")

    text = _clean_text(text)
    print(f"[imggen] text ({len(text)} chars): {text[:100]}")

    bp = body_font_path or FONT_BODY
    hp = bold_font_path or FONT_BOLD

    usable_w  = CANVAS[0] - 2 * PAD
    text_area = FOOT_Y - 20 - TOP_Y

    tmp_img  = Image.new('RGB', CANVAS, BG_COLOR)
    tmp_draw = ImageDraw.Draw(tmp_img)

    # Find the right font size
    font_size = FONT_SIZE_BODY
    body_font = _load_font(bp, font_size)
    lines     = _wrap_rtl(text, body_font, usable_w, tmp_draw)
    lh        = _line_height(body_font, tmp_draw)

    while len(lines) * lh > text_area and font_size > FONT_SIZE_MIN:
        font_size -= 2
        body_font  = _load_font(bp, font_size)
        lines      = _wrap_rtl(text, body_font, usable_w, tmp_draw)
        lh         = _line_height(body_font, tmp_draw)

    print(f"[imggen] final font={font_size}px, lines={len(lines)}, "
          f"total_h={len(lines)*lh}px / {text_area}px available")

    bold_font = _load_font(hp, FONT_SIZE_HEADER)

    # Split into pages (carousel)
    lines_per_page = max(1, text_area // lh)
    pages = [lines[i:i+lines_per_page] for i in range(0, len(lines), lines_per_page)]
    pages = pages[:10]
    print(f"[imggen] {len(pages)} slide(s)")

    slides = []
    for idx, page_lines in enumerate(pages):
        is_last = (idx == len(pages) - 1)
        slide   = _draw_slide(page_lines, post_number, watermark,
                               show_arrow=not is_last,
                               body_font=body_font, bold_font=bold_font)
        slides.append(slide)

    return slides


def slides_to_bytes(slides: list) -> list[bytes]:
    result = []
    for i, img in enumerate(slides):
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=92)
        size_kb = buf.tell() // 1024
        buf.seek(0)
        result.append(buf.read())
        print(f"[imggen] slide {i+1}: {size_kb} KB")
    return result


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import sys
    sample = (sys.argv[1] if len(sys.argv) > 1 else
              "עכשיו מדברים שוב על גיוס מילואים לצפון, "
              "אחרי שהיינו שם כבר חודשיים. "
              "ומפה מתחיל הצרות האמיתיות.")
    slides = generate_confession_slides(sample, 15467, "וידויים צבאיים")
    os.makedirs('/tmp/ig_test', exist_ok=True)
    for i, slide in enumerate(slides):
        path = f'/tmp/ig_test/slide_{i+1}.jpg'
        slide.save(path, quality=92)
        print(f'Saved {path}')
