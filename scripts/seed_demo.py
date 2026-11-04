"""Seed demo headache entries for past months.

Generates realistic attack entries (transcript + structured details built from
the same random draw, so they agree with each other) for September and October
2026. Offline and deterministic: no LLM calls, fixed seed unless overridden.

Only ever INSERTs new rows; existing entries are never touched. Seeded ids are
recorded in a sidecar file so `--clear` can remove exactly what was added.

Usage:
    python scripts/seed_demo.py                 # seed Sep+Oct, 10-15 days each
    python scripts/seed_demo.py --dry-run       # print what would be added
    python scripts/seed_demo.py --clear         # remove previously seeded rows
"""

from __future__ import annotations

import argparse
import calendar
import json
import random
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from quietlog import db as db_mod  # noqa: E402
from quietlog.models import AcuteMedication, AttackDetails, Entry, SymptomSet  # noqa: E402
from quietlog.settings import load_settings  # noqa: E402
from quietlog.types import EntryStatus, EntryType, MedicationOutcome, TimePrecision  # noqa: E402

SIDECAR = ROOT / "data" / "demo_seed.json"

PAIN_WORDS = {
    3: ["dull", "mild", "nagging"],
    4: ["annoying", "dull", "nagging"],
    5: ["bad", "throbbing", "rough"],
    6: ["bad", "throbbing", "pounding"],
    7: ["really bad", "pounding", "splitting"],
    8: ["brutal", "splitting", "blinding"],
    9: ["brutal", "unbearable", "blinding"],
}

# (phrase for transcript, hour, minute, precision)
STARTS = [
    ("woke up with it", 7, 10, TimePrecision.APPROXIMATE),
    ("around 7am", 7, 0, TimePrecision.APPROXIMATE),
    ("at 8am", 8, 0, TimePrecision.EXACT),
    ("mid-morning", 10, 15, TimePrecision.APPROXIMATE),
    ("around 11am", 11, 0, TimePrecision.APPROXIMATE),
    ("right after lunch", 13, 0, TimePrecision.APPROXIMATE),
    ("around 2pm", 14, 0, TimePrecision.APPROXIMATE),
    ("mid-afternoon", 15, 30, TimePrecision.APPROXIMATE),
    ("around 5pm", 17, 0, TimePrecision.APPROXIMATE),
    ("in the evening", 19, 0, TimePrecision.APPROXIMATE),
]

DURATIONS = [45, 60, 90, 120, 150, 180, 240, 300, 360, 480]

SYMPTOM_PHRASES = {
    "aura": ["saw some zigzag lights first", "had visual aura before it hit"],
    "nausea": ["felt queasy", "mild nausea the whole time"],
    "vomiting": ["threw up once", "vomited and felt slightly better after"],
    "light_sensitivity": ["light bothered me", "had to dim everything"],
    "sound_sensitivity": ["noise was unbearable", "sound really bothered me"],
    "smell_sensitivity": ["smells were overpowering", "couldn't stand cooking smells"],
    "dizziness": ["felt dizzy standing up", "a bit dizzy and off-balance"],
}

MEDS = [
    ("Excedrin", ["2 tablets", "1 tablet"]),
    ("ibuprofen", ["200mg", "400mg", "2 tablets"]),
    ("acetaminophen", ["500mg", "1000mg"]),
    ("naproxen", ["220mg"]),
    ("sumatriptan", ["50mg"]),
]

MED_OUTCOMES = [
    (MedicationOutcome.RELIEF, ["which knocked it out", "and that did the trick", "which cleared it up"]),
    (MedicationOutcome.PARTIAL, ["which took the edge off", "which helped a bit", "and that dulled it"]),
    (MedicationOutcome.NONE, ["which didn't do much", "but it didn't help", "with no real relief"]),
    (MedicationOutcome.UNKNOWN, ["", ""]),
]

TRIGGERS = [
    ("didn't sleep well the night before", "poor sleep"),
    ("barely slept", "poor sleep"),
    ("skipped lunch", "skipped meal"),
    ("only had coffee for breakfast", "skipped meal"),
    ("super stressful day at work", "stress"),
    ("back-to-back meetings all day", "stress"),
    ("stared at screens for hours", "screen time"),
    ("forgot to drink water all day", "dehydration"),
    ("had red wine last night", "alcohol"),
    ("missed my morning coffee", "caffeine withdrawal"),
    ("the weather shifted hard", "weather change"),
]

NOTES = [
    "Had to lie down for a bit.",
    "Worked through it but barely.",
    "Took a nap and woke up better.",
    "Missed the gym because of it.",
    "Kept the room dark all afternoon.",
    "Still got my errands done somehow.",
]

OTHER_SYMPTOMS = ["brain fog", "neck stiffness", "fatigue", "pressure behind the eyes"]


def _local_tz():
    return datetime.now().astimezone().tzinfo


def _planned_entry(rng: random.Random, day: date) -> Entry:
    pain = rng.choices([3, 4, 5, 6, 7, 8, 9], weights=[1, 2, 3, 3, 2, 1, 1])[0]
    stated_pain = rng.random() < 0.8
    start_phrase, hour, minute, precision = rng.choice(STARTS)
    start_dt = datetime(day.year, day.month, day.day, hour, minute, tzinfo=_local_tz())
    duration = rng.choice(DURATIONS) if rng.random() < 0.75 else None
    n_symptoms = rng.choices([0, 1, 2, 3], weights=[3, 4, 2, 1])[0]
    symptoms = rng.sample(sorted(SYMPTOM_PHRASES), k=n_symptoms)

    sentences = []
    pain_word = rng.choice(PAIN_WORDS[pain])
    article = "an" if pain_word[0] in "aeiou" else "a"
    opener = f"Had {article} {pain_word} headache, {start_phrase}."
    if start_phrase == "woke up with it":
        opener = f"Woke up with {article} {pain_word} headache."
    sentences.append(opener)
    evidence: dict[str, str] = {"time_expression": start_phrase}
    if stated_pain:
        pain_line = f"Pain was about a {pain} out of 10."
        sentences.append(pain_line)
        evidence["pain_0_10"] = pain_line
    else:
        sentences.append("Didn't rate it, just rode it out.")
    for name in symptoms:
        line = rng.choice(SYMPTOM_PHRASES[name]).capitalize() + "."
        sentences.append(line)
        evidence[f"symptoms.{name}"] = line

    meds: list[AcuteMedication] = []
    if rng.random() < 0.7:
        med_name, doses = rng.choice(MEDS)
        outcome, outcome_phrases = rng.choice(MED_OUTCOMES)
        outcome_text = rng.choice(outcome_phrases)
        dose = rng.choice(doses) if rng.random() < 0.6 else None
        took = f"Took {med_name}" + (f" {dose}" if dose else "")
        med_line = f"{took} {outcome_text}.".replace(" .", ".").strip()
        sentences.append(med_line)
        evidence["acute_medications"] = med_line
        meds.append(
            AcuteMedication(
                name=med_name,
                dose=dose,
                outcome=outcome if outcome != MedicationOutcome.UNKNOWN else None,
            )
        )

    triggers: list[str] = []
    if rng.random() < 0.55:
        trigger_text, trigger_label = rng.choice(TRIGGERS)
        sentences.append(f"Probably because I {trigger_text}.")
        triggers.append(trigger_label)
        evidence["reported_triggers"] = trigger_text

    if duration is not None:
        if duration < 60:
            dur_line = f"Went away after about {duration} minutes."
        elif duration % 60 == 0:
            hours = duration // 60
            dur_line = f"Lasted about {hours} hour{'s' if hours > 1 else ''}."
        else:
            dur_line = f"Lasted about {duration // 60} and a half hours."
        sentences.append(dur_line)
        evidence["duration_minutes"] = dur_line

    notes = rng.choice(NOTES) if rng.random() < 0.5 else None
    if notes:
        sentences.append(notes)

    other = rng.sample(OTHER_SYMPTOMS, k=1) if rng.random() < 0.15 else []
    if other:
        other_line = f"Also had {other[0]}."
        sentences.append(other_line)
        evidence["symptoms.other"] = other_line

    transcript = " ".join(sentences)
    attack = AttackDetails(
        time_expression=start_phrase,
        attack_start=start_dt,
        attack_start_precision=precision,
        duration_minutes=duration,
        ongoing=False,
        severity_function=1 if pain <= 3 else (2 if pain <= 6 else 3),
        pain_0_10=pain if stated_pain else None,
        symptoms=SymptomSet(
            **{name: True for name in symptoms},
            other=other,
        ),
        acute_medications=meds,
        reported_triggers=triggers,
        notes=notes,
        evidence=evidence,
    )
    # Logged the same evening, after the headache ended.
    log_hour = min(23, max(hour + (duration or 120) // 60 + 1, 12))
    created_at = datetime(
        day.year, day.month, day.day, int(log_hour), rng.randrange(0, 60), tzinfo=_local_tz()
    )
    return Entry(
        entry_type=EntryType.ATTACK,
        created_at=created_at,
        entry_date=day,
        transcript=transcript,
        status=EntryStatus.UNCONFIRMED,
        attack=attack,
    )


def _existing_attack_count(conn: sqlite3.Connection, year: int, month: int) -> int:
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    return len(
        [e for e in db_mod.list_entries(conn, start=start, end=end) if e.entry_type == EntryType.ATTACK]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed demo headache entries for past months.")
    parser.add_argument("--months", nargs="+", default=["2026-09", "2026-10"])
    parser.add_argument("--min-days", type=int, default=10)
    parser.add_argument("--max-days", type=int, default=15)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--db", default=None, help="Defaults to the app Settings db path.")
    parser.add_argument("--confirm", dest="confirm", action="store_true", default=True)
    parser.add_argument("--unconfirmed", dest="confirm", action="store_false")
    parser.add_argument("--force", action="store_true", help="Seed even if the months already have attacks.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--clear", action="store_true", help="Remove previously seeded rows and exit.")
    args = parser.parse_args()

    db_path = args.db or load_settings().db_path
    conn = db_mod.connect(db_path)

    if args.clear:
        if not SIDECAR.exists():
            print("No demo seed sidecar found; nothing to clear.")
            return 0
        ids = json.loads(SIDECAR.read_text())["ids"]
        removed = sum(1 for i in ids if db_mod.delete_entry(conn, i))
        SIDECAR.unlink()
        print(f"Cleared {removed} demo entries.")
        return 0

    months = []
    for raw in args.months:
        year, month = (int(part) for part in raw.split("-"))
        months.append((year, month))

    if not args.force and not args.dry_run:
        for year, month in months:
            existing = _existing_attack_count(conn, year, month)
            if existing:
                print(
                    f"{year}-{month:02d} already has {existing} attack entries. "
                    "Refusing to mix demo data with real data (use --force to override)."
                )
                return 1

    rng = random.Random(args.seed)
    planned: list[Entry] = []
    for year, month in months:
        days_in_month = calendar.monthrange(year, month)[1]
        count = rng.randint(args.min_days, args.max_days)
        for day_num in sorted(rng.sample(range(1, days_in_month + 1), k=count)):
            planned.append(_planned_entry(rng, date(year, month, day_num)))

    for entry in planned:
        when = entry.entry_date.isoformat() if entry.entry_date else "?"
        print(f"  {when}  pain={entry.attack.pain_0_10 if entry.attack else None}  {entry.transcript[:90]}…")
    if args.dry_run:
        print(f"Dry run: {len(planned)} entries planned, nothing written.")
        return 0

    status = EntryStatus.CONFIRMED if args.confirm else EntryStatus.UNCONFIRMED
    for entry in planned:
        entry.status = status
        db_mod.save_entry(conn, entry)
    conn.commit()
    SIDECAR.parent.mkdir(parents=True, exist_ok=True)
    SIDECAR.write_text(json.dumps({"seeded_at": datetime.now().isoformat(), "ids": [e.id for e in planned]}))
    print(f"Seeded {len(planned)} demo entries ({'confirmed' if args.confirm else 'unconfirmed'}).")
    print("Remove them later with: python scripts/seed_demo.py --clear")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
