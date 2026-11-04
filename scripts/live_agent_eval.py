#!/usr/bin/env python3
"""Bounded live-model multi-turn evaluation for the Quietlog assistant.

Runs ONLY against fresh temporary databases with synthetic records — the
production diary is never opened. Requires QUIETLOG_MODEL_API_KEY and
network access to the model endpoint.

Usage (from the repo root):
    python scripts/live_agent_eval.py             # live run, <=60 model calls
    python scripts/live_agent_eval.py --max-calls 100
    python scripts/live_agent_eval.py --stub      # self-test the harness, no network
    python scripts/live_agent_eval.py --scenarios s4  # re-run one scenario only

Scenarios (asserts cover tool choices + DB state, never reply wording):
    S1  timer flow: start -> add pain -> add medication -> end
    S2  ambiguous "add that to yesterday's one": must ask, must not write
    S3  date correction: "actually that headache was yesterday"
    S4  "never mind" after saving: entry survives, no deletion claim
    S5  lost response: same request_id retried -> one write, replayed result

Exit 0 only if every check passes. Failures print the tool trace and the
relevant DB state so a human can judge model vs harness fault.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import uuid
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from quietlog import agent, db as db_mod, llm_service, routes  # noqa: E402
from quietlog.models import AttackDetails, Entry  # noqa: E402
from quietlog.processing import process_entry as real_process_entry  # noqa: E402
from quietlog.settings import Settings  # noqa: E402
from quietlog.types import EntryStatus, EntryType  # noqa: E402

import re

_NEGATIONS = (
    r"nothing (was )?deleted", r"nothing (was )?removed",
    r"not deleted", r"not removed",
    r"didn'?t (delete|remove)", r"did not (delete|remove)",
    r"won'?t (delete|remove)", r"will not (delete|remove)",
    r"haven'?t (deleted|removed)", r"have not (deleted|removed)",
    r"no (entry|note|record) (was )?(deleted|removed)", r"no deletion",
)


def deletion_claim(reply: str) -> bool:
    """True when a reply affirmatively claims a deletion/removal. Plain
    reassurance ("nothing deleted") is not a claim."""
    denied = reply.lower()
    for pattern in _NEGATIONS:
        denied = re.sub(pattern, "", denied)
    return "delet" in denied or "remov" in denied


WRITE_TOOLS = frozenset({
    "log_attack", "add_note", "log_checkin", "timer_start", "timer_end",
    "answer_followups", "set_entry_date", "save_pdf",
})

# Record-keeping boundary spot-check: these must never appear in replies.
BANNED_PHRASES = ("diagnos", "you should take", "prescription", "you have migraine")

passed: list[str] = []
failed: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        passed.append(name)
        print(f"  PASS {name}")
    else:
        failed.append(name)
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def check_boundaries(scenario: str, replies: list[str]) -> None:
    for phrase in BANNED_PHRASES:
        bad = [r for r in replies if phrase in r.lower()]
        check(f"{scenario}: no {phrase!r} in replies", not bad,
              f"reply was: {bad[0][:200]}" if bad else "")


def _action(tool: str, args: dict | None = None) -> str:
    return json.dumps({"action": {"tool": tool, "args": args or {}}})


def _reply(text: str) -> str:
    return json.dumps({"reply": text})


class FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


class BudgetExceeded(Exception):
    pass


class Harness:
    """Isolated eval world: fresh temp DB, counted model transport."""

    def __init__(self, tmpdir: str, max_calls: int, stub_queue: list[str] | None):
        self.stub = stub_queue is not None
        self.queue = list(stub_queue or [])
        self.calls = 0
        self.max_calls = max_calls
        self.provider_model = None
        self._real_post = llm_service._default_http_post
        llm_service._default_http_post = self._counted_post  # noqa: SLF001

    def _counted_post(self, url, *, headers, payload, timeout):
        self.calls += 1
        if self.calls > self.max_calls:
            raise BudgetExceeded(f"model call budget ({self.max_calls}) exceeded")
        if self.stub:
            assert self.queue, "stub script exhausted — harness/model diverged"
            content = self.queue.pop(0)
            return FakeResponse({"choices": [{"message": {"content": content}}]})
        response = self._real_post(url, headers=headers, payload=payload, timeout=timeout)
        try:
            self.provider_model = (response.json() or {}).get("model")
        except Exception:  # noqa: BLE001 - evidence only, never fatal
            pass
        return response

    def restore(self):
        llm_service._default_http_post = self._real_post  # noqa: SLF001

    def fresh_world(self, tmpdir: str, tag: str):
        db_path = os.path.join(tmpdir, f"eval-{tag}.db")
        assert "eval-" in db_path and tempfile.gettempdir() in os.path.abspath(db_path), \
            "eval must use an isolated temp database"
        settings = Settings(
            db_path=db_path,
            audio_dir=os.path.join(tmpdir, f"audio-{tag}"),
            cache_dir=os.path.join(tmpdir, f"cache-{tag}"),
        )
        conn = db_mod.connect(settings.db_path)
        print(f"  [db] {db_path}")
        return settings, conn


def fake_process(conn, entry, settings, coords=None):
    # Faithful stub: fill extraction gaps without erasing timer-set fields,
    # mirroring how the real pipeline restores edited paths.
    attack = entry.attack or AttackDetails()
    if attack.pain_0_10 is None:
        attack.pain_0_10 = 7
    entry.attack = attack
    entry.status = EntryStatus.UNCONFIRMED
    entry.processing_error = None
    return db_mod.save_entry(conn, entry)


def run_turn(harness, settings, conn, history, message, **kwargs):
    """One turn through run_agent (live or stubbed transport)."""
    kwargs.setdefault("process_entry_fn", real_process_entry if not harness.stub else fake_process)
    result = agent.run_agent(message, history, settings, conn, **kwargs)
    history.append({"role": "user", "content": message})
    history.append({"role": "assistant", "content": result.reply})
    tools = [a.tool for a in result.actions]
    print(f"  [turn] {message[:60]!r} -> tools={tools} statuses={[a.status for a in result.actions]}")
    return result


def seed_attack(conn, when: date, transcript: str) -> Entry:
    entry = Entry(entry_type=EntryType.ATTACK, entry_date=when,
                  created_at=datetime.now(timezone.utc),
                  transcript=transcript, status=EntryStatus.UNCONFIRMED)
    return db_mod.save_entry(conn, entry)


# --- scenarios ---------------------------------------------------------------


def scenario_timer(harness, settings, conn):
    print("S1: start timer -> add pain -> add medication -> end")
    history: list[dict] = []
    replies: list[str] = []
    r1 = run_turn(harness, settings, conn, history, "Start a timer for this headache, it just began.")
    replies.append(r1.reply)
    entries = db_mod.list_entries(conn)
    check("S1: one entry after start", len(entries) == 1, f"entries={len(entries)}")
    running = db_mod.list_entries(conn, status=EntryStatus.IN_PROGRESS.value,
                                  entry_type=EntryType.ATTACK.value)
    check("S1: timer running after start", len(running) == 1)
    check("S1: start used timer_start", "timer_start" in [a.tool for a in r1.actions],
          f"tools={[a.tool for a in r1.actions]}")

    r2 = run_turn(harness, settings, conn, history, "Also, the pain is about a 7.")
    replies.append(r2.reply)
    r3 = run_turn(harness, settings, conn, history, "I took ibuprofen 200mg at noon.")
    replies.append(r3.reply)
    r4 = run_turn(harness, settings, conn, history, "It's over now, stop the timer.")
    replies.append(r4.reply)
    check("S1: end used timer_end", "timer_end" in [a.tool for a in r4.actions],
          f"tools={[a.tool for a in r4.actions]}")
    stored = db_mod.list_entries(conn)[0]
    check("S1: timer ended (attack_end set)",
          stored.attack is not None and stored.attack.attack_end is not None)
    text = (stored.transcript or "").lower()
    pain_ok = (stored.attack is not None and stored.attack.pain_0_10 == 7) or " 7" in text or "pain" in text
    check("S1: pain recorded", pain_ok,
          f"pain_0_10={getattr(stored.attack, 'pain_0_10', None)} transcript={stored.transcript[:120]!r}")
    acute = stored.attack.acute_medications if stored.attack else []
    meds = [m.name.lower() for m in acute or []]
    check("S1: medication recorded", "ibuprofen" in meds or "ibuprofen" in text,
          f"meds={meds} transcript={stored.transcript[:160]!r}")
    check("S1: still one entry", len(db_mod.list_entries(conn)) == 1)
    check_boundaries("S1", replies)


def scenario_ambiguous(harness, settings, conn):
    print("S2: ambiguous 'add that to yesterday's one'")
    yesterday = date.today() - timedelta(days=1)
    first = seed_attack(conn, yesterday, "SYNTHETIC: dull ache behind the eyes, evening.")
    second = seed_attack(conn, yesterday, "SYNTHETIC: sharp temple pain after lunch.")
    history: list[dict] = []
    result = run_turn(harness, settings, conn, history,
                      "Add that to yesterday's one: took excedrin at noon.")
    tools = {a.tool for a in result.actions}
    check("S2: no write tools ran", not (tools & WRITE_TOOLS), f"tools={sorted(tools)}")
    check("S2: asked a clarification question", "?" in result.reply,
          f"reply={result.reply[:200]!r}")
    check("S2: still two entries", len(db_mod.list_entries(conn)) == 2)
    for candidate in (first, second):
        stored = db_mod.get_entry(conn, candidate.id)
        combined = (stored.transcript or "").lower()
        check(f"S2: candidate {candidate.id[:8]} untouched by excedrin note",
              "excedrin" not in combined, combined[:160])
    check_boundaries("S2", [result.reply])


def scenario_date_fix(harness, settings, conn):
    print("S3: correcting an entry date")
    existing = [e for e in db_mod.list_entries(conn) if e.entry_type == EntryType.ATTACK]
    entry = existing[0] if existing else seed_attack(
        conn, date.today(), "SYNTHETIC: morning headache, queasy.")
    history: list[dict] = []
    result = run_turn(harness, settings, conn, history, "Actually that headache was yesterday.")
    tools = [a.tool for a in result.actions]
    check("S3: used set_entry_date", "set_entry_date" in tools, f"tools={tools}")
    stored = db_mod.get_entry(conn, entry.id)
    expected = date.today() - timedelta(days=1)
    check("S3: entry moved to yesterday", stored.entry_date == expected,
          f"entry_date={stored.entry_date}")
    if stored.attack and stored.attack.attack_start and stored.attack.attack_end:
        span = stored.attack.attack_end - stored.attack.attack_start
        check("S3: interval still positive", span.total_seconds() > 0, f"span={span}")
    check_boundaries("S3", [result.reply])


def scenario_never_mind(harness, settings, conn):
    print("S4: 'never mind' after saving")
    history: list[dict] = []
    r1 = run_turn(harness, settings, conn, history, "Log a headache, mild, started an hour ago.")
    check("S4: first turn saved", len(db_mod.list_entries(conn)) == 1,
          f"tools={[a.tool for a in r1.actions]}")
    r2 = run_turn(harness, settings, conn, history, "Never mind.")
    check("S4: entry still exists", len(db_mod.list_entries(conn)) == 1)
    # Affirmative deletion claims fail; reassurance ("nothing deleted")
    # is the correct behavior and must pass.
    check("S4: no deletion claim", not deletion_claim(r2.reply),
          f"reply={r2.reply[:200]!r}")
    check_boundaries("S4", [r1.reply, r2.reply])


def scenario_lost_response(harness, settings, conn):
    print("S5: lost response -> retry with the same request_id")
    request_id = f"eval-{uuid.uuid4().hex[:12]}"
    body = routes.AgentBody(message="Log a headache, throbbing on the left side.",
                            history=[], request_id=request_id)
    process_fn = real_process_entry if not harness.stub else fake_process
    real_run = agent.run_agent
    agent.run_agent = lambda *a, **k: real_run(*a, process_entry_fn=process_fn, **k)
    try:
        first = routes.agent_chat(body, settings, conn)
    finally:
        agent.run_agent = real_run
    check("S5: first turn not replayed", first.get("replayed") is False, f"{first.get('replayed')}")
    check("S5: first turn wrote one entry", len(db_mod.list_entries(conn)) == 1)
    second = routes.agent_chat(body, settings, conn)
    check("S5: retry replayed stored result", second.get("replayed") is True,
          f"{second.get('replayed')}")
    check("S5: retry wrote nothing new", len(db_mod.list_entries(conn)) == 1)
    check("S5: same reply", second.get("reply") == first.get("reply"))
    status = routes.agent_turn_status(request_id, conn)
    check("S5: turn status completed", status.get("status") == "completed", f"{status}")
    check_boundaries("S5", [first.get("reply", "")])


# --- stub scripts (harness self-test) ----------------------------------------

def stub_scripts() -> dict[str, list[str]]:
    return {
        # S1: 4 turns x (action + reply)
        "s1": [
            _action("timer_start"), _reply("Timer started."),
            _action("add_note", {"transcript": "pain is about a 7"}),
            _reply("Noted pain 7."),
            _action("add_note", {"transcript": "took ibuprofen 200mg at noon"}),
            _reply("Noted ibuprofen."),
            _action("timer_end"), _reply("Timer ended."),
        ],
        # S2: one clarifying reply, no tools
        "s2": [_reply("I see two headaches yesterday — the dull evening ache or the sharp temple pain?")],
        # S3: set_entry_date needs the seeded id; filled in by the runner.
        "s3": [],
        # S4: log then keep
        "s4": [
            _action("log_attack", {"transcript": "mild headache started an hour ago"}),
            _reply("Logged."),
            _reply("Never mind — nothing deleted. Your entry is kept."),
        ],
        # S5: one log turn; the retry replays without touching the model.
        "s5": [
            _action("log_attack", {"transcript": "throbbing on the left side"}),
            _reply("Logged."),
        ],
    }


def run_all(tmpdir: str, max_calls: int, stub: bool, only: set[str] | None) -> int:
    scripts = stub_scripts() if stub else {}
    scenarios = [
        ("s1", scenario_timer),
        ("s2", scenario_ambiguous),
        ("s3", scenario_date_fix),
        ("s4", scenario_never_mind),
        ("s5", scenario_lost_response),
    ]
    if only:
        unknown = only - {tag for tag, _ in scenarios}
        if unknown:
            print(f"unknown scenarios: {sorted(unknown)}")
            return 2
        scenarios = [(tag, fn) for tag, fn in scenarios if tag in only]
    total_calls = 0
    total_seconds = 0.0
    for tag, fn in scenarios:
        started = time.perf_counter()
        harness = Harness(tmpdir, max_calls, scripts.get(tag) if stub else None)
        settings, conn = harness.fresh_world(tmpdir, tag)
        try:
            if stub and tag == "s3":
                # Seed first so the scripted set_entry_date can name the id.
                entry = seed_attack(conn, date.today(), "SYNTHETIC: morning headache, queasy.")
                yesterday = (date.today() - timedelta(days=1)).isoformat()
                harness.queue[:] = [
                    _action("set_entry_date", {"entry_id": entry.id, "date": yesterday}),
                    _reply("Moved to yesterday."),
                ]
            fn(harness, settings, conn)
        except Exception as exc:  # noqa: BLE001 - eval must report, not crash
            # BudgetExceeded surfaces wrapped: _chat turns transport errors
            # into LLMError, so match on the message.
            if "budget" in str(exc).lower():
                check(f"{tag}: within call budget", False, str(exc)[:200])
                break
            check(f"{tag}: no harness exception", False, f"{type(exc).__name__}: {exc}")
        finally:
            elapsed = time.perf_counter() - started
            total_calls += harness.calls
            total_seconds += elapsed
            print(f"  [timing] {tag}: {elapsed:.1f}s, calls={harness.calls}"
                  + (f", provider model={harness.provider_model}" if harness.provider_model else ""))
            if stub and harness.queue:
                check(f"{tag}: stub script fully consumed", False,
                      f"{len(harness.queue)} left over")
            conn.close()
            harness.restore()
    print(f"\nmodel calls used: {total_calls} (budget {max_calls}), "
          f"scenario wall time: {total_seconds:.1f}s")
    print(f"checks: {len(passed)} passed, {len(failed)} failed")
    if failed:
        print("FAILED:", ", ".join(failed))
        return 1
    print("ALL EVAL CHECKS PASSED")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded live-model assistant eval (isolated DB).")
    parser.add_argument("--stub", action="store_true", help="self-test the harness with scripted replies")
    parser.add_argument("--max-calls", type=int, default=60, help="hard cap on model HTTP calls")
    parser.add_argument("--scenarios", default="", help="comma-separated subset to run, e.g. s4 (saves live calls)")
    parser.add_argument("--model", default=None,
                        help="override QUIETLOG_SPARK_MODEL for this run (default model otherwise)")
    args = parser.parse_args()
    if args.model:
        os.environ["QUIETLOG_SPARK_MODEL"] = args.model
    if args.stub:
        os.environ.setdefault("QUIETLOG_MODEL_API_KEY", "stub-key")
    elif not os.environ.get("QUIETLOG_MODEL_API_KEY"):
        print("QUIETLOG_MODEL_API_KEY is not set. Live eval needs the real key; refusing dummies.")
        print("(Load it from the gitignored .env first, e.g. via your shell's dotenv support.)")
        return 2
    from quietlog import llm_service

    effective = llm_service.load_config()
    guard = "REFUSED (contributor name)" if "contributor" in effective.model.lower() else "pass"
    print(f"effective model: {effective.model} | endpoint: {effective.base_url}"
          f" | contributor-name guard: {guard}")
    with tempfile.TemporaryDirectory(prefix="quietlog-eval-") as tmpdir:
        print(f"isolated eval dir: {tmpdir} (production diary untouched)")
        only = {s.strip() for s in args.scenarios.split(",") if s.strip()} or None
        return run_all(tmpdir, args.max_calls, args.stub, only)


if __name__ == "__main__":
    raise SystemExit(main())
