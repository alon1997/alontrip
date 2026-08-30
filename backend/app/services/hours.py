"""Parse catalog ``opening_hours`` strings (T-019).

Catalog text is Google-style English from the seed script, e.g.
``Open 24 hours``, ``Open · Closes 5 PM``, ``Closed · Opens 10 AM`` — but
the T-045 audit (2026-08-28) found plenty of plain ranges the old parser
returned ``(None, None)`` for, silently disabling every hours-aware
feature for those spots: ``9:00-17:00`` (24h, hyphen/en/em dash),
``9 AM - 5 PM``, ``Mon 9:00-17:00; Tue closed``. All now parse; for
multi-day text the FIRST range stands (an honest approximation — the old
behavior for these strings was "unknown hours", which the planner treats
as always-open, i.e. strictly worse).
Narrow no-break spaces from Google are treated as normal spaces.
"""

from __future__ import annotations

import re

_CLOCK = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*([AP]M)",
    re.IGNORECASE,
)

# One open–close span: "9:00-17:00", "9 AM - 5 PM", "9:00–17:00", "9-5".
# AM/PM on either side is optional; a bare side is disambiguated in code
# (side > 12 → 24h clock; both ≤ 12 with no marker → classic 9-5 AM/PM).
_RANGE = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*([AP]M)?\s*(?:-|–|—|~|\bto\b)\s*"
    r"(\d{1,2})(?::(\d{2}))?\s*([AP]M)?",
    re.IGNORECASE,
)


def _norm(text: str) -> str:
    return (text or "").replace(" ", " ").replace("\xa0", " ")


def parse_clock(token: str) -> int | None:
    """``6 PM`` / ``6:45PM`` → minutes from midnight. 12 AM is 0; close 12 AM is 24:00."""
    match = _CLOCK.search(_norm(token).strip())
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    ampm = match.group(3).upper()
    if hour == 12:
        hour = 0 if ampm == "AM" else 12
    elif ampm == "PM":
        hour += 12
    return hour * 60 + minute


def _side(hour: int, minute: int, ampm: str | None, *, closing: bool) -> int:
    """Minutes from midnight for one range endpoint."""
    if ampm:
        ampm = ampm.upper()
        if hour == 12:
            hour = 0 if ampm == "AM" else 12
        elif ampm == "PM":
            hour += 12
    elif hour <= 12 and (closing or hour < 9):
        # No marker and ≤12: "9-5" reads 9 AM → 5 PM.
        if closing:
            hour += 12
    return hour * 60 + minute


def _first_range(raw: str) -> tuple[int, int] | None:
    for match in _RANGE.finditer(raw):
        open_h, open_m, open_ampm, close_h, close_m, close_ampm = match.groups()
        opens = _side(int(open_h), int(open_m or 0), open_ampm, closing=False)
        closes = _side(int(close_h), int(close_m or 0), close_ampm, closing=True)
        if closes == 0:
            closes = 24 * 60
        if closes <= opens and closes < 24 * 60:
            continue  # junk match (e.g. a floor/address number pair), keep scanning
        return opens, closes
    return None


def parse_hours(text: str | None) -> tuple[int | None, int | None]:
    """Return ``(open_min, close_min)``. Unknown side is None. 24h → (0, 24*60)."""
    if not text:
        return None, None
    raw = _norm(text)
    if "24 hour" in raw.lower():
        return 0, 24 * 60
    opens = None
    match_open = re.search(r"Opens\s+(\d{1,2}(?::\d{2})?\s*[AP]M)", raw, re.I)
    if match_open:
        opens = parse_clock(match_open.group(1))
    closes = None
    match_close = re.search(r"Closes\s+(\d{1,2}(?::\d{2})?\s*[AP]M)", raw, re.I)
    if match_close:
        closes = parse_clock(match_close.group(1))
        if closes == 0:
            closes = 24 * 60
    if opens is None and closes is None:
        ranged = _first_range(raw)
        if ranged is not None:
            return ranged
    return opens, closes


def clip_visit(clock: int, remain: int, hours_text: str | None) -> tuple[int, int]:
    """Wait until open if early; trim stay so the visit ends at close.

    Returns ``(clock, remain)``. ``remain`` 0 means the place is already closed.
    """
    opens, closes = parse_hours(hours_text)
    if opens is not None and clock < opens:
        clock = opens
    if closes is not None and clock >= closes:
        return clock, 0
    if closes is not None:
        remain = min(remain, closes - clock)
    return clock, max(0, remain)
