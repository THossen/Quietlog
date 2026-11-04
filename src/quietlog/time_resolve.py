"""Resolve a user-stated time expression against an entry's timestamp.

The extraction model returns `time_expression` verbatim ("since 2pm",
"this morning") and never does date arithmetic. This module deterministically
maps that string onto a datetime, or returns None when it cannot.

Supported patterns (case-insensitive):
  - Clock times: "2pm", "2:30pm", "14:30", "at 2pm", "since 2pm" -> EXACT.
    "around/about/approximately/roughly <time>" -> APPROXIMATE.
    "midnight" (00:00) and "noon"/"midday" (12:00) count as clock times.
    A clock time that falls in the future is moved back one day, because an
    attack always starts before it is logged ("since 11pm" logged at 1am).
    A clock time combined with "yesterday" ("9am yesterday") resolves to
    the previous day at that time.
  - Relative: "an hour ago", "two hours ago", "20 minutes ago",
    "half an hour ago", "3 days ago", "just now" -> APPROXIMATE.
    Quantities may be digits or English number words.
  - Dayparts: "this morning" (09:00), "this afternoon" (15:00),
    "this evening" (19:00), "tonight" (21:00), "last night" (22:00 prior
    day), "yesterday morning/afternoon/evening" -> APPROXIMATE.
    Same-day daypart defaults are capped at the reference time so the
    result is never in the future. A clock time in the same phrase wins
    over a daypart ("7am this morning" -> 07:00).
  - Meals: "breakfast" (08:00), "lunch"/"lunchtime" (12:30),
    "dinner" (18:30), same day, capped at reference -> APPROXIMATE.
  - Bare days: "today" (reference date), "yesterday" (prior date) ->
    DATE_ONLY with a 12:00 placeholder time. Consumers must check the
    precision flag and never present the placeholder as a real time.
  - Calendar dates: "November 1st", "Nov 1", "1st of November" resolve to
    the most recent such date on or before the reference -> DATE_ONLY.
    With a clock time ("November 1st at 8am") the stated date anchors the
    time -> EXACT. An explicit date always beats relative/clock-only
    readings of the same phrase.

Anything else (including None/empty) -> (None, UNKNOWN).

Phones send UTC timestamps, but users speak in wall-clock time ("8am" means
local 8am), so clock readings resolve in the server's local time and carry
its offset. Relative offsets ("two hours ago") are instants and unaffected.

`resolve_duration_range` computes minutes from a stated clock range
("from 8am to 12pm" -> 240, midnight crossings handled). Ranges need two
explicit clock readings; bare numbers ("from 8 to 12") are too ambiguous
and yield None.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional

from .types import TimePrecision

_CLOCK_RE = re.compile(
    r"\b(?:at|since|around|about|approximately|roughly)?\s*"
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)\b"
)
_24H_RE = re.compile(r"\b(?:at|since)?\s*([01]?\d|2[0-3]):(\d{2})\b")
_APPROX_WORD_RE = re.compile(r"\b(around|about|approximately|roughly)\b")

_ONES = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60}
_QTY_WORD = (
    r"(?:\d+|half an?|an?|"
    + "|".join(sorted(set(_ONES) | set(_TENS), key=len, reverse=True))
    + r"(?:[ -](?:one|two|three|four|five|six|seven|eight|nine))?)"
)
_AGO_RE = re.compile(r"\b(" + _QTY_WORD + r")\s+(hours?|minutes?|days?)\s+ago\b")

_DAYPART_HOURS = {
    "this morning": 9,
    "morning": 9,
    "this afternoon": 15,
    "afternoon": 15,
    "this evening": 19,
    "evening": 19,
    "tonight": 21,
}
_YESTERDAY_DAYPART_HOURS = {
    "yesterday morning": 9,
    "yesterday afternoon": 15,
    "yesterday evening": 19,
}
_MEAL_HOURS = {
    "breakfast": (8, 0),
    "lunch": (12, 30),
    "lunchtime": (12, 30),
    "dinner": (18, 30),
}

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATE_MDY_RE = re.compile(
    r"\b(" + _MONTH_ALT + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*,?\s*(\d{4}))?\b"
)
_DATE_DMY_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(" + _MONTH_ALT + r")\.?(?:\s*,?\s*(\d{4}))?\b"
)


def _parse_quantity(raw: str) -> Optional[float]:
    q = raw.strip().lower()
    if q in ("a", "an"):
        return 1
    if q in ("half a", "half an"):
        return 0.5
    if q.isdigit():
        return int(q)
    parts = re.split(r"[ -]", q)
    total = 0
    for part in parts:
        if part in _ONES:
            total += _ONES[part]
        elif part in _TENS:
            total += _TENS[part]
        else:
            return None
    return total if total > 0 else None


def _at(reference: datetime, day: datetime, hour: int, minute: int = 0) -> datetime:
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def resolve_attack_start(
    time_expression: Optional[str], reference: datetime
) -> tuple[Optional[datetime], TimePrecision]:
    """Resolve `time_expression` (as the user said it) against `reference`
    (the entry's saved timestamp). Never returns a future datetime."""
    if not time_expression or not time_expression.strip():
        return None, TimePrecision.UNKNOWN
    # Wall-clock phrases are local ("8am" is the user's 8am), while phones
    # send UTC: read the reference in local time first. Instants are
    # unchanged, so relative offsets are unaffected.
    reference = reference.astimezone()
    text = time_expression.strip().lower()

    # Explicit calendar dates anchor everything else in the phrase.
    day = _explicit_day(text, reference)
    if day is not None:
        return _resolve_explicit_day(reference, text, day)

    if re.search(r"\bjust now\b", text):
        return reference, TimePrecision.APPROXIMATE

    # Relative offsets ("two hours ago", "20 minutes ago", "3 days ago").
    m = _AGO_RE.search(text)
    if m:
        qty = _parse_quantity(m.group(1))
        unit = m.group(2).lower()
        if qty is not None:
            if unit.startswith("hour"):
                return reference - timedelta(hours=qty), TimePrecision.APPROXIMATE
            if unit.startswith("minute") and float(qty).is_integer():
                return reference - timedelta(minutes=int(qty)), TimePrecision.APPROXIMATE
            if unit.startswith("day") and float(qty).is_integer():
                return reference - timedelta(days=int(qty)), TimePrecision.APPROXIMATE

    # Clock times (checked before dayparts: "7am this morning" -> 07:00).
    clock = _CLOCK_RE.search(text)
    if clock:
        hour = int(clock.group(1)) % 12
        if clock.group(3).startswith("p"):
            hour += 12
        minute = int(clock.group(2)) if clock.group(2) else 0
        return _resolve_clock(reference, text, hour, minute)
    clock24 = _24H_RE.search(text)
    if clock24:
        return _resolve_clock(reference, text, int(clock24.group(1)), int(clock24.group(2)))
    if re.search(r"\bmidnight\b", text):
        return _resolve_clock(reference, text, 0, 0)
    if re.search(r"\b(noon|midday)\b", text):
        return _resolve_clock(reference, text, 12, 0)

    # Yesterday dayparts ("yesterday morning", ...) and "last night".
    for phrase, hour in _YESTERDAY_DAYPART_HOURS.items():
        if phrase in text:
            return _at(reference, reference - timedelta(days=1), hour), TimePrecision.APPROXIMATE
    if "last night" in text:
        return _at(reference, reference - timedelta(days=1), 22), TimePrecision.APPROXIMATE

    # Same-day dayparts, capped at the reference time.
    for phrase, hour in _DAYPART_HOURS.items():
        if re.search(r"\b" + re.escape(phrase) + r"\b", text):
            resolved = _at(reference, reference, hour)
            if resolved > reference:
                resolved = reference
            return resolved, TimePrecision.APPROXIMATE

    # Meals ("since lunch", "after dinner", ...).
    for meal, (hour, minute) in _MEAL_HOURS.items():
        if re.search(r"\b" + re.escape(meal) + r"\b", text):
            resolved = _at(reference, reference, hour, minute)
            if resolved > reference:
                resolved = reference
            return resolved, TimePrecision.APPROXIMATE

    # Bare day references -> date only.
    if re.search(r"\byesterday\b", text):
        return _at(reference, reference - timedelta(days=1), 12, 0), TimePrecision.DATE_ONLY
    if re.search(r"\btoday\b", text):
        return _at(reference, reference, 12, 0), TimePrecision.DATE_ONLY

    return None, TimePrecision.UNKNOWN


def _explicit_day(text: str, reference: datetime) -> Optional[date]:
    """Most recent calendar date named in the text, or None.

    Without a year, the date is the latest occurrence on or before the
    reference date. A stated year in the future, or an impossible day
    (November 31), yields None so other patterns can try.
    """
    m = _DATE_MDY_RE.search(text)
    if m:
        month_name, day, year = m.group(1), int(m.group(2)), m.group(3)
    else:
        m = _DATE_DMY_RE.search(text)
        if not m:
            return None
        day, month_name, year = int(m.group(1)), m.group(2), m.group(3)
    month = _MONTHS[month_name]
    try:
        if year is not None:
            named = date(int(year), month, day)
            return named if named <= reference.date() else None
        named = date(reference.year, month, day)
        if named > reference.date():
            named = date(reference.year - 1, month, day)
        return named
    except ValueError:
        return None


def _resolve_explicit_day(
    reference: datetime, text: str, day: date
) -> tuple[Optional[datetime], TimePrecision]:
    """Resolve a named date, anchoring any clock time the phrase states."""
    clocked: Optional[tuple[int, int]] = None
    clock = _CLOCK_RE.search(text)
    if clock:
        hour = int(clock.group(1)) % 12
        if clock.group(3).startswith("p"):
            hour += 12
        clocked = (hour, int(clock.group(2)) if clock.group(2) else 0)
    else:
        clock24 = _24H_RE.search(text)
        if clock24:
            clocked = (int(clock24.group(1)), int(clock24.group(2)))
        elif re.search(r"\bmidnight\b", text):
            clocked = (0, 0)
        elif re.search(r"\b(noon|midday)\b", text):
            clocked = (12, 0)
    noon = datetime(day.year, day.month, day.day, 12, 0, tzinfo=reference.tzinfo)
    if clocked is None:
        return noon, TimePrecision.DATE_ONLY
    hour, minute = clocked
    if minute > 59 or hour > 23:
        return noon, TimePrecision.DATE_ONLY
    resolved = datetime(day.year, day.month, day.day, hour, minute,
                        tzinfo=reference.tzinfo)
    if resolved > reference:
        # A stated clock in the future: keep the honest date, drop the time.
        return noon, TimePrecision.DATE_ONLY
    precision = (
        TimePrecision.APPROXIMATE if _APPROX_WORD_RE.search(text) else TimePrecision.EXACT
    )
    return resolved, precision


_RANGE_SEP_RE = re.compile(r"^(?:\s*(?:to|till|until|through|thru|and|&|-|\u2013|\u2014)\s*)$")


def _clock_tokens(text: str) -> list[tuple[int, int, int]]:
    """(start, end, minutes-since-midnight) for each clock-like token."""
    tokens: list[tuple[int, int, int]] = []
    for m in _CLOCK_RE.finditer(text):
        hour = int(m.group(1)) % 12
        if m.group(3).startswith("p"):
            hour += 12
        minute = int(m.group(2)) if m.group(2) else 0
        tokens.append((m.start(), m.end(), hour * 60 + minute))
    for m in _24H_RE.finditer(text):
        tokens.append((m.start(), m.end(), int(m.group(1)) * 60 + int(m.group(2))))
    for m in re.finditer(r"\bmidnight\b", text):
        tokens.append((m.start(), m.end(), 0))
    for m in re.finditer(r"\b(noon|midday)\b", text):
        tokens.append((m.start(), m.end(), 12 * 60))
    return sorted(tokens)


def resolve_duration_range(time_expression: Optional[str]) -> Optional[int]:
    """Minutes spanned by a stated clock range, or None.

    Only pure ranges qualify: two explicit clock readings joined by nothing
    but a range word ("from 8am to 12pm", "8am-12pm", "10pm to 2am", which
    crosses midnight). An end at or before the start rolls past midnight;
    a full 24-hour span ("8am to 8am") is ambiguous, not a duration.
    """
    if not time_expression or not time_expression.strip():
        return None
    text = time_expression.strip().lower()
    tokens = _clock_tokens(text)
    for (_s1, e1, m1), (_s2, _e2, m2) in zip(tokens, tokens[1:]):
        if e1 > _s2:
            continue  # overlapping token readings; not a range
        if not _RANGE_SEP_RE.match(text[e1:_s2]):
            continue
        delta = m2 - m1
        if delta <= 0:
            delta += 24 * 60
        if delta >= 24 * 60:
            continue
        return delta
    return None


def _resolve_clock(
    reference: datetime, text: str, hour: int, minute: int
) -> tuple[Optional[datetime], TimePrecision]:
    if minute > 59 or hour > 23:
        return None, TimePrecision.UNKNOWN
    if re.search(r"\byesterday\b", text):
        resolved = _at(reference, reference - timedelta(days=1), hour, minute)
    else:
        resolved = _at(reference, reference, hour, minute)
        if resolved > reference:
            resolved -= timedelta(days=1)
    precision = (
        TimePrecision.APPROXIMATE if _APPROX_WORD_RE.search(text) else TimePrecision.EXACT
    )
    return resolved, precision
