"""Deterministic summary calculations for the printable one-page summary.

Every number here is computed by code from stored entries. The model writes
nothing on the summary page. Key honesty rules:

- A day with no entry and no check-in is "not logged", never "no attack".
- Symptoms: "mentioned in X of N attack entries" — only explicit mentions
  count; silence is not absence, and explicit denials ("no nausea") are not
  mentions.
- Medications are grouped by normalized name (case/whitespace-insensitive);
  brand vs generic names ("Advil" vs "ibuprofen") are NOT merged, because
  merging would be interpretation.
- A day with both an attack entry and a check-in counts in both day counts;
  the calendar marks it as an attack day (attack takes visual precedence).
- Unconfirmed entries are included in counts but flagged; the caller must
  display the unconfirmed marker.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Optional

from .types import SYMPTOM_FIELDS, EntryStatus, EntryType

if TYPE_CHECKING:  # Entry is annotation-only; runtime is duck-typed.
    from .models import Entry


@dataclass(frozen=True)
class CalendarDay:
    day: date
    state: str  # "attack" | "no_attack" | "not_logged"
    unconfirmed: bool = False


@dataclass(frozen=True)
class DurationStats:
    n: int
    median_minutes: Optional[float] = None
    min_minutes: Optional[int] = None
    max_minutes: Optional[int] = None


@dataclass(frozen=True)
class MedicationStats:
    display_name: str
    attack_entries: int  # attacks it was taken for
    distinct_days: int  # distinct entry_dates
    outcomes: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Summary:
    window_start: date
    window_end: date
    generated_at: datetime
    attack_days: int
    no_attack_days: int
    not_logged_days: int
    attack_entries: int
    unconfirmed_entries: int
    calendar: tuple[CalendarDay, ...]
    duration: DurationStats
    severity_counts: dict[int, int]
    severity_n: int  # attacks with severity_function set
    pain_mean: Optional[float]  # mean of reported 0-10 scores; None if none
    pain_n: int  # attacks with a 0-10 score (missing scores ignored)
    symptoms: dict[str, int]  # symptom -> entries mentioning it
    symptoms_other: dict[str, int]  # free-text symptom -> entries mentioning it
    symptoms_n: int  # attack entries in window
    medications: tuple[MedicationStats, ...]
    triggers: dict[str, int]  # exact stated trigger -> entries mentioning it
    triggers_n: int  # attack entries in window
    weather_n: int  # attacks carrying a weather snapshot
    weather_tendency_n: int  # attacks with a pressure tendency
    weather_falling_n: int  # attacks with falling pressure


def _daterange(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def compute_summary(
    entries: list["Entry"],
    window_start: date,
    window_end: date,
    *,
    generated_at: Optional[datetime] = None,
) -> Summary:
    """Compute the summary over the inclusive window [window_start, window_end].

    Entries outside the window are ignored. `generated_at` defaults to now;
    pass it explicitly for deterministic output in tests and evaluation.
    """
    in_window = [
        e for e in entries if e.entry_date is not None and window_start <= e.entry_date <= window_end
    ]
    attacks = [e for e in in_window if e.entry_type == EntryType.ATTACK]
    checkins = [e for e in in_window if e.entry_type == EntryType.NO_ATTACK]

    attack_dates = {e.entry_date for e in attacks}
    checkin_dates = {e.entry_date for e in checkins}
    unconfirmed_dates = {
        e.entry_date
        for e in attacks
        if e.status != EntryStatus.CONFIRMED and e.entry_date is not None
    }

    calendar: list[CalendarDay] = []
    for day in _daterange(window_start, window_end):
        if day in attack_dates:
            state = "attack"
        elif day in checkin_dates:
            state = "no_attack"
        else:
            state = "not_logged"
        calendar.append(
            CalendarDay(day=day, state=state, unconfirmed=day in unconfirmed_dates)
        )

    durations = sorted(
        e.attack.duration_minutes
        for e in attacks
        if e.attack is not None and e.attack.duration_minutes is not None
    )
    if durations:
        duration = DurationStats(
            n=len(durations),
            median_minutes=float(statistics.median(durations)),
            min_minutes=durations[0],
            max_minutes=durations[-1],
        )
    else:
        duration = DurationStats(n=0)

    severities = [
        e.attack.severity_function
        for e in attacks
        if e.attack is not None and e.attack.severity_function is not None
    ]
    severity_counts = {1: 0, 2: 0, 3: 0}
    for s in severities:
        severity_counts[s] += 1

    pains = [
        e.attack.pain_0_10
        for e in attacks
        if e.attack is not None and e.attack.pain_0_10 is not None
    ]
    pain_mean = (sum(pains) / len(pains)) if pains else None

    symptoms: dict[str, int] = {name: 0 for name in SYMPTOM_FIELDS}
    other_counter: Counter[str] = Counter()
    for e in attacks:
        if e.attack is None:
            continue
        for name in SYMPTOM_FIELDS:
            if getattr(e.attack.symptoms, name) is True:
                symptoms[name] += 1
        for text in set(e.attack.symptoms.other):
            other_counter[text] += 1

    # Medications grouped by normalized name; display the first-seen form.
    med_days: dict[str, set[date]] = {}
    med_attacks: Counter[str] = Counter()
    med_display: dict[str, str] = {}
    med_outcomes: dict[str, Counter[str]] = {}
    for e in attacks:
        if e.attack is None:
            continue
        seen_in_entry: set[str] = set()
        for med in e.attack.acute_medications:
            key = " ".join(med.name.strip().lower().split())
            if not key:
                continue
            med_display.setdefault(key, med.name.strip())
            med_days.setdefault(key, set())
            if e.entry_date is not None:
                med_days[key].add(e.entry_date)
            if key not in seen_in_entry:
                # One attack counts once per medication even if listed twice.
                med_attacks[key] += 1
                seen_in_entry.add(key)
            outcome = med.outcome.value if med.outcome else "unknown"
            med_outcomes.setdefault(key, Counter())[outcome] += 1
    medications = tuple(
        MedicationStats(
            display_name=med_display[key],
            attack_entries=med_attacks[key],
            distinct_days=len(med_days[key]),
            outcomes=dict(sorted(med_outcomes[key].items())),
        )
        for key in sorted(med_display)
    )

    trigger_counter: Counter[str] = Counter()
    for e in attacks:
        if e.attack is None:
            continue
        for trigger in set(e.attack.reported_triggers):
            trigger_counter[trigger] += 1

    weather_n = 0
    weather_tendency_n = 0
    weather_falling_n = 0
    for e in attacks:
        snapshot = getattr(e, "weather", None)
        if snapshot is None:
            continue
        weather_n += 1
        if snapshot.pressure_tendency is None:
            continue
        weather_tendency_n += 1
        if snapshot.pressure_tendency == "falling":
            weather_falling_n += 1

    return Summary(
        window_start=window_start,
        window_end=window_end,
        generated_at=generated_at or datetime.now().astimezone(),
        attack_days=len(attack_dates),
        no_attack_days=len(checkin_dates),
        not_logged_days=sum(1 for c in calendar if c.state == "not_logged"),
        attack_entries=len(attacks),
        unconfirmed_entries=sum(1 for e in attacks if e.status != EntryStatus.CONFIRMED),
        calendar=tuple(calendar),
        duration=duration,
        severity_counts=severity_counts,
        severity_n=len(severities),
        pain_mean=pain_mean,
        pain_n=len(pains),
        symptoms=symptoms,
        symptoms_other=dict(sorted(other_counter.items())),
        symptoms_n=len(attacks),
        medications=medications,
        triggers=dict(sorted(trigger_counter.items())),
        triggers_n=len(attacks),
        weather_n=weather_n,
        weather_tendency_n=weather_tendency_n,
        weather_falling_n=weather_falling_n,
    )
