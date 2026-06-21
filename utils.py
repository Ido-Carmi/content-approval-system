from datetime import datetime


def calculate_textarea_height(text: str) -> int:
    """Calculate optimal textarea height (px) based on content length."""
    if not text:
        return 80

    chars_per_line = 85
    lines = text.count('\n') + 1
    wrapped_lines = 0
    for line in text.split('\n'):
        if len(line) == 0:
            wrapped_lines += 1
        else:
            wrapped_lines += max(1, (len(line) + chars_per_line - 1) // chars_per_line)

    total_lines = max(lines, wrapped_lines)
    height = int(total_lines * 21) + 15
    return max(80, min(400, height))


def get_hebrew_weekday(date_str: str) -> str:
    """Return Hebrew weekday name from an ISO datetime string."""
    try:
        dt = datetime.fromisoformat(date_str)
        weekdays = ['שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון']
        return weekdays[dt.weekday()]
    except Exception:
        return ''


def extract_referenced_numbers(text: str, exclude_number=None, current_number=None, window=100) -> list:
    """Extract confession numbers a post references, e.g. 'לכותב וידוי 15499' / '#15499'.

    Two detection passes:
    1. Cued numbers — any number following a reference cue (#, וידוי, לכותב, פוסט).
    2. Window numbers — when current_number is given, any bare number that falls in
       [current_number - window, current_number - 1] is very likely a reference to a
       recent post (e.g. current 15876 → catch a plain 15799 in the text), while years/
       ages/counts (37, 400, 2026) fall outside the window and are ignored.

    Returns sorted unique ints."""
    import re
    if not text:
        return []
    nums = set()
    pattern = r"(?:#|וידוי|לכותב|פוסט)\s*(?:וידוי\s*)?(?:מס['׳.]*\s*)?#?\s*(\d{2,6})"
    for m in re.finditer(pattern, text):
        try:
            nums.add(int(m.group(1)))
        except ValueError:
            pass

    # Pass 2: bare numbers within the recent-posts window.
    if current_number is not None:
        try:
            cur = int(current_number)
            lo, hi = cur - int(window), cur - 1
            for m in re.finditer(r"\d{2,6}", text):
                try:
                    n = int(m.group(0))
                except ValueError:
                    continue
                if lo <= n <= hi:
                    nums.add(n)
        except (ValueError, TypeError):
            pass

    if exclude_number is not None:
        try:
            nums.discard(int(exclude_number))
        except (ValueError, TypeError):
            pass
    return sorted(nums)
