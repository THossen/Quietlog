"""Synthetic fixtures: determinism, planted-aggregate consistency, label quality.

The consistency check below recounts ground truth from raw diary.json with
plain code (it never imports summary.py), mirroring what scripts/evaluate.py
will assert against summary.py in M2.
"""

import json
import statistics
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from quietlog.models import AttackDetails, Entry

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import make_synthetic_diary as gen


REQUIRED_TAGS = {
    "vague", "relative_time", "self_correction", "brand_generic", "second_dose",
    "negation", "other_person", "missing_details", "typo", "injection",
}


def _read(out, name):
    return (out / name).read_bytes()


def test_generator_is_deterministic(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    gen.write_outputs(first, seed=20260704, days=90, end=date(2026, 10, 31))
    gen.write_outputs(second, seed=20260704, days=90, end=date(2026, 10, 31))
    for name in ("diary.json", "aggregates.json", "transcripts.jsonl"):
        assert _read(first, name) == _read(second, name)
    other = tmp_path / "c"
    gen.write_outputs(other, seed=1, days=90, end=date(2026, 10, 31))
    assert _read(first, "diary.json") != _read(other, "diary.json")


def _recount(diary):
    """Independent recount from raw JSON (no summary.py import)."""
    entries = diary["entries"]
    attacks = [e for e in entries if e["entry_type"] == "attack"]
    checkins = [e for e in entries if e["entry_type"] == "no_attack"]
    attack_days = {e["entry_date"] for e in attacks}
    checkin_days = {e["entry_date"] for e in checkins}
    window = [diary["window_start"], diary["window_end"]]
    start = date.fromisoformat(window[0])
    ndays = (date.fromisoformat(window[1]) - start).days + 1
    all_days = {(start + timedelta(days=i)).isoformat() for i in range(ndays)}
    durations = sorted(a["attack"]["duration_minutes"] for a in attacks
                       if a["attack"]["duration_minutes"] is not None)
    severities = [a["attack"]["severity_function"] for a in attacks
                  if a["attack"]["severity_function"] in (1, 2, 3)]
    symptoms = Counter()
    for a in attacks:
        for name, val in a["attack"]["symptoms"].items():
            if name != "other" and val is True:
                symptoms[name] += 1
    meds: dict[str, dict] = {}
    for a in attacks:
        seen = set()
        for med in a["attack"]["acute_medications"]:
            key = " ".join(med["name"].strip().lower().split())
            g = meds.setdefault(key, {"display": med["name"].strip(), "days": set(),
                                      "attacks": 0, "outcomes": Counter()})
            g["days"].add(a["entry_date"])
            if key not in seen:
                g["attacks"] += 1
                seen.add(key)
            g["outcomes"][med.get("outcome") or "unknown"] += 1
    triggers = Counter()
    for a in attacks:
        for t in set(a["attack"]["reported_triggers"]):
            triggers[t] += 1
    return {
        "attack_days": len(attack_days),
        "no_attack_days": len(checkin_days),
        "not_logged_days": len(all_days - attack_days - checkin_days),
        "attack_entries": len(attacks),
        "unconfirmed": sum(1 for a in attacks if a["status"] != "confirmed"),
        "durations": durations,
        "severity_counts": Counter(severities),
        "symptoms": symptoms,
        "meds": meds,
        "triggers": triggers,
    }


def test_planted_aggregates_match_diary(tmp_path):
    gen.write_outputs(tmp_path, seed=20260704, days=90, end=date(2026, 10, 31))
    diary = json.loads((tmp_path / "diary.json").read_text(encoding="utf-8"))
    planted = json.loads((tmp_path / "aggregates.json").read_text(encoding="utf-8"))
    got = _recount(diary)
    assert planted["attack_days"] == got["attack_days"] == 14
    assert planted["no_attack_days"] == got["no_attack_days"] == 62
    assert planted["not_logged_days"] == got["not_logged_days"] == 14
    assert planted["attack_entries"] == got["attack_entries"] == 16
    assert planted["unconfirmed_entries"] == got["unconfirmed"]
    assert planted["duration"]["n"] == len(got["durations"])
    if got["durations"]:
        assert planted["duration"]["median_minutes"] == float(statistics.median(got["durations"]))
        assert planted["duration"]["min_minutes"] == got["durations"][0]
        assert planted["duration"]["max_minutes"] == got["durations"][-1]
    assert Counter({int(k): v for k, v in planted["severity_counts"].items()}) == got["severity_counts"]
    assert planted["severity_n"] == sum(got["severity_counts"].values())
    for name, count in planted["symptoms"].items():
        assert count == got["symptoms"].get(name, 0), name
    assert planted["symptoms_n"] == got["attack_entries"]
    assert len(planted["medications"]) == len(got["meds"])
    for m in planted["medications"]:
        key = " ".join(m["display_name"].strip().lower().split())
        g = got["meds"][key]
        assert m["attack_entries"] == g["attacks"], key
        assert m["distinct_days"] == len(g["days"]), key
        assert m["outcomes"] == dict(g["outcomes"]), key
    assert planted["triggers"] == dict(got["triggers"])
    assert planted["triggers_n"] == got["attack_entries"]


def test_diary_entries_are_valid_and_sane(tmp_path):
    gen.write_outputs(tmp_path, seed=20260704, days=90, end=date(2026, 10, 31))
    diary = json.loads((tmp_path / "diary.json").read_text(encoding="utf-8"))
    for raw in diary["entries"]:
        entry = Entry.model_validate(raw)  # must validate
        if entry.is_attack:
            assert entry.transcript, entry.id
            start = entry.attack.attack_start
            # date_only carries a 12:00 placeholder, not a real time; only
            # resolved (exact/approximate) times must precede the save.
            if entry.attack.attack_start_precision.value in ("exact", "approximate"):
                assert start is not None and start <= entry.created_at, entry.id
        else:
            assert entry.status.value == "confirmed"


def _load_transcripts(tmp_path):
    gen.write_outputs(tmp_path, seed=20260704, days=90, end=date(2026, 10, 31))
    lines = (tmp_path / "transcripts.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def test_transcript_coverage_and_schema(tmp_path):
    items = _load_transcripts(tmp_path)
    assert len(items) >= 60
    ids = [t["id"] for t in items]
    assert len(set(ids)) == len(ids)
    tags = {tag for t in items for tag in t["tags"]}
    assert REQUIRED_TAGS <= tags
    for t in items:
        AttackDetails.model_validate(t["labels"])  # must validate


def test_stated_labels_are_transcript_substrings(tmp_path):
    """Names, doses, times, triggers, and time expressions are verbatim."""
    items = _load_transcripts(tmp_path)
    for t in items:
        text = t["transcript"].lower()
        labels = t["labels"]
        stated = []
        for key in ("time_expression", "menstrual_context"):
            if labels.get(key):
                stated.append((key, labels[key]))
        stated += [("trigger", v) for v in labels["reported_triggers"]]
        stated += [("other", v) for v in labels["symptoms"]["other"]]
        for med in labels["acute_medications"]:
            for key in ("name", "dose", "time_taken"):
                if med.get(key):
                    stated.append((f"med.{key}", med[key]))
        for kind, value in stated:
            assert value.lower() in text, f"{t['id']} {kind}: {value!r}"
