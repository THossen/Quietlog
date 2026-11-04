"""Deterministic quote verifier for structured extraction (M2).

The model returns every field as a ``{"value": ..., "quote": ...}`` pair where
``quote`` must be copied verbatim from the transcript. This module checks, with
plain code and no model involved:

- after normalizing case, whitespace, and punctuation, each quote is a
  substring of the transcript;
- ``stated_as`` string fields (names, doses, times, triggers, ...) appear
  verbatim inside their own quote;
- stated numbers are consistent with the quote: ``duration_minutes`` must
  match a number+unit in the quote (digits or English words, e.g. "two hours"
  -> 120), and ``pain_0_10`` must match a 0-10 number in the quote;
- symptom True/False values must be consistent with negation wording in the
  quote ("no nausea" cannot support ``nausea: true``).

Any field that fails is reset to unknown and its path is recorded in
``AttackDetails.unsupported_fields`` ("not supported by what you said").
Fail-closed throughout: malformed model output becomes unknown, never an
exception and never a guessed value.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass, field
from typing import Any, Optional

from .models import AcuteMedication, AttackDetails, SymptomSet
from .time_resolve import resolve_duration_range
from .types import SYMPTOM_FIELDS, MedicationOutcome

__all__ = [
    "FieldFailure",
    "VerificationOutcome",
    "attack_to_raw",
    "count_claimed",
    "normalize",
    "verify_extraction",
]

_FANCY_QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})
_PUNCTUATION = str.maketrans("", "", string.punctuation)


def normalize(text: str) -> str:
    """Lowercase, unify quotes, drop punctuation, collapse whitespace."""
    text = text.translate(_FANCY_QUOTES).lower()
    text = text.translate(_PUNCTUATION)
    return " ".join(text.split())


_ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60}
_NUMBER_WORD = (
    r"(?:\d+|"
    + "|".join(sorted(set(_ONES) | set(_TENS), key=len, reverse=True))
    + r"(?:[ -](?:one|two|three|four|five|six|seven|eight|nine))?)"
)
_HOUR_RE = re.compile(_NUMBER_WORD + r"\s*(?:hours?|hrs?)\b")
_MINUTE_RE = re.compile(_NUMBER_WORD + r"\s*(?:minutes?|mins?)\b")
_HALF_HOUR_RE = re.compile(r"\bhalf\s+(?:an?\s+)?hour\b")
_AN_HOUR_RE = re.compile(r"\ban?\s+hour\b")
_AND_HALF_RE = re.compile(
    r"(?:" + _NUMBER_WORD + r"|an?)(?:\s+hours?)?\s+and\s+(?:a\s+)?half(?:\s+hours?)?\b"
)
_AND_HALF_AHEAD_RE = re.compile(r"\s+and\s+(?:a\s+)?half\b")
_SCALE_RE = re.compile(r"\bout\s+of\s+(?:10|ten)\b|/10\b")
_PAIN_NUM_RE = re.compile(r"\b(\d+|zero|one|two|three|four|five|six|seven|eight|nine|ten)\b")

_NEGATIONS = (
    "no", "not", "never", "without", "denies", "denied", "deny",
    "none", "neither", "nor",
)
_NEGATION_RE = re.compile(r"\b(" + "|".join(_NEGATIONS) + r")\b")
_SYMPTOM_WORDS = {
    "aura": ("aura",),
    "nausea": ("nausea", "nauseous"),
    "vomiting": ("vomit", "vomiting", "vomited", "threw up", "throw up"),
    "light_sensitivity": ("light sensitivity", "lights hurt", "light hurts", "sensitive to light"),
    "sound_sensitivity": ("sound sensitivity", "sounds hurt", "sound hurts", "sensitive to sound"),
    "smell_sensitivity": ("smell sensitivity", "smells", "smell", "sensitive to smell"),
    "dizziness": ("dizzy", "dizziness"),
}


def _word_to_number(raw: str) -> Optional[float]:
    q = raw.strip().lower()
    if q in ("a", "an"):
        return 1
    if q.isdigit():
        return int(q)
    total = 0
    for part in re.split(r"[ -]", q):
        if part in _ONES:
            total += _ONES[part]
        elif part in _TENS:
            total += _TENS[part]
        else:
            return None
    return total if total > 0 or q == "zero" else None


def durations_in_quote(quote: str) -> set[int]:
    """Minute values stated in a quote via number+unit phrases."""
    text = normalize(quote)
    found: set[int] = set()
    if _HALF_HOUR_RE.search(text):
        found.add(30)
    for m in _AN_HOUR_RE.finditer(text):
        # "an hour" but not "half an hour" (already counted as 30) and not
        # "an hour and (a) half" (counted as 90 below).
        start = max(0, m.start() - 5)
        if "half" not in text[start:m.start()] and not _AND_HALF_AHEAD_RE.match(text, m.end()):
            found.add(60)
    for m in _HOUR_RE.finditer(text):
        # The regex consumed "<number> <unit>"; strip the unit to parse it.
        num = _word_to_number(re.sub(r"\s*(hours?|hrs?)$", "", m.group(0)))
        if num is not None and float(num).is_integer():
            found.add(int(num) * 60)
    for m in _MINUTE_RE.finditer(text):
        num = _word_to_number(re.sub(r"\s*(minutes?|mins?)$", "", m.group(0)))
        if num is not None and float(num).is_integer():
            found.add(int(num))
    for m in _AND_HALF_RE.finditer(text):
        num = _word_to_number(
            re.sub(r"(?:\s+hours?)?\s+and\s+(?:a\s+)?half(?:\s+hours?)?$", "", m.group(0))
        )
        if num is not None and float(num).is_integer():
            found.add(int(num) * 60 + 30)
    ranged = resolve_duration_range(quote)
    if ranged is not None:
        found.add(ranged)
    return found


def pain_numbers_in_quote(quote: str) -> set[int]:
    """0-10 numbers stated in a quote, excluding the '/10' scale marker."""
    text = _SCALE_RE.sub(" ", normalize(quote))
    found: set[int] = set()
    for m in _PAIN_NUM_RE.finditer(text):
        num = _word_to_number(m.group(1))
        if num is not None and 0 <= num <= 10 and float(num).is_integer():
            found.add(int(num))
    return found


def _mentions_symptom(norm_quote: str, field: str) -> bool:
    return any(phrase in norm_quote for phrase in _SYMPTOM_WORDS[field])


@dataclass(frozen=True)
class FieldFailure:
    path: str
    reason: str  # quote_not_found | missing_quote | value_not_in_quote |
    # number_mismatch | negation_mismatch | malformed | empty_transcript
    detail: str = ""


@dataclass
class VerificationOutcome:
    attack: AttackDetails
    failures: list[FieldFailure] = field(default_factory=list)

    @property
    def claimed(self) -> int:
        return count_claimed_attack(self.attack) + len(self.failures)

    @property
    def supported(self) -> int:
        return count_claimed_attack(self.attack)


def count_claimed_attack(attack: AttackDetails) -> int:
    """Leaf fields carrying a non-null value in a verified attack object."""
    n = 0
    for name in (
        "time_expression", "duration_minutes", "ongoing", "severity_function",
        "pain_0_10", "menstrual_context", "notes",
    ):
        if getattr(attack, name) is not None:
            n += 1
    for name in SYMPTOM_FIELDS:
        if getattr(attack.symptoms, name) is not None:
            n += 1
    n += len(attack.symptoms.other)
    for med in attack.acute_medications:
        n += 1  # name always present
        for name in ("dose", "time_taken", "outcome", "second_dose"):
            if getattr(med, name) is not None:
                n += 1
    n += len(attack.reported_triggers)
    return n


def count_claimed(raw: Any) -> int:
    """Leaf fields claimed (non-null) in a raw model payload."""
    if not isinstance(raw, dict):
        return 0
    n = 0
    for key, pair in raw.items():
        if key == "symptoms" and isinstance(pair, dict):
            for name in SYMPTOM_FIELDS:
                if _pair_value(pair.get(name)) is not None:
                    n += 1
            other = pair.get("other")
            if isinstance(other, list):
                n += sum(1 for item in other if _pair_value(item) is not None)
        elif key == "acute_medications" and isinstance(pair, list):
            for med in pair:
                if not isinstance(med, dict):
                    continue
                if _pair_value(med.get("name")) is not None:
                    n += 1
                    for name in ("dose", "time_taken", "outcome", "second_dose"):
                        if _pair_value(med.get(name)) is not None:
                            n += 1
        elif key == "reported_triggers" and isinstance(pair, list):
            n += sum(1 for item in pair if _pair_value(item) is not None)
        elif _pair_value(pair) is not None:
            n += 1
    return n


def _pair_value(pair: Any) -> Any:
    if isinstance(pair, dict) and "value" in pair:
        return pair["value"]
    return None


def attack_to_raw(attack: AttackDetails) -> dict[str, Any]:
    """Rebuild a value+quote payload from verified output (for re-checks)."""
    ev = attack.evidence

    def pair(path: str, value: Any) -> Any:
        if value is None:
            return None
        return {"value": value, "quote": ev.get(path, "")}

    raw: dict[str, Any] = {
        "time_expression": pair("time_expression", attack.time_expression),
        "duration_minutes": pair("duration_minutes", attack.duration_minutes),
        "ongoing": pair("ongoing", attack.ongoing),
        "severity_function": pair("severity_function", attack.severity_function),
        "pain_0_10": pair("pain_0_10", attack.pain_0_10),
        "menstrual_context": pair("menstrual_context", attack.menstrual_context),
        "notes": pair("notes", attack.notes),
        "symptoms": {
            name: pair(f"symptoms.{name}", getattr(attack.symptoms, name))
            for name in SYMPTOM_FIELDS
        },
        "acute_medications": [],
        "reported_triggers": [],
    }
    raw["symptoms"]["other"] = [
        {"value": text, "quote": ev.get(f"symptoms.other[{i}]", "")}
        for i, text in enumerate(attack.symptoms.other)
    ]
    for i, med in enumerate(attack.acute_medications):
        raw["acute_medications"].append({
            "name": {"value": med.name, "quote": ev.get(f"acute_medications[{i}].name", "")},
            "dose": pair(f"acute_medications[{i}].dose", med.dose),
            "time_taken": pair(f"acute_medications[{i}].time_taken", med.time_taken),
            "outcome": pair(
                f"acute_medications[{i}].outcome",
                med.outcome.value if med.outcome else None,
            ),
            "second_dose": pair(f"acute_medications[{i}].second_dose", med.second_dose),
        })
    raw["reported_triggers"] = [
        {"value": text, "quote": ev.get(f"reported_triggers[{i}]", "")}
        for i, text in enumerate(attack.reported_triggers)
    ]
    return raw


class _Verifier:
    def __init__(self, transcript: str):
        self.transcript = transcript
        self.norm_transcript = normalize(transcript)
        self.failures: list[FieldFailure] = []
        self.evidence: dict[str, str] = {}

    def fail(self, path: str, reason: str, detail: str = "") -> None:
        self.failures.append(FieldFailure(path, reason, detail))

    def check_quote(self, path: str, quote: Any) -> Optional[str]:
        """Validate the quote itself. Returns normalized quote or None."""
        if not self.norm_transcript:
            self.fail(path, "empty_transcript")
            return None
        if not isinstance(quote, str) or not quote.strip():
            self.fail(path, "missing_quote")
            return None
        norm = normalize(quote)
        if not norm or norm not in self.norm_transcript:
            self.fail(path, "quote_not_found", quote[:120])
            return None
        return norm

    def take_pair(
        self, path: str, pair: Any, *, stated_as: bool = False
    ) -> tuple[Any, Optional[str]]:
        """Unpack and quote-check one pair. Returns (value, norm_quote)."""
        if pair is None:
            return None, None
        if not isinstance(pair, dict) or "value" not in pair:
            self.fail(path, "malformed", f"expected {{value, quote}}, got {pair!r}"[:120])
            return None, None
        value = pair["value"]
        if value is None:
            return None, None
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None, None
        norm_quote = self.check_quote(path, pair.get("quote"))
        if norm_quote is None:
            return None, None
        if stated_as and isinstance(value, str):
            if normalize(value) not in norm_quote:
                self.fail(path, "value_not_in_quote", value[:120])
                return None, None
        self.evidence[path] = pair["quote"]
        return value, norm_quote

    def take_int(
        self, path: str, pair: Any, *, lo: int, hi: int
    ) -> tuple[Optional[int], Optional[str]]:
        value, norm_quote = self.take_pair(path, pair)
        if value is None or norm_quote is None:
            return None, None
        if isinstance(value, bool):
            self.fail(path, "malformed", f"expected int, got {value!r}")
            del self.evidence[path]
            return None, None
        if isinstance(value, str):
            if re.fullmatch(r"\d+", value.strip()):
                value = int(value.strip())
            else:
                self.fail(path, "malformed", f"expected int, got {value!r}"[:120])
                del self.evidence[path]
                return None, None
        if not isinstance(value, int) or isinstance(value, bool):
            self.fail(path, "malformed", f"expected int, got {value!r}"[:120])
            del self.evidence[path]
            return None, None
        if not (lo <= value <= hi):
            self.fail(path, "malformed", f"{value} outside {lo}..{hi}")
            del self.evidence[path]
            return None, None
        return value, norm_quote

    def take_bool(self, path: str, pair: Any) -> tuple[Optional[bool], Optional[str]]:
        value, norm_quote = self.take_pair(path, pair)
        if value is None or norm_quote is None:
            return None, None
        if not isinstance(value, bool):
            self.fail(path, "malformed", f"expected bool, got {value!r}"[:120])
            del self.evidence[path]
            return None, None
        return value, norm_quote


def verify_extraction(transcript: str, raw: Any) -> VerificationOutcome:
    """Verify a raw model payload; failing fields reset to unknown."""
    checker = _Verifier(transcript or "")
    data = raw if isinstance(raw, dict) else {}
    if not isinstance(raw, dict):
        checker.fail("(root)", "malformed", f"expected object, got {type(raw).__name__}")

    time_expression, _ = checker.take_pair(
        "time_expression", data.get("time_expression"), stated_as=True
    )
    duration, duration_quote = checker.take_int(
        "duration_minutes", data.get("duration_minutes"), lo=1, hi=60 * 24 * 14
    )
    if duration is not None and duration_quote is not None:
        if duration not in durations_in_quote(data["duration_minutes"]["quote"]):
            checker.fail("duration_minutes", "number_mismatch", f"{duration} not in quote")
            del checker.evidence["duration_minutes"]
            duration = None
    ongoing, _ = checker.take_bool("ongoing", data.get("ongoing"))
    severity, _ = checker.take_int(
        "severity_function", data.get("severity_function"), lo=1, hi=3
    )
    pain, pain_quote = checker.take_int("pain_0_10", data.get("pain_0_10"), lo=0, hi=10)
    if pain is not None and pain_quote is not None:
        if pain not in pain_numbers_in_quote(data["pain_0_10"]["quote"]):
            checker.fail("pain_0_10", "number_mismatch", f"{pain} not in quote")
            del checker.evidence["pain_0_10"]
            pain = None
    menstrual, _ = checker.take_pair(
        "menstrual_context", data.get("menstrual_context"), stated_as=True
    )
    notes, _ = checker.take_pair("notes", data.get("notes"))

    symptoms = _verify_symptoms(checker, data.get("symptoms"))
    meds = _verify_meds(checker, data.get("acute_medications"))
    triggers = _verify_string_list(checker, data.get("reported_triggers"), "reported_triggers")

    attack = AttackDetails(
        time_expression=time_expression if isinstance(time_expression, str) else None,
        duration_minutes=duration,
        ongoing=ongoing,
        severity_function=severity,
        pain_0_10=pain,
        symptoms=symptoms,
        acute_medications=meds,
        reported_triggers=[t for t in triggers if isinstance(t, str)],
        menstrual_context=menstrual if isinstance(menstrual, str) else None,
        notes=notes if isinstance(notes, str) else None,
        evidence=checker.evidence,
        unsupported_fields=sorted({f.path for f in checker.failures}),
    )
    return VerificationOutcome(attack=attack, failures=checker.failures)


def _verify_symptoms(checker: _Verifier, node: Any) -> SymptomSet:
    flags: dict[str, Optional[bool]] = {}
    node = node if isinstance(node, dict) else {}
    for name in SYMPTOM_FIELDS:
        path = f"symptoms.{name}"
        value, norm_quote = checker.take_bool(path, node.get(name))
        if value is not None and norm_quote is not None:
            negated = _NEGATION_RE.search(norm_quote) is not None
            mentioned = _mentions_symptom(norm_quote, name)
            if value is True and negated and mentioned:
                checker.fail(path, "negation_mismatch", "quote denies this symptom")
                del checker.evidence[path]
                value = None
            elif value is False and not negated:
                checker.fail(path, "negation_mismatch", "quote states no denial")
                del checker.evidence[path]
                value = None
        flags[name] = value
    other = _verify_string_list(checker, node.get("other"), "symptoms.other")
    return SymptomSet(**flags, other=[t for t in other if isinstance(t, str)])


def _verify_string_list(checker: _Verifier, node: Any, base: str) -> list[str]:
    if node is None:
        return []
    if not isinstance(node, list):
        checker.fail(base, "malformed", f"expected list, got {type(node).__name__}")
        return []
    out: list[str] = []
    for i, item in enumerate(node):
        value, _ = checker.take_pair(f"{base}[{i}]", item, stated_as=True)
        if isinstance(value, str):
            out.append(value)
    return out


def _verify_meds(checker: _Verifier, node: Any) -> list[AcuteMedication]:
    if node is None:
        return []
    if not isinstance(node, list):
        checker.fail("acute_medications", "malformed", f"expected list, got {type(node).__name__}")
        return []
    meds: list[AcuteMedication] = []
    for i, item in enumerate(node):
        base = f"acute_medications[{i}]"
        if not isinstance(item, dict):
            checker.fail(base, "malformed", f"expected object, got {item!r}"[:120])
            continue
        name, _ = checker.take_pair(f"{base}.name", item.get("name"), stated_as=True)
        if not isinstance(name, str):
            continue  # unnamed medication items are dropped entirely
        dose, _ = checker.take_pair(f"{base}.dose", item.get("dose"), stated_as=True)
        time_taken, _ = checker.take_pair(
            f"{base}.time_taken", item.get("time_taken"), stated_as=True
        )
        outcome = _verify_outcome(checker, item.get("outcome"), f"{base}.outcome")
        second_dose, _ = checker.take_bool(f"{base}.second_dose", item.get("second_dose"))
        try:
            meds.append(AcuteMedication(
                name=name,
                dose=dose if isinstance(dose, str) else None,
                time_taken=time_taken if isinstance(time_taken, str) else None,
                outcome=outcome,
                second_dose=second_dose,
            ))
        except ValueError as exc:
            checker.fail(base, "malformed", str(exc)[:120])
    return meds


def _verify_outcome(checker: _Verifier, pair: Any, path: str) -> Optional[MedicationOutcome]:
    value, _ = checker.take_pair(path, pair)
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() == "unknown":
        return None  # "unknown" stated explicitly is the same as unstated
    try:
        return MedicationOutcome(str(value).strip().lower())
    except ValueError:
        checker.fail(path, "malformed", f"bad outcome {value!r}"[:120])
        if path in checker.evidence:
            del checker.evidence[path]
        return None
