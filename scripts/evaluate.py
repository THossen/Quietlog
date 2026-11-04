#!/usr/bin/env python3
"""M2 evaluation: extraction accuracy, verifier rejection, summary correctness.

Usage (PowerShell):
  .venv\\Scripts\\Activate.ps1
  python scripts\\make_synthetic_diary.py   # once: writes data/synthetic
  python scripts\\evaluate.py               # writes reports/evaluation.md

Test 1 (extraction accuracy + hallucination before/after the verifier) needs
QUIETLOG_MODEL_API_KEY or a warm response cache. Tests 2 (deliberately
corrupted outputs are rejected) and 3 (summary matches planted aggregates
across generated diaries) are deterministic and key-free.

Exit 0: every selected test ran and passed. Exit 1: a test failed or a model
call errored. Exit 2: test 1 incomplete (no key and a cold cache).

Synthetic evaluation tests the code, not real patients or real speech variety.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from quietlog.llm_service import (  # noqa: E402
    EXTRACTION_PROMPT_VERSION,
    LLMError,
    MissingApiKeyError,
    extract_attack,
    load_config,
)
from quietlog.models import AttackDetails  # noqa: E402
from quietlog.summary import compute_summary  # noqa: E402
from quietlog.types import SYMPTOM_FIELDS  # noqa: E402
from quietlog.verifier import (  # noqa: E402
    attack_to_raw,
    count_claimed,
    count_claimed_attack,
    normalize,
    verify_extraction,
)

import make_synthetic_diary as gen  # noqa: E402

SCALAR_STR = ("time_expression", "menstrual_context")
SCALAR_EXACT = ("duration_minutes", "ongoing", "severity_function", "pain_0_10")


def _norm_or_none(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return normalize(value)
    return None


def _outcome_key(value) -> str | None:
    if value is None:
        return None
    text = value.value if hasattr(value, "value") else str(value)
    text = text.strip().lower()
    return None if text in ("", "unknown") else text


def _med_key(med: dict) -> tuple:
    return (
        normalize(med["name"]),
        _norm_or_none(med.get("dose")),
        _norm_or_none(med.get("time_taken")),
        _outcome_key(med.get("outcome")),
        med.get("second_dose"),
    )


def attack_to_labels(attack: AttackDetails) -> dict:
    """Verified output reshaped like the fixture labels, for scoring."""
    return {
        "time_expression": attack.time_expression,
        "duration_minutes": attack.duration_minutes,
        "ongoing": attack.ongoing,
        "severity_function": attack.severity_function,
        "pain_0_10": attack.pain_0_10,
        "symptoms": {**attack.symptoms.mentioned(), "other": list(attack.symptoms.other)},
        "acute_medications": [m.model_dump() for m in attack.acute_medications],
        "reported_triggers": list(attack.reported_triggers),
        "menstrual_context": attack.menstrual_context,
        "notes": attack.notes,
    }


def raw_to_labels(raw: dict) -> dict:
    """Lenient unverified view of a raw payload (before-verifier scoring).

    Takes values as-is without checking quotes, so accuracy "before" reflects
    the model alone. Malformed slots become None (unparseable, not verified).
    """

    def val(pair):
        if isinstance(pair, dict):
            return pair.get("value")
        return None

    labels: dict = {}
    for key in SCALAR_STR:
        v = val(raw.get(key))
        labels[key] = v.strip() if isinstance(v, str) and v.strip() else None
    for key in SCALAR_EXACT:
        v = val(raw.get(key))
        labels[key] = v
    symptoms = raw.get("symptoms") if isinstance(raw.get("symptoms"), dict) else {}
    labels["symptoms"] = {
        name: val(symptoms.get(name)) if isinstance(val(symptoms.get(name)), bool)
        or val(symptoms.get(name)) is None else None
        for name in SYMPTOM_FIELDS
    }
    other = symptoms.get("other") if isinstance(symptoms.get("other"), list) else []
    labels["symptoms"]["other"] = [
        i["value"].strip() for i in other
        if isinstance(i, dict) and isinstance(i.get("value"), str) and i["value"].strip()
    ]
    meds = raw.get("acute_medications") if isinstance(raw.get("acute_medications"), list) else []
    parsed = []
    for med in meds:
        if not isinstance(med, dict):
            continue
        name = val(med.get("name"))
        if not isinstance(name, str) or not name.strip():
            continue
        dose = val(med.get("dose"))
        taken = val(med.get("time_taken"))
        second = val(med.get("second_dose"))
        parsed.append({
            "name": name.strip(),
            "dose": dose.strip() if isinstance(dose, str) and dose.strip() else None,
            "time_taken": taken.strip() if isinstance(taken, str) and taken.strip() else None,
            "outcome": _outcome_key(val(med.get("outcome"))),
            "second_dose": second if isinstance(second, bool) else None,
        })
    labels["acute_medications"] = parsed
    triggers = raw.get("reported_triggers") if isinstance(raw.get("reported_triggers"), list) else []
    labels["reported_triggers"] = [
        i["value"].strip() for i in triggers
        if isinstance(i, dict) and isinstance(i.get("value"), str) and i["value"].strip()
    ]
    notes = val(raw.get("notes"))
    labels["notes"] = notes.strip() if isinstance(notes, str) and notes.strip() else None
    return labels


def score_labels(got: dict, want: dict) -> dict[str, bool]:
    """Per-field correctness of one extraction against ground-truth labels."""
    result: dict[str, bool] = {}
    for key in SCALAR_STR:
        result[key] = _norm_or_none(got.get(key)) == _norm_or_none(want.get(key))
    for key in SCALAR_EXACT:
        result[key] = got.get(key) == want.get(key)
    for name in SYMPTOM_FIELDS:
        result[f"symptoms.{name}"] = (
            got["symptoms"].get(name) == want["symptoms"].get(name)
        )
    result["symptoms.other"] = Counter(
        normalize(t) for t in got["symptoms"].get("other", [])
    ) == Counter(normalize(t) for t in want["symptoms"].get("other", []))
    result["acute_medications"] = Counter(
        _med_key(m) for m in got.get("acute_medications", [])
    ) == Counter(_med_key(m) for m in want.get("acute_medications", []))
    result["reported_triggers"] = Counter(
        normalize(t) for t in got.get("reported_triggers", [])
    ) == Counter(normalize(t) for t in want.get("reported_triggers", []))
    # Notes are free text: presence agreement only, reported separately.
    result["notes(presence)"] = (got.get("notes") is not None) == (
        want.get("notes") is not None
    )
    return result


FIELD_ORDER = [
    "time_expression", "duration_minutes", "ongoing", "severity_function",
    "pain_0_10", *[f"symptoms.{n}" for n in SYMPTOM_FIELDS], "symptoms.other",
    "acute_medications", "reported_triggers", "menstrual_context",
    "notes(presence)",
]


def run_test1(transcripts: list[dict], *, limit: int, use_cache: bool) -> dict:
    """Extraction accuracy + hallucination rate, before/after the verifier."""
    config = load_config()
    items = transcripts[:limit] if limit else transcripts
    before: Counter[str] = Counter()
    after: Counter[str] = Counter()
    hallu_before = [0, 0]  # [unsupported, claimed]
    hallu_after = [0, 0]
    errors: list[str] = []
    skipped: list[str] = []
    worst: list[tuple[str, int]] = []
    cache_hits = 0
    ran = 0
    for item in items:
        tid = item["id"]
        try:
            result = extract_attack(
                item["transcript"], config=config, use_cache=use_cache
            )
        except MissingApiKeyError:
            skipped.append(tid)
            continue
        except LLMError as exc:
            errors.append(f"{tid}: {exc}")
            continue
        ran += 1
        cache_hits += 1 if result.cache_hit else 0
        claimed_raw = count_claimed(result.raw)
        hallu_before[0] += len(result.failures_before)
        hallu_before[1] += claimed_raw
        final_claimed = count_claimed_attack(result.verified.attack)
        hallu_after[0] += len(result.verified.failures)
        hallu_after[1] += len(result.verified.failures) + final_claimed
        pre = score_labels(raw_to_labels(result.raw), item["labels"])
        post = score_labels(attack_to_labels(result.verified.attack), item["labels"])
        for field_name in FIELD_ORDER:
            before[field_name] += 1 if pre[field_name] else 0
            after[field_name] += 1 if post[field_name] else 0
        missed = sum(1 for f in FIELD_ORDER if f != "notes(presence)" and not post[f])
        worst.append((tid, missed))
    worst.sort(key=lambda t: (-t[1], t[0]))
    status = "complete" if ran and not skipped and not errors else (
        "not_run" if ran == 0 and not errors else "partial"
    )
    return {
        "status": status,
        "n": len(items),
        "ran": ran,
        "skipped": skipped,
        "errors": errors,
        "cache_hits": cache_hits,
        "before": dict(before),
        "after": dict(after),
        "hallucination_before": hallu_before,
        "hallucination_after": hallu_after,
        "worst": worst[:5],
        "model": config.model,
    }


def P(value, quote):  # noqa: N802 - test helper, mirrors verifier pair shape
    return {"value": value, "quote": quote}


def verifier_cases() -> list[tuple[str, str, dict, list[str]]]:
    """(name, transcript, corrupted payload, expected failing paths)."""
    t = ("Migraine since 9am. Moderate, slowed me down. No nausea this time, "
         "just light sensitivity. Took ibuprofen 200mg at ten, helped.")
    good = {
        "time_expression": P("since 9am", "Migraine since 9am"),
        "severity_function": P(2, "Moderate, slowed me down"),
        "symptoms": {
            "nausea": P(False, "No nausea this time"),
            "light_sensitivity": P(True, "just light sensitivity"),
        },
        "acute_medications": [
            {"name": P("ibuprofen", "Took ibuprofen 200mg"),
             "dose": P("200mg", "Took ibuprofen 200mg"),
             "time_taken": P("at ten", "at ten, helped"),
             "outcome": P("relief", "at ten, helped")},
        ],
        "reported_triggers": [P("bad sleep", "bad sleep")],
    }
    import copy

    def mutate(path, pair):
        bad = copy.deepcopy(good)
        node = bad
        *parents, leaf = path.split(".")
        for part in parents:
            m = part.rfind("[")
            if m >= 0:
                node = node[part[:m]][int(part[m + 1:-1])]
            else:
                node = node[part]
        m = leaf.rfind("[")
        if m >= 0:
            node[leaf[:m]][int(leaf[m + 1:-1])] = pair
        else:
            node[leaf] = pair
        return bad

    trigger_transcript = t + " Probably bad sleep."
    return [
        ("control_good", trigger_transcript, good, []),
        ("fabricated_quote", trigger_transcript,
         mutate("severity_function", P(3, "I was completely bedridden")),
         ["severity_function"]),
        ("wrong_duration_number", trigger_transcript,
         {**good, "duration_minutes": P(999, "Moderate, slowed me down")},
         ["duration_minutes"]),
        ("pain_number_mismatch", trigger_transcript,
         {**good, "pain_0_10": P(9, "Moderate, slowed me down")},
         ["pain_0_10"]),
        ("value_not_in_quote", trigger_transcript,
         mutate("time_expression", P("at 5pm", "Migraine since 9am")),
         ["time_expression"]),
        ("missing_quote", trigger_transcript,
         {**good, "ongoing": {"value": True}},
         ["ongoing"]),
        ("symptom_true_negated", trigger_transcript,
         mutate("symptoms.nausea", P(True, "No nausea this time")),
         ["symptoms.nausea"]),
        ("symptom_false_no_denial", trigger_transcript,
         mutate("symptoms.light_sensitivity",
                P(False, "just light sensitivity")),
         ["symptoms.light_sensitivity"]),
        ("malformed_pair", trigger_transcript,
         mutate("severity_function", {"value": "severe-ish", "quote": "Moderate"}),
         ["severity_function"]),
        ("bad_outcome_enum", trigger_transcript,
         mutate("acute_medications[0].outcome", P("cured", "at ten, helped")),
         ["acute_medications[0].outcome"]),
        ("med_name_fabricated", trigger_transcript,
         mutate("acute_medications[0].name", P("morphine", "Took ibuprofen 200mg")),
         ["acute_medications[0].name"]),
        ("trigger_not_substring", t, good, ["reported_triggers[0]"]),
    ]


def run_test2() -> dict:
    """Deliberately corrupted outputs must be rejected; the control passes."""
    rows = []
    for name, transcript, raw, expected in verifier_cases():
        outcome = verify_extraction(transcript, raw)
        got = sorted({f.path for f in outcome.failures})
        rows.append({"name": name, "expected": expected, "got": got,
                     "pass": got == sorted(expected)})
    return {"rows": rows, "passed": sum(1 for r in rows if r["pass"]),
            "total": len(rows)}


def compare_summary(entries, planted: dict, summary) -> list[str]:
    """Every summary number must equal the planted aggregate. Returns diffs."""
    diffs: list[str] = []

    def check(name, got, want):
        if got != want:
            diffs.append(f"{name}: summary={got!r} planted={want!r}")

    check("attack_days", summary.attack_days, planted["attack_days"])
    check("no_attack_days", summary.no_attack_days, planted["no_attack_days"])
    check("not_logged_days", summary.not_logged_days, planted["not_logged_days"])
    check("attack_entries", summary.attack_entries, planted["attack_entries"])
    check("unconfirmed_entries", summary.unconfirmed_entries,
          planted["unconfirmed_entries"])
    check("duration.n", summary.duration.n, planted["duration"]["n"])
    check("duration.median", summary.duration.median_minutes,
          planted["duration"]["median_minutes"])
    check("duration.min", summary.duration.min_minutes, planted["duration"]["min_minutes"])
    check("duration.max", summary.duration.max_minutes, planted["duration"]["max_minutes"])
    check("severity_counts", summary.severity_counts,
          {int(k): v for k, v in planted["severity_counts"].items()})
    check("severity_n", summary.severity_n, planted["severity_n"])
    check("symptoms", summary.symptoms, planted["symptoms"])
    check("symptoms_n", summary.symptoms_n, planted["symptoms_n"])
    got_meds = {m.display_name: (m.attack_entries, m.distinct_days, m.outcomes)
                for m in summary.medications}
    want_meds = {m["display_name"]: (m["attack_entries"], m["distinct_days"], m["outcomes"])
                 for m in planted["medications"]}
    check("medications", got_meds, want_meds)
    check("triggers", summary.triggers, planted["triggers"])
    check("triggers_n", summary.triggers_n, planted["triggers_n"])

    attack_dates = {e.entry_date for e in entries if e.entry_type.value == "attack"}
    checkin_dates = {e.entry_date for e in entries if e.entry_type.value == "no_attack"}
    unconfirmed = {e.entry_date for e in entries
                   if e.entry_type.value == "attack" and e.status.value != "confirmed"}
    for day in summary.calendar:
        want_state = ("attack" if day.day in attack_dates
                      else "no_attack" if day.day in checkin_dates else "not_logged")
        if day.state != want_state or day.unconfirmed != (day.day in unconfirmed):
            diffs.append(f"calendar {day.day}: summary=({day.state}, "
                         f"unconfirmed={day.unconfirmed})")
            break  # one calendar line is enough to fail loudly
    return diffs


def run_test3(fixtures: Path, seeds: list[int]) -> dict:
    """Summary correctness on the committed fixtures plus fresh seeds."""
    from quietlog.models import Entry

    rows = []
    diary_path = fixtures / "diary.json"
    planted_path = fixtures / "aggregates.json"
    if diary_path.exists() and planted_path.exists():
        diary = json.loads(diary_path.read_text(encoding="utf-8"))
        planted = json.loads(planted_path.read_text(encoding="utf-8"))
        entries = [Entry.model_validate(e) for e in diary["entries"]]
        summary = compute_summary(
            entries, date.fromisoformat(diary["window_start"]),
            date.fromisoformat(diary["window_end"]),
            generated_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
        )
        diffs = compare_summary(entries, planted, summary)
        rows.append({"seed": f"fixtures(seed {diary.get('seed')})",
                     "diffs": diffs, "pass": not diffs})
    else:
        rows.append({"seed": "fixtures", "diffs": ["fixtures missing: run "
                     "python scripts\\make_synthetic_diary.py first"],
                     "pass": False})
    for seed in seeds:
        plans, checkins, all_days = gen.plan_diary(seed, 90, gen.ANCHOR_END)
        entries = gen.build_entries(plans, checkins)
        planted = gen.planted_aggregates(plans, checkins, all_days)
        summary = compute_summary(
            entries, all_days[0], all_days[-1],
            generated_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
        )
        diffs = compare_summary(entries, planted, summary)
        rows.append({"seed": f"seed {seed}", "diffs": diffs, "pass": not diffs})
    return {"rows": rows, "passed": sum(1 for r in rows if r["pass"]),
            "total": len(rows)}


def _pct(num: int, den: int) -> str:
    return f"{100 * num / den:.1f}%" if den else "n/a"


def write_report(path: Path, *, test1: dict | None, test2: dict | None,
                 test3: dict | None) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Quietlog M2 evaluation",
        "",
        f"Generated {now} by `scripts/evaluate.py`.",
        f"Extraction prompt version: `{EXTRACTION_PROMPT_VERSION}`.",
        "",
    ]
    if test1 is not None:
        lines += ["## Test 1 — extraction accuracy and hallucination rate", ""]
        if test1["status"] == "not_run":
            lines += [
                "**NOT RUN.** No `QUIETLOG_MODEL_API_KEY` and no cached responses "
                f"for {test1['n']} transcripts. Set the key in `.env` (see "
                "`.env.example`) and re-run; results below are absent, not zero.",
                "",
            ]
        else:
            if test1["status"] == "partial":
                lines += [
                    f"**PARTIAL.** {test1['ran']}/{test1['n']} transcripts ran; "
                    f"{len(test1['skipped'])} skipped (no key, cold cache), "
                    f"{len(test1['errors'])} errored. Numbers below cover only "
                    "the transcripts that ran.",
                    "",
                ]
            n = test1["ran"]
            lines += [
                f"Model `{test1['model']}`, {n} transcripts "
                f"({test1['cache_hits']} cache hits).",
                "",
                "| field | before verifier | after verifier |",
                "|---|---:|---:|",
            ]
            for field_name in FIELD_ORDER:
                b, a = test1["before"][field_name], test1["after"][field_name]
                lines.append(f"| `{field_name}` | {b}/{n} ({_pct(b, n)}) "
                             f"| {a}/{n} ({_pct(a, n)}) |")
            scored = [f for f in FIELD_ORDER if f != "notes(presence)"]
            b_tot = sum(test1["before"][f] for f in scored)
            a_tot = sum(test1["after"][f] for f in scored)
            lines += [
                f"| **micro average (excl. notes)** | **{b_tot}/{n * len(scored)} "
                f"({_pct(b_tot, n * len(scored))})** | **{a_tot}/{n * len(scored)} "
                f"({_pct(a_tot, n * len(scored))})** |",
                "",
                "Hallucination rate (non-null fields not supported by the transcript):",
                "",
            ]
            hb, ha = test1["hallucination_before"], test1["hallucination_after"]
            lines += [
                f"- before verifier: {hb[0]}/{hb[1]} ({_pct(hb[0], hb[1])})",
                f"- after verifier: {ha[0]}/{ha[1]} ({_pct(ha[0], ha[1])})",
                "",
            ]
            if test1["worst"]:
                lines += ["Most-missed transcripts (fields wrong after verifier):", ""]
                lines += [f"- {tid}: {missed}" for tid, missed in test1["worst"]]
                lines += [""]
            for err in test1["errors"]:
                lines.append(f"- ERROR {err}")
            if test1["errors"]:
                lines.append("")
            lines += [
                "Notes are free text, so `notes(presence)` scores presence agreement "
                "only and stays out of the micro average.",
                "",
            ]
    if test2 is not None:
        lines += ["## Test 2 — verifier rejects corrupted outputs", "",
                  f"{test2['passed']}/{test2['total']} cases behaved as expected.",
                  "",
                  "| case | expected rejections | got | result |",
                  "|---|---|---|---|",
                  ]
        for row in test2["rows"]:
            exp = ", ".join(row["expected"]) or "(none)"
            got = ", ".join(row["got"]) or "(none)"
            mark = "PASS" if row["pass"] else "FAIL"
            lines.append(f"| {row['name']} | `{exp}` | `{got}` | {mark} |")
        lines.append("")
    if test3 is not None:
        lines += ["## Test 3 — summary matches planted aggregates", "",
                  f"{test3['passed']}/{test3['total']} diaries match exactly.",
                  "",
                  "| diary | result | diffs |",
                  "|---|---|---|",
                  ]
        for row in test3["rows"]:
            mark = "PASS" if row["pass"] else "FAIL"
            diffs = "; ".join(row["diffs"][:3]) or "(none)"
            lines.append(f"| {row['seed']} | {mark} | {diffs} |")
        lines.append("")
    lines += [
        "## What this does and does not show",
        "",
        "Synthetic evaluation tests the code, not real patients or real speech "
        "variety. Test 1 measures extraction against hand-written transcripts; "
        "real voice notes, accents, and background noise are out of scope until "
        "the SHOULD-tier real-speech test (M5).",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the M2 evaluation (tests 1-3).")
    parser.add_argument("--fixtures", type=Path, default=ROOT / "data" / "synthetic")
    parser.add_argument("--out", type=Path, default=ROOT / "reports" / "evaluation.md")
    parser.add_argument("--seeds", default="1,7,99")
    parser.add_argument("--limit", type=int, default=0,
                        help="score at most N transcripts in test 1 (0 = all)")
    parser.add_argument("--only", default="1,2,3",
                        help="comma-separated subset of tests to run")
    parser.add_argument("--no-cache", action="store_true",
                        help="bypass the response cache (spends API budget)")
    args = parser.parse_args()
    selected = {s.strip() for s in args.only.split(",") if s.strip()}

    test1 = test2 = test3 = None
    if "1" in selected:
        lines = (args.fixtures / "transcripts.jsonl").read_text(
            encoding="utf-8").splitlines()
        transcripts = [json.loads(line) for line in lines if line.strip()]
        test1 = run_test1(transcripts, limit=args.limit,
                           use_cache=not args.no_cache)
        print(f"test 1: {test1['status']} "
              f"({test1['ran']}/{test1['n']} ran, {test1['cache_hits']} cache hits)")
    if "2" in selected:
        test2 = run_test2()
        print(f"test 2: {test2['passed']}/{test2['total']} cases pass")
    if "3" in selected:
        seeds = [int(s) for s in args.seeds.split(",") if s.strip()]
        test3 = run_test3(args.fixtures, seeds)
        print(f"test 3: {test3['passed']}/{test3['total']} diaries match")
    write_report(args.out, test1=test1, test2=test2, test3=test3)
    print(f"wrote {args.out}")

    if test2 is not None and test2["passed"] != test2["total"]:
        return 1
    if test3 is not None and test3["passed"] != test3["total"]:
        return 1
    if test1 is not None:
        if test1["errors"]:
            return 1
        if test1["status"] in ("not_run", "partial"):
            print("test 1 incomplete: set QUIETLOG_MODEL_API_KEY and re-run.")
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
