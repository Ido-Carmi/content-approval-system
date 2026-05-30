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

BG_COLOR        = (0x46, 0x65, 0x29)   # #466529
TEXT_COLOR      = (0xfa, 0xca, 0x19)   # #FACA19
ACCENT_COLOR    = (0xfa, 0xca, 0x19)   # #FACA19
DIVIDER_COLOR   = (0x57, 0x7a, 0x33)   # slightly lighter green
WATERMARK_COLOR = (0x99, 0xb3, 0x72)   # muted olive
ARROW_COLOR     = (0xcc, 0xdf, 0xa0)   # light olive

FONT_SIZE_BODY   = 70
FONT_SIZE_HEADER = 62
FONT_SIZE_WM     = 36
FONT_SIZE_ARROW  = 70
FONT_SIZE_MIN    = 60
LINE_SPACING     = 28


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


# Assistant is the preferred Hebrew font (Google Fonts, clean modern look).
# Falls back to DejaVu (full Unicode) then Noto Hebrew.
_HERE = os.path.dirname(os.path.abspath(__file__))

FONT_BODY = _find_font([
    os.path.join(_HERE, 'fonts', 'Assistant.ttf'),
    os.path.join(_HERE, 'fonts', 'Assistant-Regular.ttf'),
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    '/usr/share/fonts/truetype/noto/NotoSansHebrew-Regular.ttf',
    '/usr/share/fonts/truetype/culmus/MiriamCLM-Book.ttf',
    '/usr/share/fonts/truetype/freefont/FreeSans.ttf',
])
FONT_BOLD = _find_font([
    os.path.join(_HERE, 'fonts', 'Assistant.ttf'),
    os.path.join(_HERE, 'fonts', 'Assistant-Bold.ttf'),
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    '/usr/share/fonts/truetype/noto/NotoSansHebrew-Bold.ttf',
    '/usr/share/fonts/truetype/culmus/MiriamCLM-Bold.ttf',
    '/usr/share/fonts/truetype/freefont/FreeSansBold.ttf',
])

# Check if Pillow was compiled with RAQM (proper RTL shaping engine).
# If yes, we can draw Hebrew directly without manual character reversal.
try:
    from PIL import features as _pil_features
    HAS_RAQM = _pil_features.check_feature('raqm')
except Exception:
    HAS_RAQM = False
print(f"[imggen] RAQM support: {HAS_RAQM}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_font(path: str, size: int, weight: int = 400):
    from PIL import ImageFont
    if not os.path.exists(path):
        print(f"   [imggen] ⚠️  font missing: {path}, using default")
        return ImageFont.load_default()
    try:
        f = ImageFont.truetype(path, size)
        # Set variable font weight axis if supported (Pillow 9.2+)
        try:
            f.set_variation_by_axes([weight])
            print(f"   [imggen] ✓ {os.path.basename(path)} {size}px weight={weight}")
        except Exception:
            print(f"   [imggen] ✓ {os.path.basename(path)} {size}px (fixed weight)")
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
            # If a single word is wider than max_width, break it at character level
            single_bb = draw.textbbox((0, 0), word if HAS_RAQM else word[::-1], font=font)
            if single_bb[2] - single_bb[0] > max_width:
                if current:
                    done = ' '.join(current)
                    visual_lines.append(done if HAS_RAQM else done[::-1])
                    current = []
                # Break word into chunks that fit
                chunk = ''
                for ch in word:
                    test_chunk = chunk + ch
                    bb = draw.textbbox((0, 0), test_chunk if HAS_RAQM else test_chunk[::-1], font=font)
                    if bb[2] - bb[0] > max_width and chunk:
                        visual_lines.append(chunk if HAS_RAQM else chunk[::-1])
                        chunk = ch
                    else:
                        chunk = test_chunk
                if chunk:
                    current = [chunk]
                continue

            test     = ' '.join(current + [word])
            measure  = test if HAS_RAQM else test[::-1]
            bb       = draw.textbbox((0, 0), measure, font=font)
            line_w   = bb[2] - bb[0]
            if line_w > max_width and current:
                done = ' '.join(current)
                visual_lines.append(done if HAS_RAQM else done[::-1])
                current = [word]
            else:
                current.append(word)
        if current:
            line = ' '.join(current)
            visual_lines.append(line if HAS_RAQM else line[::-1])

    print(f"   [imggen] RAQM={HAS_RAQM}, {len(visual_lines)} line(s) after wrap")
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

    # ── Body text — centered horizontally, starts near top ───────────────────
    lh           = _line_height(body_font, draw)
    text_area_h  = FOOT_Y - 20 - TOP_Y
    total_text_h = len(lines) * lh
    top_pad      = min(30, max(0, (text_area_h - total_text_h) // 4))
    y        = TOP_Y + top_pad
    center_x = CANVAS[0] // 2

    for line in lines:
        if line:
            if HAS_RAQM:
                draw.text((center_x, y), line,
                          font=body_font, fill=TEXT_COLOR, anchor='ma',
                          direction='rtl', language='he')
            else:
                draw.text((center_x, y), line,
                          font=body_font, fill=TEXT_COLOR, anchor='ma')
        y += lh

    # ── Watermark (centered) ──────────────────────────────────────────────────
    wm_font = _load_font(FONT_BODY, FONT_SIZE_WM)
    draw.text((CANVAS[0] // 2, FOOT_Y + 10),
              watermark,
              font=wm_font, fill=WATERMARK_COLOR, anchor='mm')

    # ── Swipe arrow — proper → arrow below the footer divider ────────────────
    if show_arrow:
        ax  = CANVAS[0] - PAD - 10   # rightmost point of arrowhead
        ay  = FOOT_Y + 28            # below the divider line
        sw  = 40                     # shaft length
        hw  = 18                     # arrowhead width
        hh  = 12                     # arrowhead half-height
        # Shaft
        draw.line([(ax - sw - hw, ay), (ax - hw, ay)],
                  fill=ARROW_COLOR, width=5)
        # Arrowhead (filled triangle)
        draw.polygon([(ax - hw, ay - hh), (ax, ay), (ax - hw, ay + hh)],
                     fill=ARROW_COLOR)

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

    bold_font = _load_font(hp, FONT_SIZE_HEADER, weight=700)

    # Sentence-aware slide distribution:
    # 1. Split text into sentences
    # 2. Wrap each sentence to know its line count
    # 3. Distribute sentences across slides as evenly as possible
    #    (last slide is allowed to be lighter)
    import math, re as _re

    lines_per_page = max(1, text_area // lh)

    # Split into sentences at . ? ! and paragraph breaks
    raw_sentences: list[str] = []
    for para in text.split('\n'):
        para = para.strip()
        if not para:
            raw_sentences.append('')        # paragraph break
            continue
        parts = _re.split(r'(?<=[.?!…])\s+', para)
        for p in parts:
            if p.strip():
                raw_sentences.append(p.strip())

    # Wrap each sentence independently
    sent_wrapped: list[tuple[bool, list[str]]] = []   # (is_break, lines)
    for s in raw_sentences:
        if not s:
            sent_wrapped.append((True, []))
        else:
            wl = _wrap_rtl(s, body_font, usable_w, tmp_draw)
            sent_wrapped.append((False, wl))

    total_content = sum(len(wl) for brk, wl in sent_wrapped if not brk)
    n_slides = min(10, max(1, math.ceil(total_content / lines_per_page)))
    target   = total_content / n_slides      # float — aim for this per slide

    print(f"[imggen] {len(raw_sentences)} sentence(s), {total_content} lines "
          f"→ {n_slides} slide(s), target={target:.1f} lines/slide")

    pages: list[list[str]] = []
    cur:   list[str]       = []
    cur_n: float           = 0.0

    for is_brk, wl in sent_wrapped:
        if is_brk:
            if cur:
                cur.append('')   # preserve paragraph gap within a slide
            continue

        # Hard limit: never exceed lines_per_page on any slide
        hard_overflow = (cur_n + len(wl) > lines_per_page) and cur

        # Soft limit: close slide once we hit target (unless last slide)
        soft_limit = (cur_n >= target) and (len(pages) < n_slides - 1)

        if hard_overflow or soft_limit:
            while cur and not cur[-1]:
                cur.pop()
            pages.append(cur)
            cur, cur_n = list(wl), float(len(wl))
        else:
            cur.extend(wl)
            cur_n += len(wl)

    if cur:
        while cur and not cur[-1]:
            cur.pop()
        pages.append(cur)

    pages = [p for p in pages if any(l for l in p)][:10]
    print(f"[imggen] {len(pages)} slide(s) after sentence distribution")

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
