"""Deterministic summary: hand-built entries must produce exact numbers."""

from datetime import date, datetime

from quietlog.models import AcuteMedication, AttackDetails, Entry, EntryStatus, EntryType
from quietlog.summary import compute_summary


def _atk(day, status=EntryStatus.CONFIRMED, **attack_kw):
    attack = AttackDetails(**attack_kw)
    return Entry(
        entry_type=EntryType.ATTACK,
        created_at=datetime(2026, 10, day, 14, 0),
        entry_date=date(2026, 10, day),
        status=status,
        attack=attack,
    )


def _checkin(day):
    return Entry(
        entry_type=EntryType.NO_ATTACK,
        created_at=datetime(2026, 10, day, 21, 0),
        entry_date=date(2026, 10, day),
    )


def _scenario():
    return [
        _atk(1, duration_minutes=120, severity_function=2,
             symptoms={"nausea": True, "light_sensitivity": True, "vomiting": False},
             acute_medications=[AcuteMedication(name="ibuprofen", dose="200mg",
                                                outcome="relief")],
             reported_triggers=["skipped lunch"]),
        _atk(1, status=EntryStatus.UNCONFIRMED, duration_minutes=60, severity_function=3,
             symptoms={"nausea": True},
             acute_medications=[AcuteMedication(name="Ibuprofen", dose="400mg",
                                                outcome="partial")],
             reported_triggers=["skipped lunch", "bad sleep"]),
        _atk(3, severity_function=1,
             acute_medications=[AcuteMedication(name="Advil", dose="200mg",
                                                outcome="relief")]),
        _atk(5, status=EntryStatus.NEEDS_PROCESSING),
        _checkin(2),
        _checkin(4),
        _checkin(3),  # same day as an attack: counts in both, calendar shows attack
    ]


def test_counts_with_honest_denominators():
    s = compute_summary(_scenario(), date(2026, 10, 1), date(2026, 10, 7),
                        generated_at=datetime(2026, 10, 8, 9, 0))
    assert (s.attack_days, s.no_attack_days, s.not_logged_days) == (3, 3, 2)
    assert s.attack_entries == 4
    assert s.unconfirmed_entries == 2  # unconfirmed + needs_processing
    assert len(s.calendar) == 7


def test_calendar_marks_attack_precedence_and_unconfirmed():
    s = compute_summary(_scenario(), date(2026, 10, 1), date(2026, 10, 7),
                        generated_at=datetime(2026, 10, 8, 9, 0))
    cells = {c.day.day: c for c in s.calendar}
    assert cells[1].state == "attack" and cells[1].unconfirmed is True
    assert cells[2].state == "no_attack" and cells[2].unconfirmed is False
    assert cells[3].state == "attack" and cells[3].unconfirmed is False
    assert cells[5].state == "attack" and cells[5].unconfirmed is True
    assert cells[6].state == "not_logged"
    assert cells[7].state == "not_logged"


def test_duration_median_and_range():
    s = compute_summary(_scenario(), date(2026, 10, 1), date(2026, 10, 7),
                        generated_at=datetime(2026, 10, 8, 9, 0))
    assert s.duration.n == 2
    assert s.duration.median_minutes == 90.0
    assert (s.duration.min_minutes, s.duration.max_minutes) == (60, 120)


def test_severity_counts():
    s = compute_summary(_scenario(), date(2026, 10, 1), date(2026, 10, 7),
                        generated_at=datetime(2026, 10, 8, 9, 0))
    assert s.severity_counts == {1: 1, 2: 1, 3: 1}
    assert s.severity_n == 3


def test_pain_mean_ignores_missing_scores():
    entries = [
        _atk(1, pain_0_10=4),
        _atk(2, pain_0_10=8),
        _atk(3, duration_minutes=60),  # no score: ignored, not zero
        _checkin(4),  # check-ins never contribute
    ]
    s = compute_summary(entries, date(2026, 10, 1), date(2026, 10, 7),
                        generated_at=datetime(2026, 10, 8, 9, 0))
    assert s.pain_mean == 6.0
    assert s.pain_n == 2

    s = compute_summary([_atk(1)], date(2026, 10, 1), date(2026, 10, 7),
                        generated_at=datetime(2026, 10, 8, 9, 0))
    assert s.pain_mean is None
    assert s.pain_n == 0


def test_symptoms_count_mentions_only():
    s = compute_summary(_scenario(), date(2026, 10, 1), date(2026, 10, 7),
                        generated_at=datetime(2026, 10, 8, 9, 0))
    assert s.symptoms["nausea"] == 2
    assert s.symptoms["light_sensitivity"] == 1
    assert s.symptoms["vomiting"] == 0  # explicit denial is not a mention
    assert s.symptoms["aura"] == 0  # silence is not presence
    assert s.symptoms_n == 4


def test_medication_grouping_and_outcomes():
    s = compute_summary(_scenario(), date(2026, 10, 1), date(2026, 10, 7),
                        generated_at=datetime(2026, 10, 8, 9, 0))
    by_name = {m.display_name: m for m in s.medications}
    ibu = by_name["ibuprofen"]  # case variants grouped, first-seen display
    assert ibu.attack_entries == 2
    assert ibu.distinct_days == 1
    assert ibu.outcomes == {"partial": 1, "relief": 1}
    advil = by_name["Advil"]  # brand NOT merged with generic
    assert (advil.attack_entries, advil.distinct_days) == (1, 1)
    assert advil.outcomes == {"relief": 1}


def test_triggers_with_counts_and_n():
    s = compute_summary(_scenario(), date(2026, 10, 1), date(2026, 10, 7),
                        generated_at=datetime(2026, 10, 8, 9, 0))
    assert s.triggers == {"bad sleep": 1, "skipped lunch": 2}
    assert s.triggers_n == 4


def test_empty_window():
    s = compute_summary([], date(2026, 10, 1), date(2026, 10, 7),
                        generated_at=datetime(2026, 10, 8, 9, 0))
    assert (s.attack_days, s.no_attack_days, s.not_logged_days) == (0, 0, 7)
    assert s.duration.n == 0 and s.duration.median_minutes is None
    assert s.severity_counts == {1: 0, 2: 0, 3: 0}
    assert s.medications == () and s.triggers == {}


def test_entries_outside_window_are_ignored():
    entries = _scenario() + [_atk(15, severity_function=3)]
    s = compute_summary(entries, date(2026, 10, 1), date(2026, 10, 7),
                        generated_at=datetime(2026, 10, 8, 9, 0))
    assert s.attack_entries == 4
    assert s.severity_counts[3] == 1
