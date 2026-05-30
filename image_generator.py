"""
image_generator.py — Generate 1080×1080 Instagram slides for Hebrew confessions.

Uses Cairo + Pango for text rendering — the only Python stack that handles
Hebrew RTL text correctly out of the box (no manual bidi, no character reversal).

Install on server:
    apt-get install -y python3-gi python3-gi-cairo gir1.2-pango-1.0 gir1.2-pangocairo-1.0
    pip install pycairo PyGObject
"""
from __future__ import annotations
import io
import os
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CANVAS_W = 1080
CANVAS_H = 1080
PAD      = 80

BG_COLOR        = (40/255,  55/255,  30/255)   # dark army green
TEXT_COLOR      = (1.0,     210/255, 0.0)       # yellow
ACCENT_COLOR    = (1.0,     210/255, 0.0)       # gold header
DIVIDER_COLOR   = (70/255,  90/255,  50/255)
WATERMARK_COLOR = (150/255, 170/255, 120/255)
ARROW_COLOR     = (180/255, 200/255, 150/255)

FONT_FAMILY   = "Noto Sans Hebrew"
FONT_SIZE_BODY   = 52
FONT_SIZE_HEADER = 56
FONT_SIZE_WM     = 34
FONT_SIZE_MIN    = 40
LINE_SPACING     = 6    # extra pixels between lines (Pango already adds some)

TOP_Y   = 160           # y where body text starts
FOOT_Y  = CANVAS_H - 90


# ---------------------------------------------------------------------------
# Cairo / Pango helpers
# ---------------------------------------------------------------------------

def _get_cairo_context(surface):
    import cairo
    return cairo.Context(surface)


def _make_pango_layout(ctx, text: str, font_size: int, width_px: int):
    """Create a Pango layout with RTL alignment for Hebrew text."""
    import gi
    gi.require_version('Pango', '1.0')
    gi.require_version('PangoCairo', '1.0')
    from gi.repository import Pango, PangoCairo

    layout = PangoCairo.create_layout(ctx)
    layout.set_text(text, -1)

    # Hebrew is RTL — Pango handles it automatically
    layout.set_alignment(Pango.Alignment.CENTER)
    layout.set_width(width_px * Pango.SCALE)

    fd = Pango.FontDescription(f"{FONT_FAMILY} {font_size}")
    layout.set_font_description(fd)

    return layout


def _draw_pango_layout(ctx, layout, x: float, y: float, color: tuple):
    import gi
    gi.require_version('PangoCairo', '1.0')
    from gi.repository import PangoCairo

    ctx.set_source_rgb(*color)
    ctx.move_to(x, y)
    PangoCairo.show_layout(ctx, layout)


def _layout_height(layout) -> int:
    """Return the pixel height of a rendered Pango layout."""
    import gi
    gi.require_version('Pango', '1.0')
    from gi.repository import Pango
    _w, h = layout.get_size()
    return h // Pango.SCALE


def _layout_line_height(ctx, font_size: int) -> int:
    """Return approximate single-line height for a given font size."""
    import gi
    gi.require_version('Pango', '1.0')
    gi.require_version('PangoCairo', '1.0')
    from gi.repository import Pango, PangoCairo
    layout = PangoCairo.create_layout(ctx)
    layout.set_text("אבג", -1)
    fd = Pango.FontDescription(f"{FONT_FAMILY} {font_size}")
    layout.set_font_description(fd)
    _w, h = layout.get_size()
    return h // Pango.SCALE + LINE_SPACING


# ---------------------------------------------------------------------------
# Slide renderer
# ---------------------------------------------------------------------------

def _render_slide(surface, text: str, post_number: int, watermark: str,
                  show_arrow: bool):
    """Draw all elements onto an existing Cairo ImageSurface."""
    import cairo

    ctx = _get_cairo_context(surface)
    usable_w = CANVAS_W - 2 * PAD

    # ── Background ────────────────────────────────────────────────────────
    ctx.set_source_rgb(*BG_COLOR)
    ctx.paint()

    # ── Header: post number ───────────────────────────────────────────────
    header_layout = _make_pango_layout(ctx, f"#{post_number}", FONT_SIZE_HEADER, usable_w)
    _draw_pango_layout(ctx, header_layout, PAD, 40, ACCENT_COLOR)
    print(f"   [imggen] header: #{post_number}")

    # ── Divider ───────────────────────────────────────────────────────────
    ctx.set_source_rgb(*DIVIDER_COLOR)
    ctx.set_line_width(2)
    ctx.move_to(PAD, TOP_Y - 20)
    ctx.line_to(CANVAS_W - PAD, TOP_Y - 20)
    ctx.stroke()

    # ── Body text ─────────────────────────────────────────────────────────
    text_area_h = FOOT_Y - 20 - TOP_Y
    body_layout  = _make_pango_layout(ctx, text, FONT_SIZE_BODY, usable_w)
    body_h       = _layout_height(body_layout)

    # Shrink font until text fits
    font_size = FONT_SIZE_BODY
    while body_h > text_area_h and font_size > FONT_SIZE_MIN:
        font_size -= 2
        body_layout = _make_pango_layout(ctx, text, font_size, usable_w)
        body_h      = _layout_height(body_layout)
        print(f"   [imggen] shrink → {font_size}px, height={body_h}px")

    body_y = TOP_Y + max(0, (text_area_h - body_h) // 2)
    print(f"   [imggen] body: font={font_size}px, height={body_h}px, y={body_y}")
    _draw_pango_layout(ctx, body_layout, PAD, body_y, TEXT_COLOR)

    # ── Footer divider + watermark ────────────────────────────────────────
    ctx.set_source_rgb(*DIVIDER_COLOR)
    ctx.move_to(PAD, FOOT_Y - 20)
    ctx.line_to(CANVAS_W - PAD, FOOT_Y - 20)
    ctx.stroke()

    wm_layout = _make_pango_layout(ctx, watermark, FONT_SIZE_WM, usable_w)
    _draw_pango_layout(ctx, wm_layout, PAD, FOOT_Y + 5, WATERMARK_COLOR)

    # ── Swipe arrow (non-final slides) ────────────────────────────────────
    if show_arrow:
        arrow_layout = _make_pango_layout(ctx, "❯", FONT_SIZE_HEADER, 100)
        _draw_pango_layout(ctx, arrow_layout, PAD, FOOT_Y - 10, ARROW_COLOR)
        print(f"   [imggen] swipe arrow drawn")


# ---------------------------------------------------------------------------
# Carousel split (by character count, then Pango decides line breaks)
# ---------------------------------------------------------------------------

def _split_into_pages(text: str, ctx, font_size: int, usable_w: int,
                      text_area_h: int) -> list[str]:
    """Split text into page-sized chunks based on what fits in text_area_h."""
    paragraphs = text.split('\n')
    pages: list[str] = []
    current_paras: list[str] = []

    for para in paragraphs:
        test_text = '\n'.join(current_paras + [para]).strip()
        if not test_text:
            current_paras.append(para)
            continue
        layout = _make_pango_layout(ctx, test_text, font_size, usable_w)
        h = _layout_height(layout)
        if h > text_area_h and current_paras:
            pages.append('\n'.join(current_paras).strip())
            current_paras = [para]
        else:
            current_paras.append(para)

    if current_paras:
        remaining = '\n'.join(current_paras).strip()
        if remaining:
            pages.append(remaining)

    return pages[:10]  # Instagram carousel max 10 slides


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _clean_text(text: str) -> str:
    """Remove characters Cairo/Pango can't render (emoji, etc.)."""
    import re
    result = []
    for ch in text:
        cp = ord(ch)
        if (0x0020 <= cp <= 0x007E        # Basic ASCII
                or 0x05B0 <= cp <= 0x05FF  # Hebrew
                or 0x2000 <= cp <= 0x206F  # General punctuation
                or ch in '\n'):
            result.append(ch)
        else:
            result.append(' ')
    return re.sub(r'  +', ' ', ''.join(result)).strip()


def generate_confession_slides(
    text: str,
    post_number: int,
    watermark: str = "וידויים צבאיים",
    body_font_path: Optional[str] = None,   # unused — Pango uses system fonts
    bold_font_path:  Optional[str] = None,
) -> list:
    """Generate 1080×1080 Cairo ImageSurface slides. Returns list of surfaces."""
    import cairo
    import gi
    gi.require_version('Pango', '1.0')
    gi.require_version('PangoCairo', '1.0')
    from gi.repository import Pango, PangoCairo

    print(f"\n[imggen] === generate_confession_slides (Cairo+Pango) ===")
    print(f"[imggen] post_number={post_number}, watermark='{watermark}'")

    text = _clean_text(text)
    print(f"[imggen] text ({len(text)} chars): {text[:100]}")

    usable_w   = CANVAS_W - 2 * PAD
    text_area_h = FOOT_Y - 20 - TOP_Y

    # Use a dummy surface to measure text
    dummy = cairo.ImageSurface(cairo.FORMAT_RGB24, CANVAS_W, CANVAS_H)
    dummy_ctx = cairo.Context(dummy)

    pages = _split_into_pages(text, dummy_ctx, FONT_SIZE_BODY, usable_w, text_area_h)
    print(f"[imggen] {len(pages)} page(s)")

    slides = []
    for idx, page_text in enumerate(pages):
        is_last = (idx == len(pages) - 1)
        surface  = cairo.ImageSurface(cairo.FORMAT_RGB24, CANVAS_W, CANVAS_H)
        _render_slide(surface, page_text, post_number, watermark, show_arrow=not is_last)
        slides.append(surface)
        print(f"[imggen] slide {idx+1}/{len(pages)} rendered")

    return slides


def slides_to_bytes(slides: list) -> list[bytes]:
    """Convert each Cairo ImageSurface to JPEG bytes."""
    from PIL import Image as PilImage
    result = []
    for i, surface in enumerate(slides):
        # Cairo gives us RGB24 (4 bytes/pixel, no alpha channel used)
        w, h    = surface.get_width(), surface.get_height()
        data    = bytes(surface.get_data())
        # Cairo RGB24 is BGRX — need to swap channels and drop padding
        img = PilImage.frombuffer('RGBA', (w, h), data, 'raw', 'BGRA', 0, 1)
        img = img.convert('RGB')
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
    import sys, os
    sample = (
        "אני חייל ביחידה קרבית ואני רוצה לספר על משהו שקרה לי לפני כמה חודשים. "
        "היינו בסיור לילי כשפתאום שמענו ירי מכיוון לא צפוי. "
        "כולם שכבו על הקרקע ואני פשוט קפאתי על המקום לשנייה."
    )
    if len(sys.argv) > 1:
        sample = sys.argv[1]

    slides = generate_confession_slides(sample, post_number=15467,
                                        watermark="וידויים צבאיים")
    imgs   = slides_to_bytes(slides)
    out_dir = '/tmp/ig_test'
    os.makedirs(out_dir, exist_ok=True)
    for i, data in enumerate(imgs):
        path = f'{out_dir}/slide_{i+1}.jpg'
        with open(path, 'wb') as f:
            f.write(data)
        print(f'Saved {path}')
    print(f'\n✅ {len(slides)} slide(s) in {out_dir}/')
