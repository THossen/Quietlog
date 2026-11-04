"""Quietlog data model (Pydantic v2).

Mirrors schema/fields.yaml. All clinical fields are nullable: "unknown"
(not stated) is always valid. Nothing here interprets or advises; these
models only record what the user said plus deterministic metadata.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Iterable, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from .types import (
    SYMPTOM_FIELDS,
    EntryStatus,
    EntryType,
    MedicationOutcome,
    TimePrecision,
)

__all__ = [
    "SYMPTOM_FIELDS",
    "AcuteMedication",
    "AttackDetails",
    "Entry",
    "EntryStatus",
    "EntryType",
    "MedicationOutcome",
    "Recording",
    "SymptomSet",
    "TimePrecision",
    "WeatherSnapshot",
    "mark_edited",
]


class AcuteMedication(BaseModel):
    """One acute medication taken for an attack, as stated by the user."""

    name: str
    dose: Optional[str] = None
    time_taken: Optional[str] = None
    outcome: Optional[MedicationOutcome] = None
    second_dose: Optional[bool] = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("medication name must not be blank")
        return v.strip()


class SymptomSet(BaseModel):
    """Tri-state symptoms: True = stated present, False = explicitly denied,
    None = not mentioned. Summary counts only True (never infer from silence)."""

    aura: Optional[bool] = None
    nausea: Optional[bool] = None
    vomiting: Optional[bool] = None
    light_sensitivity: Optional[bool] = None
    sound_sensitivity: Optional[bool] = None
    smell_sensitivity: Optional[bool] = None
    dizziness: Optional[bool] = None
    other: list[str] = Field(default_factory=list)

    @field_validator("other")
    @classmethod
    def strip_other(cls, v: list[str]) -> list[str]:
        return [s.strip() for s in v if s.strip()]

    def mentioned(self) -> dict[str, Optional[bool]]:
        """Fixed symptom flags as a plain dict (excludes free-text other)."""
        return {name: getattr(self, name) for name in SYMPTOM_FIELDS}


class AttackDetails(BaseModel):
    """Structured content of one attack entry, extracted from the user's words."""

    time_expression: Optional[str] = None
    attack_start: Optional[datetime] = None
    attack_start_precision: TimePrecision = TimePrecision.UNKNOWN
    # Stamped by the one-tap timer only; never extracted from words.
    attack_end: Optional[datetime] = None
    duration_minutes: Optional[int] = Field(default=None, ge=0)
    ongoing: Optional[bool] = None
    severity_function: Optional[int] = Field(default=None, ge=1, le=3)
    pain_0_10: Optional[int] = Field(default=None, ge=0, le=10)
    symptoms: SymptomSet = Field(default_factory=SymptomSet)
    acute_medications: list[AcuteMedication] = Field(default_factory=list)
    reported_triggers: list[str] = Field(default_factory=list)
    menstrual_context: Optional[str] = None
    notes: Optional[str] = None
    # field path -> verbatim quote from the transcript supporting it.
    evidence: dict[str, str] = Field(default_factory=dict)
    # field paths the verifier rejected ("not supported by what you said").
    unsupported_fields: list[str] = Field(default_factory=list)
    # field paths set by the user (review edits, timer stamps): re-extraction
    # must preserve these values instead of overwriting them with new guesses.
    edited_fields: list[str] = Field(default_factory=list)

    @field_validator("reported_triggers")
    @classmethod
    def strip_triggers(cls, v: list[str]) -> list[str]:
        return [s.strip() for s in v if s.strip()]

    @field_validator("duration_minutes")
    @classmethod
    def duration_must_be_positive(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v <= 0:
            raise ValueError("duration_minutes must be positive")
        return v


def mark_edited(attack: AttackDetails, paths: Iterable[str]) -> None:
    """Record user-set field paths (edits, timer stamps) so re-extraction
    preserves them. Paths are top-level field names or symptoms.<name>."""
    attack.edited_fields = sorted(set(attack.edited_fields) | set(paths))


class WeatherSnapshot(BaseModel):
    """Weather observed for an entry's moment. Coordinates are never stored;
    only this snapshot is kept. All fields nullable: missing weather is valid."""

    observed_at: Optional[datetime] = None
    temperature_c: Optional[float] = None
    humidity_pct: Optional[float] = None
    pressure_hpa: Optional[float] = None
    pressure_tendency: Optional[Literal["rising", "falling", "steady"]] = None
    condition: Optional[str] = None
    basis: Optional[Literal["log_time", "attack_time"]] = None
    source: Optional[str] = None


class Recording(BaseModel):
    """One voice recording attached to an entry, with explicit transcription
    state. The client generates the id (stable across retries); the server
    persists the transcript here the moment it arrives, and folds it into
    entry.transcript exactly once (`appended`)."""

    id: str
    entry_id: str
    audio_path: str
    status: str = "pending"  # pending|transcribing|transcribed|failed
    failure_kind: Optional[str] = None  # transient|permanent
    attempts: int = 0
    next_attempt_at: Optional[datetime] = None
    error: Optional[str] = None
    transcript: Optional[str] = None
    appended: bool = False
    created_at: datetime
    updated_at: Optional[datetime] = None


class Entry(BaseModel):
    """One saved record: either an attack entry or a no-attack check-in."""

    id: str = Field(default_factory=lambda: uuid4().hex)
    entry_type: EntryType
    created_at: datetime
    entry_date: Optional[date] = None
    audio_path: Optional[str] = None
    transcript: Optional[str] = None
    status: EntryStatus = EntryStatus.UNCONFIRMED
    attack: Optional[AttackDetails] = None
    processing_error: Optional[str] = None
    weather: Optional[WeatherSnapshot] = None
    # Voice recordings with transcription state. Populated by routes from the
    # recordings table; never stored on the entries row itself.
    recordings: list[Recording] = Field(default_factory=list)

    @model_validator(mode="after")
    def _defaults(self) -> "Entry":
        if self.entry_date is None:
            # The entry belongs to the user's calendar day. Phones send UTC,
            # so convert to local time first: a 9pm US entry is not tomorrow.
            created = self.created_at
            if created.tzinfo is not None:
                created = created.astimezone()
            self.entry_date = created.date()
        if self.entry_type == EntryType.NO_ATTACK:
            # A check-in is one tap with nothing to extract: confirmed at save.
            self.status = EntryStatus.CONFIRMED
            self.attack = None
        elif self.entry_type == EntryType.ATTACK and self.attack is None:
            self.attack = AttackDetails()
        return self

    @field_validator("id")
    @classmethod
    def id_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("id must not be blank")
        return v

    @property
    def is_attack(self) -> bool:
        return self.entry_type == EntryType.ATTACK
