"""Model-assisted reads: diary Q&A and narrative summaries.

Both features share one honesty rule: the model only ever reads a
deterministic digest of stored data, and empty inputs get a deterministic
answer with no model call at all. Digests cap size so prompts stay small.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import llm_service
from .settings import Settings
from .summary import Summary
from .types import SYMPTOM_FIELDS, EntryStatus, EntryType

__all__ = [
    "AskResult",
    "NarrativeResult",
    "ask_diary",
    "draft_window_narrative",
    "entries_digest",
    "summary_digest",
]

MAX_DIGEST_ENTRIES = 40
MAX_DIGEST_TRANSCRIPT_CHARS = 400


@dataclass(frozen=True)
class AskResult:
    answer: str
    entries_consulted: int
    cached: bool = False


@dataclass(frozen=True)
class NarrativeResult:
    text: str
    cached: bool = False


def _attack_bits(entry) -> list[str]:
    attack = entry.attack
    if attack is None:
        return []
    bits: list[str] = []
    if attack.pain_0_10 is not None:
        bits.append(f"pain {attack.pain_0_10}/10")
    elif attack.severity_function == 1:
        bits.append("mild")
    elif attack.severity_function == 2:
        bits.append("moderate")
    elif attack.severity_function == 3:
        bits.append("severe")
    if attack.attack_start is not None:
        bits.append(f"started {attack.attack_start.isoformat(timespec='minutes')}")
    if attack.duration_minutes is not None:
        bits.append(f"lasted {attack.duration_minutes} min")
    symptoms = [name for name in SYMPTOM_FIELDS
                if getattr(attack.symptoms, name) is True]
    symptoms.extend(attack.symptoms.other)
    if symptoms:
        bits.append("symptoms: " + ", ".join(symptoms))
    for med in attack.acute_medications:
        outcome = med.outcome.value if med.outcome else "outcome unknown"
        bits.append(f"{med.name} ({outcome})")
    if attack.reported_triggers:
        bits.append("triggers: " + ", ".join(attack.reported_triggers))
    return bits


def entries_digest(
    entries,
    *,
    max_entries: int = MAX_DIGEST_ENTRIES,
    transcript_chars: int = MAX_DIGEST_TRANSCRIPT_CHARS,
) -> str:
    """One line per entry, oldest first, capped for prompt size."""
    dated = sorted(
        (e for e in entries if e.entry_date is not None),
        key=lambda e: (e.entry_date, e.created_at),
    )
    lines: list[str] = []
    for entry in dated[:max_entries]:
        if entry.entry_type == EntryType.NO_ATTACK:
            lines.append(f"{entry.entry_date.isoformat()} (no headache check-in).")
            continue
        review = "awaiting review" if entry.status != EntryStatus.CONFIRMED else "confirmed"
        bits = _attack_bits(entry)
        line = f"{entry.entry_date.isoformat()} (attack, {review})"
        if bits:
            line += ": " + "; ".join(bits)
        note = (entry.transcript or "").strip().replace("\n", " ")
        if note:
            line += f' Note: "{note[:transcript_chars]}"'
        lines.append(line + ".")
    return "\n".join(lines)


def summary_digest(summary: Summary) -> str:
    """The summary's numbers as plain lines for the narrative draft."""
    lines = [
        f"Window: {summary.window_start.isoformat()} to "
        f"{summary.window_end.isoformat()} ({len(summary.calendar)} days).",
        f"Attack days: {summary.attack_days}; headache-free check-in days: "
        f"{summary.no_attack_days}; unlogged days: {summary.not_logged_days}.",
        f"Attack entries: {summary.attack_entries} "
        f"({summary.unconfirmed_entries} awaiting review).",
    ]
    if summary.duration.median_minutes is not None:
        lines.append(
            f"Median duration: {summary.duration.median_minutes:.0f} minutes "
            f"({summary.duration.n} entries with durations)."
        )
    else:
        lines.append("No durations recorded.")
    symptoms = [f"{name} in {count} of {summary.symptoms_n}"
                for name, count in sorted(summary.symptoms.items()) if count > 0]
    symptoms.extend(f"{text} in {count} of {summary.symptoms_n}"
                    for text, count in sorted(summary.symptoms_other.items()))
    lines.append("Symptoms: " + ("; ".join(symptoms) if symptoms else "none mentioned") + ".")
    meds = []
    for med in summary.medications:
        helped = med.outcomes.get("relief", 0) + med.outcomes.get("partial", 0)
        meds.append(f"{med.display_name} taken for {med.attack_entries} attacks, "
                    f"relief reported {helped} times")
    lines.append("Medications: " + ("; ".join(meds) if meds else "none recorded") + ".")
    triggers = [f"{trigger} ({count})" for trigger, count in sorted(summary.triggers.items())]
    lines.append("Stated triggers: " + (", ".join(triggers) if triggers else "none") + ".")
    return "\n".join(lines)


def ask_diary(
    question: str,
    entries,
    settings: Settings,
    *,
    http_post=None,
) -> AskResult:
    """Answer a question strictly from the given entries."""
    usable = [e for e in entries if e.entry_date is not None]
    if not usable:
        return AskResult("There are no entries in this window yet.", 0)
    digest = entries_digest(usable)
    text, cached = llm_service.answer_question(
        question.strip(),
        digest,
        config=llm_service.load_config(cache_dir=settings.cache_dir),
        http_post=http_post,
    )
    return AskResult(text, min(len(usable), MAX_DIGEST_ENTRIES), cached)


def draft_window_narrative(
    summary: Summary,
    settings: Settings,
    *,
    http_post=None,
) -> NarrativeResult:
    """Draft a doctor-summary paragraph from a computed summary."""
    if summary.attack_entries == 0:
        return NarrativeResult(
            f"No attacks were logged from {summary.window_start.isoformat()} to "
            f"{summary.window_end.isoformat()}"
            f" ({summary.no_attack_days} headache-free check-ins)."
            " Discuss these patterns with your doctor."
        )
    text, cached = llm_service.draft_narrative(
        summary_digest(summary),
        config=llm_service.load_config(cache_dir=settings.cache_dir),
        http_post=http_post,
    )
    return NarrativeResult(text, cached)
