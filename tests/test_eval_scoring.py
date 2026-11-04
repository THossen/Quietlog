"""Evaluation scoring unit tests (pure functions, no model, no key)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import evaluate as ev

from quietlog.models import AttackDetails


def _labels(**overrides):
    base = {
        "time_expression": "since 2pm",
        "duration_minutes": 120,
        "ongoing": False,
        "severity_function": 2,
        "pain_0_10": None,
        "symptoms": {"aura": None, "nausea": True, "vomiting": False,
                     "light_sensitivity": None, "sound_sensitivity": None,
                     "smell_sensitivity": None, "dizziness": None, "other": []},
        "acute_medications": [
            {"name": "ibuprofen", "dose": "200mg", "time_taken": "at 3",
             "outcome": "relief", "second_dose": None},
        ],
        "reported_triggers": ["bad sleep"],
        "menstrual_context": None,
        "notes": None,
    }
    base.update(overrides)
    return base


def test_exact_match_scores_all_true():
    got = ev.score_labels(_labels(), _labels())
    assert all(got.values()) and len(got) == len(ev.FIELD_ORDER)


def test_mismatches_flagged_per_field():
    got = ev.score_labels(_labels(severity_function=3, ongoing=True), _labels())
    assert got["severity_function"] is False
    assert got["ongoing"] is False
    assert got["duration_minutes"] is True
    assert got["symptoms.nausea"] is True


def test_tri_state_none_vs_false_differs():
    got = ev.score_labels(_labels(), _labels(symptoms={**_labels()["symptoms"],
                                                       "vomiting": None}))
    assert got["symptoms.vomiting"] is False


def test_string_compare_ignores_case_and_punct():
    got = ev.score_labels(_labels(time_expression="Since  2PM!"), _labels())
    assert got["time_expression"] is True


def test_med_lists_compared_as_multisets_with_unknown_outcome():
    a = _labels()["acute_medications"] + [
        {"name": "Advil", "dose": None, "time_taken": None,
         "outcome": "unknown", "second_dose": None},
    ]
    b = [
        {"name": "advil", "dose": None, "time_taken": None,
         "outcome": None, "second_dose": None},
        {"name": "Ibuprofen", "dose": "200mg", "time_taken": "at 3",
         "outcome": "relief", "second_dose": None},
    ]
    got = ev.score_labels(_labels(acute_medications=a), _labels(acute_medications=b))
    assert got["acute_medications"] is True


def test_notes_scores_presence_only():
    got = ev.score_labels(_labels(notes="totally different words"), _labels(notes="x"))
    assert got["notes(presence)"] is True
    got = ev.score_labels(_labels(notes="x"), _labels(notes=None))
    assert got["notes(presence)"] is False


def test_raw_to_labels_is_lenient():
    raw = {
        "time_expression": {"value": " at 3pm ", "quote": "q"},
        "severity_function": {"value": "nonsense", "quote": "q"},
        "symptoms": {"nausea": {"value": "yes", "quote": "q"}},
        "acute_medications": [{"name": {"value": "  ", "quote": "q"}},
                              {"name": {"value": "aspirin", "quote": "q"},
                               "outcome": {"value": "unknown", "quote": "q"}}],
        "reported_triggers": "not-a-list",
    }
    labels = ev.raw_to_labels(raw)
    assert labels["time_expression"] == "at 3pm"
    assert labels["severity_function"] == "nonsense"  # unverified: as-is
    assert labels["symptoms"]["nausea"] is None  # non-bool tri-state: unparseable
    assert labels["acute_medications"] == [
        {"name": "aspirin", "dose": None, "time_taken": None,
         "outcome": None, "second_dose": None}
    ]
    assert labels["reported_triggers"] == []


def test_attack_to_labels_round_trip():
    attack = AttackDetails.model_validate(_labels())
    assert ev.score_labels(ev.attack_to_labels(attack), _labels())["severity_function"]
    assert ev.attack_to_labels(attack)["acute_medications"][0]["name"] == "ibuprofen"


def test_deletion_claim_matcher():
    import live_agent_eval as lae

    assert lae.deletion_claim("I deleted the entry") is True
    assert lae.deletion_claim("Deleted it for you") is True
    assert lae.deletion_claim("I removed the entry") is True
    assert lae.deletion_claim("Got it — nothing deleted. Your entry is kept.") is False
    assert lae.deletion_claim("Nothing was deleted; it's still there.") is False
    assert lae.deletion_claim("No problem — I kept the entry.") is False
