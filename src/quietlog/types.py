"""Leaf types for Quietlog: enums and constants with no third-party imports.

Kept dependency-free on purpose so pure logic (time resolution, summary
math, synthetic planning) never needs Pydantic just to name a status.
"""

from __future__ import annotations

from enum import Enum


class EntryType(str, Enum):
    ATTACK = "attack"
    NO_ATTACK = "no_attack"


class EntryStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    NEEDS_PROCESSING = "needs_processing"
    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"


class TimePrecision(str, Enum):
    EXACT = "exact"
    APPROXIMATE = "approximate"
    DATE_ONLY = "date_only"
    UNKNOWN = "unknown"


class MedicationOutcome(str, Enum):
    RELIEF = "relief"
    PARTIAL = "partial"
    NONE = "none"
    SIDE_EFFECTS = "side_effects"
    RECURRENCE = "recurrence"
    UNKNOWN = "unknown"


SYMPTOM_FIELDS = (
    "aura",
    "nausea",
    "vomiting",
    "light_sensitivity",
    "sound_sensitivity",
    "smell_sensitivity",
    "dizziness",
)
