"""Onset time resolution: explicit dates anchor, clocks never float alone."""

from datetime import datetime, timedelta, timezone

from quietlog.time_resolve import resolve_attack_start, resolve_duration_range
from quietlog.types import TimePrecision

REF = datetime(2026, 11, 3, 6, 13, tzinfo=timezone.utc)


def test_explicit_month_day_is_date_only():
    start, prec = resolve_attack_start("on November 1st", REF)
    assert start is not None
    assert (start.year, start.month, start.day) == (2026, 11, 1)
    assert prec == TimePrecision.DATE_ONLY


def test_explicit_date_forms():
    for text in ("Nov 1", "november 1", "1st of november", "1 November",
                 "November 1st, 2026", "since November 1st"):
        start, prec = resolve_attack_start(text, REF)
        assert start is not None and (start.year, start.month, start.day) == (2026, 11, 1), text
        assert prec == TimePrecision.DATE_ONLY, text


def test_explicit_date_with_clock_time_is_exact():
    start, prec = resolve_attack_start("November 1st at 8 am", REF)
    assert start is not None
    assert (start.year, start.month, start.day, start.hour) == (2026, 11, 1, 8)
    assert prec == TimePrecision.EXACT
    assert start.utcoffset() == REF.astimezone().utcoffset()


def test_explicit_date_wins_over_clock_fallback():
    # The clock-only rule would say Nov 2; the stated date anchors Nov 1.
    start, _ = resolve_attack_start("November 1st at 8 am", REF)
    assert start is not None and start.day == 1


def test_explicit_date_most_recent_across_year():
    ref = datetime(2027, 1, 2, 12, 0, tzinfo=timezone.utc)
    start, prec = resolve_attack_start("December 30", ref)
    assert start is not None and (start.year, start.month, start.day) == (2026, 12, 30)
    assert prec == TimePrecision.DATE_ONLY


def test_explicit_date_invalid_day_falls_through():
    assert resolve_attack_start("November 31", REF) == (None, TimePrecision.UNKNOWN)


def test_clock_without_date_keeps_future_minus_day_rule():
    # Wall-clock rules run in local time, so the expected day is computed
    # from the local reading of the reference (robust in any zone).
    start, prec = resolve_attack_start("at 8am", REF)
    assert start is not None and (start.hour, start.minute) == (8, 0)
    assert prec == TimePrecision.EXACT
    assert start.utcoffset() == REF.astimezone().utcoffset()
    local = REF.astimezone()
    same_day = local.replace(hour=8, minute=0, second=0, microsecond=0)
    expected = same_day.date() if same_day <= local else (same_day - timedelta(days=1)).date()
    assert start.date() == expected


def test_relative_and_bare_days_unchanged():
    start, prec = resolve_attack_start("two hours ago", REF)
    assert start == REF - timedelta(hours=2) and prec == TimePrecision.APPROXIMATE
    start, prec = resolve_attack_start("yesterday", REF)
    assert start is not None
    assert start.date() == (REF.astimezone() - timedelta(days=1)).date()
    assert prec == TimePrecision.DATE_ONLY


def test_duration_range_between_clock_times():
    assert resolve_duration_range("from 8am to 12pm") == 240
    assert resolve_duration_range("8am-12pm") == 240
    assert resolve_duration_range("8:30am to 12:15pm") == 225
    assert resolve_duration_range("between 8am and 12pm") == 240
    assert resolve_duration_range("10pm to 2am") == 240


def test_duration_range_rejects_ambiguous():
    assert resolve_duration_range("since 2pm") is None
    assert resolve_duration_range("8am to 8am") is None
    assert resolve_duration_range("from 8 to 12") is None
    assert resolve_duration_range("") is None
    assert resolve_duration_range(None) is None
    assert resolve_duration_range("Migraine since 12 am. Only ate candy.") is None
