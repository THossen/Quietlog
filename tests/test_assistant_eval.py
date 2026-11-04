"""Multi-turn assistant eval: scripted turns against one database.

Each case runs scripted model turns and asserts on DATABASE STATE and TOOL
ACTIONS only — never on reply wording. All model calls are stubbed; the LLM
cache is per-test tmp, so identical inputs replay deterministically and every
case below varies its messages accordingly.

Covers the requirement-6 matrix at conversation level: duplicate requests,
partial success, ambiguous targets, invalid arguments, model failure after a
successful tool call, and recovery flows. (Concurrent retries and edit
preservation are covered in test_db.py / test_agent.py.)
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from quietlog import agent, db as db_mod
from quietlog.llm_service import LLMError
from quietlog.models import AttackDetails, Entry
from quietlog.settings import Settings
from quietlog.types import EntryStatus, EntryType


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("QUIETLOG_MODEL_API_KEY", "test-key")


@pytest.fixture()
def fx(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "t.db"),
        audio_dir=str(tmp_path / "audio"),
        cache_dir=str(tmp_path / "cache"),
    )
    conn = db_mod.connect(settings.db_path)
    yield {"settings": settings, "conn": conn, "tmp": tmp_path}
    conn.close()


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


def _scripted_post(script, seen=None):
    def fake_post(url, headers=None, payload=None, timeout=None):
        if seen is not None:
            seen.append(payload)
        content = script.pop(0)
        return FakeResponse(200, {"choices": [{"message": {"content": content}}]})
    return fake_post


def _fake_process(conn, entry, settings, coords=None):
    prev = entry.attack
    entry.attack = AttackDetails(pain_0_10=7)
    if prev is not None:
        # Mirror process_entry: user-set fields survive re-extraction.
        for path in ("attack_start", "attack_end", "duration_minutes", "ongoing"):
            value = getattr(prev, path)
            if value is not None:
                setattr(entry.attack, path, value)
        entry.attack.edited_fields = list(prev.edited_fields)
    entry.status = EntryStatus.UNCONFIRMED
    entry.processing_error = None
    return db_mod.save_entry(conn, entry)


def _action(tool, args=None):
    return json.dumps({"action": {"tool": tool, "args": args or {}}})


def _reply(text):
    return json.dumps({"reply": text})


def run_turn(fx, message, script, context=None):
    """One scripted turn; returns (result, seen_payloads)."""
    seen = []
    result = agent.run_agent(
        message, [], fx["settings"], fx["conn"],
        http_post=_scripted_post(script, seen),
        process_entry_fn=_fake_process,
        context=context,
    )
    return result, seen


def attacks(fx):
    return [e for e in db_mod.list_entries(fx["conn"])
            if e.entry_type == EntryType.ATTACK]


def test_log_then_answer_followups(fx):
    first, _ = run_turn(
        fx, "I have a headache.",
        [_action("log_attack", {"transcript": "pounding"}), _reply("Saved.")])
    assert len(attacks(fx)) == 1
    entry_id = first.actions[0].entry_id
    assert first.actions[0].followups != ()
    assert first.state["pending_entry_id"] == entry_id

    second, _ = run_turn(
        fx, "Pain is 8.",
        [_action("answer_followups", {
            "entry_id": entry_id,
            "answers": [{"question": "How bad was the pain, 0-10?", "answer": "8"}]}),
         _reply("Noted.")],
        context=first.state)
    assert second.actions[0].ok is True
    assert len(attacks(fx)) == 1
    assert "8" in (attacks(fx)[0].transcript or "")


def test_timer_note_stop_chain(fx):
    run_turn(fx, "start the timer",
             [_action("timer_start"), _reply("Timing.")])
    run_turn(fx, "Also, I took ibuprofen at noon.",
             [_action("add_note", {"transcript": "Took ibuprofen at noon."}),
              _reply("Added.")])
    last, _ = run_turn(fx, "It's over.",
                       [_action("timer_end"), _reply("Ended.")])
    rows = attacks(fx)
    assert len(rows) == 1
    assert rows[0].attack.attack_end is not None
    assert rows[0].attack.duration_minutes is not None
    assert "ibuprofen" in (rows[0].transcript or "")
    assert last.actions[0].entry_id == rows[0].id


def test_ambiguous_add_looks_up_before_asking(fx):
    for day, text in [(1, "morning migraine"), (2, "evening headache")]:
        db_mod.save_entry(fx["conn"], Entry(
            entry_type=EntryType.ATTACK,
            created_at=datetime(2026, 10, day, 12, tzinfo=timezone.utc),
            transcript=text, status=EntryStatus.CONFIRMED))
    result, _ = run_turn(
        fx, "add nausea to that one",
        [_action("find_entries", {"contains": "headache"}), _reply("Which?")])
    assert result.actions[0].tool == "find_entries"
    assert result.actions[0].ok is True
    assert len(attacks(fx)) == 2  # lookup wrote nothing


def test_invalid_arguments_write_nothing(fx):
    result, _ = run_turn(
        fx, "do it",
        [_action("log_attack", {"transcript": "x", "bogus": 1}), _reply("Nope.")])
    assert result.actions[0].ok is False
    assert result.actions[0].status == "tool_error"
    assert attacks(fx) == []
    result, _ = run_turn(
        fx, "do it differently",
        [_action("answer_diary", {"question": 42}), _reply("Nope.")])
    assert result.actions[0].ok is False
    assert attacks(fx) == []


def test_model_failure_after_write_keeps_receipt(fx):
    calls = {"n": 0}
    scripted = _scripted_post(
        [_action("log_attack", {"transcript": "pounding"}), _reply("lost")], [])

    def flaky_post(url, headers=None, payload=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise LLMError("Model request failed: down")
        return scripted(url, headers, payload, timeout)

    result = agent.run_agent(
        "log it", [], fx["settings"], fx["conn"],
        http_post=flaky_post, process_entry_fn=_fake_process)
    assert len(attacks(fx)) == 1
    assert result.actions[0].tool == "log_attack"
    assert result.actions[0].ok is True
    assert result.actions[-1].status == "model_error"


def test_never_mind_after_write_keeps_entry(fx):
    first, _ = run_turn(
        fx, "headache",
        [_action("log_attack", {"transcript": "pounding"}), _reply("Saved.")])
    entry_id = first.actions[0].entry_id
    second, _ = run_turn(
        fx, "never mind",
        [_action("get_entry", {"entry_id": entry_id}), _reply("Kept.")],
        context=first.state)
    assert second.actions[0].ok is True
    assert db_mod.count_entries(fx["conn"]) == 1
    assert db_mod.get_entry(fx["conn"], entry_id) is not None


def test_date_correction_moves_entry(fx):
    first, _ = run_turn(
        fx, "headache",
        [_action("log_attack", {"transcript": "pounding at 8am"}), _reply("Saved.")])
    entry_id = first.actions[0].entry_id
    before = db_mod.get_entry(fx["conn"], entry_id).entry_date
    assert before is not None
    target = (before - timedelta(days=1)).isoformat()
    second, _ = run_turn(
        fx, "actually that was yesterday",
        [_action("set_entry_date", {"entry_id": entry_id, "date": target}),
         _reply("Moved.")],
        context=first.state)
    assert second.actions[0].ok is True
    assert len(attacks(fx)) == 1
    assert db_mod.get_entry(fx["conn"], entry_id).entry_date.isoformat() == target


def test_advice_question_uses_diary_tool(fx):
    db_mod.save_entry(fx["conn"], Entry(
        entry_type=EntryType.ATTACK,
        created_at=datetime.now(timezone.utc),
        transcript="migraine, took sumatriptan which helped",
        status=EntryStatus.CONFIRMED,
        attack=AttackDetails(pain_0_10=8)))
    result, _ = run_turn(
        fx, "should I take sumatriptan again?",
        [_action("answer_diary", {"question": "should I take sumatriptan again?"}),
         "Sumatriptan helped once, per the diary.",
         _reply("See the diary.")])
    assert result.actions[0].tool == "answer_diary"
    assert result.actions[0].ok is True
    assert len(attacks(fx)) == 1  # a question never writes


def test_record_keeping_boundaries_in_prompts():
    prompt = agent.AGENT_SYSTEM_PROMPT.lower()
    assert "never recommend treatments" in prompt
    assert "never invent" in prompt
    assert "never diagnoses" in prompt or "diagnoses" in prompt
    from quietlog import llm_service

    ask = llm_service.ASK_SYSTEM_PROMPT
    assert "ONLY from the entries" in ask
    assert "never recommend treatments" in ask.lower()
    assert "not medical advice" in ask.lower()
