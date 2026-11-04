"""Quote verifier unit tests (deterministic, no network, no model)."""

from quietlog.verifier import (
    attack_to_raw,
    count_claimed,
    durations_in_quote,
    normalize,
    pain_numbers_in_quote,
    verify_extraction,
)


def P(value, quote):
    return {"value": value, "quote": quote}


def test_normalize_unifies_case_punct_and_spacing():
    assert normalize("  Since 2PM, severe!") == "since 2pm severe"
    assert normalize("can’t “function”") == "cant function"


def test_durations_in_quote():
    assert durations_in_quote("It faded after about two hours.") == {120}
    assert durations_in_quote("an hour ago it started") == {60}
    assert durations_in_quote("lasting half an hour") == {30}
    assert durations_in_quote("Started about 20 minutes ago") == {20}
    assert durations_in_quote("Moderate, 4 hours, took naproxen") == {240}
    assert durations_in_quote("two and a half hours of pain") == {150}
    assert durations_in_quote("Went away after an hour and a half.") == {90}
    assert durations_in_quote("Went away after an hour and half.") == {90}
    assert durations_in_quote("one and a half hours of pain") == {90}
    assert durations_in_quote("1 and a half hours") == {90}
    assert durations_in_quote("an hour of pain") == {60}
    assert durations_in_quote("Had it from 8am to 12pm") == {240}
    assert durations_in_quote("Started at 3, no duration stated") == set()
    assert durations_in_quote("7 out of 10 pain") == set()  # no unit: not a duration


def test_pain_numbers_in_quote():
    assert pain_numbers_in_quote("Pain is about a 7 out of 10.") == {7}
    assert pain_numbers_in_quote("Ten out of ten, worst ever.") == {10}
    assert pain_numbers_in_quote("I'd say 3 out of 10.") == {3}
    assert pain_numbers_in_quote("no numbers here") == set()


TRANSCRIPT = (
    "My migraine started around 2pm. It was moderate, slowed me right down. "
    "I had nausea and light sensitivity. No vomiting. I took sumatriptan 100mg "
    "at half past two and it helped. It faded after about two hours."
)


def _good_raw():
    return {
        "time_expression": P("around 2pm", "started around 2pm"),
        "duration_minutes": P(120, "It faded after about two hours"),
        "ongoing": P(False, "It faded after about two hours"),
        "severity_function": P(2, "It was moderate, slowed me right down"),
        "pain_0_10": None,
        "symptoms": {
            "nausea": P(True, "I had nausea and light sensitivity"),
            "light_sensitivity": P(True, "I had nausea and light sensitivity"),
            "vomiting": P(False, "No vomiting"),
            "aura": None, "dizziness": None, "sound_sensitivity": None,
            "smell_sensitivity": None, "other": [],
        },
        "acute_medications": [
            {"name": P("sumatriptan", "I took sumatriptan 100mg"),
             "dose": P("100mg", "I took sumatriptan 100mg"),
             "time_taken": P("at half past two", "at half past two and it helped"),
             "outcome": P("relief", "at half past two and it helped"),
             "second_dose": None},
        ],
        "reported_triggers": [],
        "menstrual_context": None,
        "notes": None,
    }


def test_control_extraction_passes():
    outcome = verify_extraction(TRANSCRIPT, _good_raw())
    assert outcome.failures == []
    attack = outcome.attack
    assert attack.time_expression == "around 2pm"
    assert attack.duration_minutes == 120
    assert attack.ongoing is False
    assert attack.severity_function == 2
    assert attack.symptoms.nausea is True
    assert attack.symptoms.vomiting is False
    assert attack.symptoms.aura is None
    assert attack.acute_medications[0].name == "sumatriptan"
    assert attack.acute_medications[0].dose == "100mg"
    assert attack.acute_medications[0].outcome.value == "relief"
    assert attack.evidence["symptoms.nausea"] == "I had nausea and light sensitivity"
    assert attack.unsupported_fields == []


def test_fabricated_quote_rejected():
    raw = _good_raw()
    raw["severity_function"] = P(3, "I was completely bedridden")
    outcome = verify_extraction(TRANSCRIPT, raw)
    assert outcome.attack.severity_function is None
    assert outcome.attack.unsupported_fields == ["severity_function"]
    assert outcome.failures[0].reason == "quote_not_found"


def test_inferred_med_dose_rejected():
    raw = _good_raw()
    raw["acute_medications"][0]["dose"] = P("200mg", "I took sumatriptan")
    outcome = verify_extraction(TRANSCRIPT, raw)
    assert outcome.attack.acute_medications[0].dose is None
    assert outcome.failures, "expected the inferred dose to fail verification"


def test_wrong_number_rejected():
    raw = _good_raw()
    raw["duration_minutes"] = P(999, "It faded after about two hours")
    outcome = verify_extraction(TRANSCRIPT, raw)
    assert outcome.attack.duration_minutes is None
    assert outcome.failures[0].reason == "number_mismatch"


def test_value_not_in_quote_rejected():
    raw = _good_raw()
    raw["time_expression"] = P("at 5pm", "started around 2pm")
    outcome = verify_extraction(TRANSCRIPT, raw)
    assert outcome.attack.time_expression is None
    assert outcome.failures[0].reason == "value_not_in_quote"


def test_missing_quote_rejected():
    raw = _good_raw()
    raw["severity_function"] = {"value": 2}
    outcome = verify_extraction(TRANSCRIPT, raw)
    assert outcome.attack.severity_function is None
    assert outcome.failures[0].reason == "missing_quote"


def test_symptom_true_with_denial_rejected():
    raw = _good_raw()
    raw["symptoms"]["vomiting"] = P(True, "No vomiting")
    outcome = verify_extraction(TRANSCRIPT, raw)
    assert outcome.attack.symptoms.vomiting is None
    assert outcome.failures[0].reason == "negation_mismatch"


def test_symptom_false_without_denial_rejected():
    raw = _good_raw()
    raw["symptoms"]["aura"] = P(False, "I had nausea and light sensitivity")
    outcome = verify_extraction(TRANSCRIPT, raw)
    assert outcome.attack.symptoms.aura is None
    assert outcome.failures[0].reason == "negation_mismatch"


def test_malformed_pair_becomes_unknown():
    raw = _good_raw()
    raw["severity_function"] = {"value": "very bad", "quote": "It was moderate"}
    outcome = verify_extraction(TRANSCRIPT, raw)
    assert outcome.attack.severity_function is None
    assert outcome.failures[0].reason in ("malformed", "quote_not_found")


def test_unnamed_medication_dropped():
    raw = _good_raw()
    raw["acute_medications"].append({"name": None, "dose": None})
    outcome = verify_extraction(TRANSCRIPT, raw)
    assert len(outcome.attack.acute_medications) == 1


def test_outcome_unknown_is_unstated():
    raw = _good_raw()
    raw["acute_medications"][0]["outcome"] = P("unknown", "I took sumatriptan 100mg")
    outcome = verify_extraction(TRANSCRIPT, raw)
    assert outcome.attack.acute_medications[0].outcome is None
    assert outcome.failures == []


def test_empty_transcript_rejects_everything():
    outcome = verify_extraction("   ", _good_raw())
    assert count_claimed(_good_raw()) > 0
    assert outcome.attack.time_expression is None
    assert outcome.attack.duration_minutes is None
    assert all(f.reason == "empty_transcript" for f in outcome.failures)


def test_non_dict_payload_is_malformed_but_safe():
    outcome = verify_extraction(TRANSCRIPT, ["not", "a", "dict"])
    assert outcome.attack.time_expression is None
    assert any(f.reason == "malformed" for f in outcome.failures)


def test_round_trip_and_claim_count():
    raw = _good_raw()
    outcome = verify_extraction(TRANSCRIPT, raw)
    assert count_claimed(raw) == 11
    rebuilt = attack_to_raw(outcome.attack)
    again = verify_extraction(TRANSCRIPT, rebuilt)
    assert again.failures == []
    assert count_claimed(rebuilt) == 11
