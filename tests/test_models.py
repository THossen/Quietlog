"""Validation rules for the Quietlog data model."""

import pytest
from pydantic import ValidationError

from quietlog.models import (
    AcuteMedication,
    AttackDetails,
    Entry,
    EntryStatus,
    EntryType,
    SymptomSet,
)
from datetime import datetime, timezone


def _attack(**kwargs):
    base = {"entry_type": EntryType.ATTACK, "created_at": datetime(2026, 10, 24, 15, 30)}
    base.update(kwargs)
    return Entry(**base)


def test_attack_entry_defaults():
    e = _attack()
    assert e.entry_date.isoformat() == "2026-10-24"
    assert e.attack is not None
    assert e.status == EntryStatus.UNCONFIRMED


def test_checkin_is_confirmed_with_no_attack_details():
    e = Entry(entry_type=EntryType.NO_ATTACK, created_at=datetime(2026, 10, 24, 21, 0),
              status=EntryStatus.NEEDS_PROCESSING)
    assert e.status == EntryStatus.CONFIRMED
    assert e.attack is None


def test_severity_bounds():
    for bad in (0, 4, -1):
        with pytest.raises(ValidationError):
            AttackDetails(severity_function=bad)
    for ok in (1, 2, 3, None):
        assert AttackDetails(severity_function=ok).severity_function == ok


def test_pain_bounds():
    for bad in (-1, 11, 99):
        with pytest.raises(ValidationError):
            AttackDetails(pain_0_10=bad)
    assert AttackDetails(pain_0_10=0).pain_0_10 == 0
    assert AttackDetails(pain_0_10=10).pain_0_10 == 10


def test_duration_must_be_positive():
    for bad in (0, -30):
        with pytest.raises(ValidationError):
            AttackDetails(duration_minutes=bad)
    assert AttackDetails(duration_minutes=60).duration_minutes == 60


def test_medication_name_must_not_be_blank():
    with pytest.raises(ValidationError):
        AcuteMedication(name="   ")
    assert AcuteMedication(name="  ibuprofen ").name == "ibuprofen"


def test_symptom_tristate_and_mentioned():
    s = SymptomSet(nausea=True, vomiting=False)
    assert s.mentioned()["nausea"] is True
    assert s.mentioned()["vomiting"] is False
    assert s.mentioned()["aura"] is None


def test_lists_are_stripped():
    a = AttackDetails(reported_triggers=["  bad sleep ", ""], symptoms={"other": [" x ", ""]})
    assert a.reported_triggers == ["bad sleep"]
    assert a.symptoms.other == ["x"]


def test_entry_id_must_not_be_blank():
    with pytest.raises(ValidationError):
        _attack(id="  ")
