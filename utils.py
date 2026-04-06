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
