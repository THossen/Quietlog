"""Chat agent tests: tool loop, tools, context, route validation.

All model calls are stubbed. Nothing here needs an API key or the internet.
"""

import json
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from quietlog import agent, db as db_mod
from quietlog import routes
from quietlog.llm_service import LLMError
from quietlog.models import AcuteMedication, AttackDetails, Entry
from quietlog.processing import process_entry
from quietlog.settings import Settings
from quietlog.types import EntryStatus, EntryType, MedicationOutcome


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
    entry.attack = AttackDetails(pain_0_10=7)
    entry.status = EntryStatus.UNCONFIRMED
    entry.processing_error = None
    return db_mod.save_entry(conn, entry)


def _action(tool, args=None):
    return json.dumps({"action": {"tool": tool, "args": args or {}}})


def _reply(text):
    return json.dumps({"reply": text})


def _run(fx, message, script, **kwargs):
    seen = []
    kwargs.setdefault("process_entry_fn", _fake_process)
    result = agent.run_agent(
        message, [], fx["settings"], fx["conn"],
        http_post=_scripted_post(script, seen),
        process_entry_fn=kwargs.pop("process_entry_fn"),
        **kwargs,
    )
    return result, seen


# --- weather in chat tools ---------------------------------------------------


def test_chat_log_attaches_weather_with_coords(fx, monkeypatch):
    from quietlog.models import WeatherSnapshot

    snapshots = []

    def fake_snap(lat, lon, enabled=True, timeout=6.0):
        snapshots.append((lat, lon))
        return WeatherSnapshot(pressure_hpa=1012.0, source="open-meteo")

    monkeypatch.setattr("quietlog.agent.weather_service.log_time_snapshot", fake_snap)
    result, _ = _run(
        fx,
        "migraine since lunch",
        [_action("log_attack", {"transcript": "migraine since lunch"}),
         _reply("Logged.")],
        coords=(40.7, -74.0),
    )
    assert snapshots == [(40.7, -74.0)]
    stored = db_mod.get_entry(fx["conn"], result.actions[0].entry_id)
    assert stored is not None and stored.weather is not None
    assert stored.weather.pressure_hpa == 1012.0


def test_chat_log_without_coords_skips_weather(fx, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("no weather fetch without coords")

    monkeypatch.setattr("quietlog.agent.weather_service.fetch_current_weather", boom)
    result, _ = _run(
        fx,
        "migraine since lunch",
        [_action("log_attack", {"transcript": "migraine since lunch"}),
         _reply("Logged.")],
    )
    stored = db_mod.get_entry(fx["conn"], result.actions[0].entry_id)
    assert stored is not None and stored.weather is None


def test_log_action_carries_followups_to_client(fx):
    result, _ = _run(
        fx,
        "migraine since lunch",
        [_action("log_attack", {"transcript": "migraine since lunch"}),
         _reply("Logged.")],
    )
    followups = result.actions[0].followups
    assert [f["id"] for f in followups] == ["duration", "start"]


# --- tools through the loop --------------------------------------------------


def test_log_attack_turn_saves_and_reports(fx):
    result, _ = _run(
        fx,
        "migraine since lunch, pretty bad",
        [_action("log_attack", {"transcript": "migraine since lunch, pretty bad"}),
         _reply("Logged.")],
    )
    assert result.reply == "Logged."
    assert len(result.actions) == 1
    call = result.actions[0]
    assert (call.tool, call.ok, call.label) == ("log_attack", True, "Logged attack")
    assert call.entry_id
    stored = db_mod.get_entry(fx["conn"], call.entry_id)
    assert stored is not None
    assert stored.transcript == "migraine since lunch, pretty bad"
    assert stored.status == EntryStatus.UNCONFIRMED


def test_log_checkin_turn(fx):
    result, _ = _run(fx, "no headache today",
                      [_action("log_checkin"), _reply("Checked in.")])
    assert result.actions[0].label == "Checked in: no headache"
    entries = db_mod.list_entries(fx["conn"])
    assert len(entries) == 1
    assert entries[0].entry_type == EntryType.NO_ATTACK
    assert entries[0].status == EntryStatus.CONFIRMED


def test_timer_start_then_end(fx):
    started, _ = _run(fx, "start the timer", [_action("timer_start"), _reply("Started.")])
    assert started.actions[0].label == "Timer started"
    entry_id = started.actions[0].entry_id
    assert db_mod.get_entry(fx["conn"], entry_id).status == EntryStatus.IN_PROGRESS
    ended, _ = _run(fx, "stop", [_action("timer_end"), _reply("Stopped.")])
    assert ended.actions[0].ok is True
    stored = db_mod.get_entry(fx["conn"], entry_id)
    assert stored.status == EntryStatus.NEEDS_PROCESSING
    assert stored.attack is not None and stored.attack.duration_minutes == 1


def test_timer_start_is_idempotent(fx):
    _run(fx, "start", [_action("timer_start"), _reply("Started.")])
    again, _ = _run(fx, "start", [_action("timer_start"), _reply("Again.")])
    assert again.actions[0].label == "Timer already running"
    assert len(db_mod.list_entries(fx["conn"])) == 1


def test_timer_end_without_timer_feeds_error_back(fx):
    result, seen = _run(
        fx, "stop",
        [_action("timer_end"), _reply("No timer is running.")],
    )
    assert result.actions[0].ok is False
    assert "no timer" in result.actions[0].label.lower()
    assert result.reply.startswith("No timer is running.")
    assert "I couldn't complete" in result.reply
    tool_feedback = seen[1]["messages"][-1]["content"]
    assert "no timer is running" in tool_feedback.lower()


def test_empty_transcript_is_rejected_by_tool(fx):
    result, _ = _run(
        fx, "log a headache",
        [_action("log_attack", {"transcript": "  "}),
         _reply("Tell me what happened.")],
    )
    assert result.actions[0].ok is False
    assert db_mod.list_entries(fx["conn"]) == []
    assert result.reply.startswith("Tell me what happened.")
    assert "I couldn't complete" in result.reply


def test_bare_words_still_log_with_followups(fx):
    result, seen = _run(
        fx,
        "I have a headache.",
        [_action("log_attack", {"transcript": "I have a headache."}),
         _reply("Logged.")],
    )
    assert result.actions[0].ok is True
    entry_id = result.actions[0].entry_id
    assert db_mod.get_entry(fx["conn"], entry_id) is not None
    tool_feedback = seen[1]["messages"][-1]["content"]
    assert "followups" in tool_feedback
    # Log-first rule: the agent must act on bare requests, never ask first.
    assert "Log first, ask later" in agent.AGENT_SYSTEM_PROMPT


def test_answer_diary_tool_reads_entries(fx):
    entry = Entry(entry_type=EntryType.ATTACK,
                  created_at=datetime.now(timezone.utc) - timedelta(days=1),
                  transcript="migraine since lunch",
                  status=EntryStatus.CONFIRMED,
                  attack=AttackDetails(pain_0_10=6))
    db_mod.save_entry(fx["conn"], entry)
    result, _ = _run(
        fx,
        "how many migraines?",
        [_action("answer_diary", {"question": "how many migraines?"}),
         "One migraine, yesterday.",
         _reply("You had one.")],
    )
    assert result.actions[0].ok is True
    assert result.reply == "You had one."


# --- chat follow-ups -----------------------------------------------------------


def test_log_result_lists_followups(fx):
    _, seen = _run(
        fx,
        "migraine since lunch",
        [_action("log_attack", {"transcript": "migraine since lunch"}),
         _reply("Logged. How bad, 0-10?")],
    )
    tool_feedback = seen[1]["messages"][-1]["content"]
    # Fake extraction fills pain only; duration and start stay gaps.
    assert "followups" in tool_feedback
    assert "How long did it last?" in tool_feedback
    assert "When did it start?" in tool_feedback


def test_answer_followups_round_refines_entry(fx):
    entry = Entry(entry_type=EntryType.ATTACK,
                  created_at=datetime.now(timezone.utc),
                  transcript="migraine since lunch",
                  status=EntryStatus.UNCONFIRMED,
                  attack=AttackDetails())
    db_mod.save_entry(fx["conn"], entry)
    result, _ = _run(
        fx,
        "7 out of 10",
        [_action("answer_followups", {
            "entry_id": entry.id,
            "answers": [{"question": "How bad was the pain, 0-10?",
                         "answer": "7 out of 10"}],
        }),
         _reply("Saved.")],
    )
    assert result.actions[0].label == "Answers saved"
    stored = db_mod.get_entry(fx["conn"], entry.id)
    assert stored is not None
    assert "A: 7 out of 10" in (stored.transcript or "")
    assert stored.status == EntryStatus.UNCONFIRMED


def test_answer_followups_missing_entry_fails_soft(fx):
    result, _ = _run(
        fx,
        "7",
        [_action("answer_followups", {
            "entry_id": "nope",
            "answers": [{"question": "Q?", "answer": "7"}],
        }),
         _reply("Lost track of that note.")],
    )
    assert result.actions[0].ok is False


# --- summary / narrative / pdf tools -------------------------------------------


def _saved_attack(fx, **kwargs):
    entry = Entry(entry_type=EntryType.ATTACK,
                  created_at=datetime.now(timezone.utc) - timedelta(days=1),
                  transcript="migraine since lunch",
                  status=EntryStatus.CONFIRMED,
                  attack=AttackDetails(pain_0_10=6))
    for key, value in kwargs.items():
        setattr(entry, key, value)
    db_mod.save_entry(fx["conn"], entry)
    return entry


def test_get_summary_turn(fx):
    _saved_attack(fx)
    result, seen = _run(
        fx, "summarize my month",
        [_action("get_summary", {"window_days": 30}), _reply("One attack.")],
    )
    assert result.actions[0].label == "30-day summary"
    assert "Attack entries: 1" in seen[1]["messages"][-1]["content"]


def test_get_summary_bad_window(fx):
    result, _ = _run(
        fx, "summarize",
        [_action("get_summary", {"window_days": 45}), _reply("Which window?")],
    )
    assert result.actions[0].ok is False


def test_draft_narrative_turn(fx):
    _saved_attack(fx)
    result, _ = _run(
        fx,
        "draft my doctor summary",
        [_action("draft_narrative", {"window_days": 30}),
         "One attack this month. Discuss these patterns with your doctor.",
         _reply("Drafted.")],
    )
    assert result.actions[0].label == "Narrative drafted"


def test_save_pdf_turn_carries_window(fx):
    _saved_attack(fx)
    result, _ = _run(
        fx, "save my report",
        [_action("save_pdf", {"window_days": 30}), _reply("Opening it now.")],
    )
    call = result.actions[0]
    assert call.ok is True
    assert "PDF" in call.label
    assert call.data == {"window_days": 30}


def test_end_state_reports_timer(fx):
    quiet, _ = _run(fx, "hi", [_reply("Hi.")])
    assert quiet.state == {"timer_running": False}
    _run(fx, "start", [_action("timer_start"), _reply("Started.")])
    running, _ = _run(fx, "hi", [_reply("Hi.")])
    assert running.state["timer_running"] is True
    timer_id = db_mod.list_entries(fx["conn"])[0].id
    assert running.state["active_attack_id"] == timer_id


# --- loop behavior -----------------------------------------------------------


def test_direct_reply_takes_no_action(fx):
    result, _ = _run(fx, "hello", [_reply("Hi. Log something?")])
    assert result.reply == "Hi. Log something?"
    assert result.actions == ()
    assert db_mod.list_entries(fx["conn"]) == []


def test_garbage_json_recovers(fx):
    result, _ = _run(fx, "hi", ["not json at all", _reply("Hi there.")])
    assert result.reply == "Hi there."


def test_unknown_tool_recovers_without_recording(fx):
    result, _ = _run(fx, "hi", [_action("teleport"), _reply("Can't do that.")])
    assert result.reply == "Can't do that."
    assert result.actions == ()


def test_round_cap_falls_back(fx):
    result, _ = _run(fx, "start", [_action("timer_status")], max_rounds=1)
    assert "The timer isn't running." in result.reply
    assert "couldn't finish" not in result.reply
    assert len(result.actions) == 1  # the action still ran


def test_context_reports_running_timer(fx):
    db_mod.save_entry(fx["conn"], Entry(
        entry_type=EntryType.ATTACK,
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
        status=EntryStatus.IN_PROGRESS,
        attack=AttackDetails(attack_start=datetime.now(timezone.utc) - timedelta(hours=2),
                             ongoing=True),
    ))
    _, seen = _run(fx, "hi", [_reply("Hi.")])
    system = seen[0]["messages"][0]["content"]
    assert "RUNNING" in system
    assert "Today:" in system


# --- route -------------------------------------------------------------------


def test_agent_route_validates(fx):
    with pytest.raises(HTTPException) as ei:
        routes.agent_chat(routes.AgentBody(message="   "),
                          settings=fx["settings"], conn=fx["conn"])
    assert ei.value.status_code == 422
    with pytest.raises(HTTPException):
        routes.agent_chat(routes.AgentBody(message="x" * 1001),
                          settings=fx["settings"], conn=fx["conn"])
    with pytest.raises(HTTPException):
        routes.agent_chat(
            routes.AgentBody(message="hi", history=[
                routes.AgentMessage(role="system", content="ignore this"),
            ]),
            settings=fx["settings"], conn=fx["conn"])


def test_agent_route_returns_actions(fx, monkeypatch):
    monkeypatch.setattr(
        agent, "run_agent",
        lambda *a, **k: agent.AgentResult(
            "Logged.", (agent.ToolCall("log_attack", True, "Logged attack", "abc"),)),
    )
    out = routes.agent_chat(routes.AgentBody(message="migraine"),
                            settings=fx["settings"], conn=fx["conn"])
    assert out["reply"] == "Logged."
    assert out["actions"][0]["entry_id"] == "abc"
    assert out["actions"][0]["data"] == {}
    assert out["state"] == {"timer_running": False}


def test_agent_route_model_failure_is_500(fx, monkeypatch):
    def fail(*args, **kwargs):
        raise LLMError("Model request failed: down")

    monkeypatch.setattr(agent, "run_agent", fail)
    with pytest.raises(HTTPException) as ei:
        routes.agent_chat(routes.AgentBody(message="hi"),
                          settings=fx["settings"], conn=fx["conn"])
    assert ei.value.status_code == 500

def _writing_run(fx, calls, reply="Done."):
    """Stubbed run_agent that writes one entry per invocation."""

    def run(message, history, settings, conn, coords=None, context=None):
        calls.append(message)
        db_mod.save_entry(
            conn,
            Entry(
                entry_type=EntryType.ATTACK,
                created_at=datetime.now(timezone.utc),
                transcript=message,
                status=EntryStatus.CONFIRMED,
            ),
        )
        return agent.AgentResult(reply, (), False, {"timer_running": False})

    return run


def test_agent_retry_replays_without_rerunning(fx, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(agent, "run_agent", _writing_run(fx, calls))
    body = routes.AgentBody(message="log it", request_id="r-retry")
    first = routes.agent_chat(body, settings=fx["settings"], conn=fx["conn"])
    assert first["replayed"] is False
    second = routes.agent_chat(body, settings=fx["settings"], conn=fx["conn"])
    assert second["replayed"] is True
    assert second["reply"] == first["reply"]
    assert calls == ["log it"]
    assert db_mod.count_entries(fx["conn"]) == 1


def test_agent_same_id_different_input_is_422(fx, monkeypatch):
    monkeypatch.setattr(agent, "run_agent", _writing_run(fx, []))
    routes.agent_chat(
        routes.AgentBody(message="log it", request_id="r-clash"),
        settings=fx["settings"], conn=fx["conn"])
    with pytest.raises(HTTPException) as ei:
        routes.agent_chat(
            routes.AgentBody(message="something else", request_id="r-clash"),
            settings=fx["settings"], conn=fx["conn"])
    assert ei.value.status_code == 422


def test_agent_pending_same_input_is_409(fx, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(agent, "run_agent", _writing_run(fx, calls))
    body = routes.AgentBody(message="log it", request_id="r-overlap")
    input_hash = routes._agent_input_hash("log it", [])
    db_mod.claim_turn(fx["conn"], "r-overlap", input_hash)
    with pytest.raises(HTTPException) as ei:
        routes.agent_chat(body, settings=fx["settings"], conn=fx["conn"])
    assert ei.value.status_code == 409
    assert calls == []
    poll = routes.agent_turn_status("r-overlap", conn=fx["conn"])
    assert poll["status"] == "pending"
    assert "response" not in poll


def test_agent_failed_turn_retries(fx, monkeypatch):
    attempts: list[str] = []

    def flaky(message, history, settings, conn, coords=None, context=None):
        attempts.append(message)
        if len(attempts) == 1:
            raise LLMError("Model request failed: down")
        return agent.AgentResult("Recovered.", (), False, {"timer_running": False})

    monkeypatch.setattr(agent, "run_agent", flaky)
    body = routes.AgentBody(message="hi", request_id="r-flaky")
    with pytest.raises(HTTPException) as ei:
        routes.agent_chat(body, settings=fx["settings"], conn=fx["conn"])
    assert ei.value.status_code == 500
    out = routes.agent_chat(body, settings=fx["settings"], conn=fx["conn"])
    assert out["reply"] == "Recovered."
    assert out["replayed"] is False
    assert attempts == ["hi", "hi"]


def test_agent_expired_turn_never_reruns(fx, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(agent, "run_agent", _writing_run(fx, calls))
    conn = fx["conn"]
    conn.execute(
        "INSERT INTO agent_turns (request_id, input_hash, status, response_json,"
        " created_at, updated_at) VALUES (?, ?, 'pending', NULL, ?, ?)",
        ("r-stale", routes._agent_input_hash("log it", []),
         "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00"),
    )
    conn.commit()
    with pytest.raises(HTTPException) as ei:
        routes.agent_chat(
            routes.AgentBody(message="log it", request_id="r-stale"),
            settings=fx["settings"], conn=conn)
    assert ei.value.status_code == 409
    assert "History" in ei.value.detail
    assert calls == []
    assert db_mod.count_entries(conn) == 0
    assert db_mod.get_turn(conn, "r-stale")["status"] == "expired"


def test_agent_legacy_no_id_runs_unclaimed(fx, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(agent, "run_agent", _writing_run(fx, calls))
    out = routes.agent_chat(routes.AgentBody(message="hi"),
                            settings=fx["settings"], conn=fx["conn"])
    assert out["replayed"] is False
    assert db_mod.get_turn(fx["conn"], "anything") is None


def test_unknown_arg_rejected_before_writing(fx):
    result, _ = _run(
        fx, "log it",
        [_action("log_attack", {"transcript": "headache", "bogus": 1}),
         _reply("Fix it.")],
    )
    assert result.actions[0].ok is False
    assert db_mod.list_entries(fx["conn"]) == []


def test_wrong_types_rejected(fx):
    for tool, args in [
        ("get_summary", {"window_days": "30"}),
        ("get_summary", {"window_days": 30.0}),
        ("get_summary", {"window_days": True}),
        ("log_attack", {"transcript": "x", "new_attack": 1}),
        ("log_attack", {"transcript": "x", "new_attack": "true"}),
        ("answer_diary", {"question": "q", "window_days": "90"}),
        ("answer_diary", {"question": "q?" * 500}),
        ("log_checkin", {"today": True}),
        ("timer_end", {"entry_id": "x"}),
    ]:
        result, _ = _run(fx, "go", [_action(tool, args), _reply("Fix it.")])
        assert result.actions[0].ok is False, (tool, args)
    assert db_mod.list_entries(fx["conn"]) == []


def test_blank_and_malformed_ids_rejected(fx):
    _run(fx, "start", [_action("timer_start"), _reply("Started.")])
    for args in [
        {"entry_id": "  ", "transcript": "x"},
        {"entry_id": "not-an-id", "transcript": "x"},
        {"entry_id": "zzzz", "transcript": "x"},
    ]:
        result, _ = _run(
            fx, "add", [_action("add_note", args), _reply("Fix it.")])
        assert result.actions[0].ok is False, args
    entry = db_mod.list_entries(fx["conn"])[0]
    assert "x" not in (entry.transcript or "")


def test_distinct_pdf_windows_both_run(fx):
    result, _ = _run(
        fx, "two reports",
        [_action("save_pdf", {"window_days": 30}),
         _action("save_pdf", {"window_days": 60}),
         _reply("Both ready.")],
    )
    assert [a.ok for a in result.actions] == [True, True]


def test_identical_add_note_after_log_is_blocked(fx):
    _run(fx, "start", [_action("timer_start"), _reply("Started.")])
    entry_id = db_mod.list_entries(fx["conn"])[0].id
    result, _ = _run(
        fx, "log it",
        [_action("log_attack", {"transcript": "pounding"}),
         _action("add_note", {"entry_id": entry_id, "transcript": "pounding"}),
         _reply("Saved.")],
    )
    assert len(result.actions) == 1
    stored = db_mod.get_entry(fx["conn"], entry_id)
    assert (stored.transcript or "").count("pounding") == 1


def test_new_facts_after_log_still_pass(fx):
    _run(fx, "start", [_action("timer_start"), _reply("Started.")])
    entry_id = db_mod.list_entries(fx["conn"])[0].id
    result, _ = _run(
        fx, "log it",
        [_action("log_attack", {"transcript": "pounding"}),
         _action("add_note", {"entry_id": entry_id, "transcript": "took ibuprofen"}),
         _reply("Saved.")],
    )
    assert [a.ok for a in result.actions] == [True, True]
    stored = db_mod.get_entry(fx["conn"], entry_id)
    assert "ibuprofen" in (stored.transcript or "")


def test_agent_turn_falls_back_when_json_object_rejected(fx):
    def picky_post(url, headers=None, payload=None, timeout=None):
        if payload.get("response_format"):
            return FakeResponse(400, {"error": "unsupported"})
        content = _reply("Fine.")
        return FakeResponse(200, {"choices": [{"message": {"content": content}}]})

    result = agent.run_agent(
        "hi", [], fx["settings"], fx["conn"],
        http_post=picky_post, process_entry_fn=_fake_process)
    assert result.reply == "Fine."


def test_find_entries_filters_and_empty(fx):
    first = _saved_attack(fx, entry_date=date(2026, 9, 5))
    _saved_attack(fx, entry_date=date(2026, 9, 6))
    result, _ = _run(
        fx, "find it",
        [_action("find_entries", {"date_from": "2026-09-05", "date_to": "2026-09-05"}),
         _reply("Found.")],
    )
    assert result.actions[0].ok is True
    assert [m["entry_id"] for m in result.actions[0].data["matches"]] == [first.id]
    result, _ = _run(
        fx, "find nothing",
        [_action("find_entries", {"contains": "zzz-no-such-words"}), _reply("None.")],
    )
    assert result.actions[0].label == "No matching entries"
    assert result.actions[0].data["total"] == 0


def test_find_entries_bad_filters(fx):
    for args in [
        {"date_from": "2026-09-06", "date_to": "2026-09-05"},
        {"date_from": "2026-13-01"},
        {"limit": 99},
        {"limit": 0},
    ]:
        result, _ = _run(fx, "find", [_action("find_entries", args), _reply("Fix.")])
        assert result.actions[0].ok is False, args


def test_get_entry_round_trip(fx):
    entry = _saved_attack(fx)
    result, _ = _run(
        fx, "which one",
        [_action("get_entry", {"entry_id": entry.id}), _reply("This one.")],
    )
    assert result.actions[0].ok is True
    assert result.actions[0].entry_id == entry.id
    assert result.actions[0].data["receipt"]["entry_id"] == entry.id
    unknown = "0" * 32
    result, _ = _run(
        fx, "and this one?",
        [_action("get_entry", {"entry_id": unknown}), _reply("Lost.")],
    )
    assert result.actions[0].ok is False


def test_context_ids_validated_before_prompt(fx):
    entry = _saved_attack(fx)
    _, seen = _run(
        fx, "that one",
        [_reply("It.")],
        context={"discussed_entry_id": entry.id,
                 "pending_entry_id": "nope",
                 "active_attack_id": None},
    )
    system = seen[0]["messages"][0]["content"]
    assert f"Discussed entry: {entry.id}" in system
    assert "nope" not in system
    assert "Entry awaiting" not in system


def test_context_pending_requires_unconfirmed(fx):
    entry = _saved_attack(fx)  # CONFIRMED
    _, seen = _run(fx, "hi", [_reply("Hi.")],
                   context={"pending_entry_id": entry.id})
    assert "Entry awaiting" not in seen[0]["messages"][0]["content"]


def test_turn_state_tracks_discussed_and_pending(fx):
    result, _ = _run(
        fx, "headache",
        [_action("log_attack", {"transcript": "pounding"}), _reply("Saved.")],
    )
    entry_id = result.actions[0].entry_id
    assert result.state["discussed_entry_id"] == entry_id
    assert result.state["pending_entry_id"] == entry_id
    assert result.state["timer_running"] is False
    result2, seen2 = _run(
        fx, "which entry",
        [_action("get_entry", {"entry_id": entry_id}), _reply("This.")],
        context=result.state,
    )
    assert result2.state["discussed_entry_id"] == entry_id
    assert entry_id in seen2[0]["messages"][0]["content"]


def test_delimiters_wrap_history_and_tool_results(fx):
    script = [_action("timer_status"), _reply("None running.")]
    result = agent.run_agent(
        "status?", [{"role": "user", "content": "old note"}], fx["settings"], fx["conn"],
        http_post=_scripted_post(script, seen := []),
        process_entry_fn=_fake_process)
    first = seen[0]["messages"]
    assert first[1]["content"].startswith("--- Earlier user message")
    assert "old note" in first[1]["content"]
    assert first[0]["content"].startswith("You are Quietlog's assistant")
    fed_back = seen[1]["messages"][-1]["content"]
    assert fed_back.startswith("--- Tool result")
    assert result.reply == "None running."


def test_user_edits_survive_reextraction(fx):
    result, _ = _run(
        fx, "headache",
        [_action("log_attack", {"transcript": "pounding"}), _reply("S.")])
    entry_id = result.actions[0].entry_id
    entry = db_mod.get_entry(fx["conn"], entry_id)
    assert entry.attack.pain_0_10 == 7
    entry.attack = routes._apply_attack_patch(
        entry.attack, {"pain_0_10": 9}, entry.created_at)
    db_mod.save_entry(fx["conn"], entry)
    assert entry.attack.edited_fields == ["pain_0_10"]

    def extract2(transcript, ref):
        return AttackDetails(
            pain_0_10=2, notes="fresh notes",
            evidence={"pain_0_10": "stale quote", "notes": "fresh quote"})

    stored = process_entry(
        fx["conn"], db_mod.get_entry(fx["conn"], entry_id),
        fx["settings"], extract_fn=extract2)
    assert stored.attack.pain_0_10 == 9
    assert stored.attack.notes == "fresh notes"
    assert "pain_0_10" not in stored.attack.evidence
    assert stored.attack.evidence.get("notes") == "fresh quote"
    assert "pain_0_10" in stored.attack.edited_fields


def test_timer_stamp_survives_reextraction(fx):
    result, _ = _run(fx, "start", [_action("timer_start"), _reply("S.")])
    entry_id = result.actions[0].entry_id
    entry = db_mod.get_entry(fx["conn"], entry_id)
    stamped = entry.attack.attack_start
    entry.transcript = "pounding headache"
    db_mod.save_entry(fx["conn"], entry)

    def extract2(transcript, ref):
        return AttackDetails(
            attack_start=datetime(2000, 1, 1, tzinfo=timezone.utc), pain_0_10=4)

    stored = process_entry(
        fx["conn"], db_mod.get_entry(fx["conn"], entry_id),
        fx["settings"], extract_fn=extract2)
    assert stored.attack.attack_start == stamped
    assert stored.attack.attack_start_precision.value == "exact"
    assert stored.attack.pain_0_10 == 4


def test_empty_reads_report_no_records(fx):
    result, _ = _run(
        fx, "summarize",
        [_action("get_summary", {"window_days": 30}), _reply("Nothing.")])
    assert result.actions[0].status == "no_records"
    result, _ = _run(
        fx, "ask away",
        [_action("answer_diary", {"question": "how many?"}), _reply("None.")])
    assert result.actions[0].status == "no_records"
    assert result.actions[0].data["entries_consulted"] == 0
    result, _ = _run(
        fx, "narrate",
        [_action("draft_narrative", {"window_days": 30}), _reply("None.")])
    assert result.actions[0].status == "no_records"


def test_saved_but_unprocessed_is_honest(fx, monkeypatch):
    def broken(conn, entry, settings, coords=None):
        raise RuntimeError("extract exploded")

    result, _ = _run(
        fx, "log it",
        [_action("log_attack", {"transcript": "pounding"}), _reply("Saved.")],
        process_entry_fn=broken)
    assert result.actions[0].ok is True
    assert result.actions[0].status == "saved_unprocessed"
    assert "processing needs retry" in result.actions[0].label
    assert "still needs processing" in result.reply
    assert db_mod.list_entries(fx["conn"])[0].status == EntryStatus.NEEDS_PROCESSING


def test_model_error_keeps_completed_read(fx, monkeypatch):
    calls = {"n": 0}
    scripted = _scripted_post(
        [_action("timer_status"), _reply("unreached")], seen := [])

    def flaky_post(url, headers=None, payload=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise LLMError("Model request failed: down")
        return scripted(url, headers, payload, timeout)

    result = agent.run_agent(
        "status?", [], fx["settings"], fx["conn"],
        http_post=flaky_post, process_entry_fn=_fake_process)
    assert "The timer isn't running." in result.reply
    assert "couldn't finish" not in result.reply
    assert result.actions[-1].status == "model_error"
    assert result.actions[-1].ok is False


def test_tool_error_label_has_no_exception_name(fx):
    result, _ = _run(
        fx, "summarize",
        [_action("get_summary", {"window_days": 45}), _reply("Which?")])
    assert result.actions[0].status == "tool_error"
    assert "ValueError" not in result.actions[0].label


def test_agent_response_carries_statuses(fx, monkeypatch):
    monkeypatch.setattr(
        agent, "run_agent",
        lambda *a, **k: agent.AgentResult(
            "Done.", (agent.ToolCall("log_attack", True, "Logged attack", "abc",
                                     status="saved_unprocessed"),)))
    out = routes.agent_chat(routes.AgentBody(message="hi"),
                            settings=fx["settings"], conn=fx["conn"])
    assert out["actions"][0]["status"] == "saved_unprocessed"
    assert out["status"] == "ok"
    monkeypatch.setattr(
        agent, "run_agent",
        lambda *a, **k: agent.AgentResult(
            "Partial.", (agent.ToolCall("assistant", False, "died",
                                        status="model_error"),)))
    out = routes.agent_chat(routes.AgentBody(message="hi"),
                            settings=fx["settings"], conn=fx["conn"])
    assert out["status"] == "model_error"


def test_agent_500_hides_model_details_but_keeps_setup_errors(fx, monkeypatch):
    def fail(*args, **kwargs):
        raise LLMError("Model request failed: secret-internal-xyz")

    monkeypatch.setattr(agent, "run_agent", fail)
    with pytest.raises(HTTPException) as ei:
        routes.agent_chat(routes.AgentBody(message="hi"),
                          settings=fx["settings"], conn=fx["conn"])
    assert ei.value.status_code == 500
    assert "secret-internal-xyz" not in ei.value.detail
    assert "unavailable" in ei.value.detail

    from quietlog.llm_service import ModelConfigError

    def misconfigured(*args, **kwargs):
        raise ModelConfigError("No API key: set QUIETLOG_MODEL_API_KEY in .env.")

    monkeypatch.setattr(agent, "run_agent", misconfigured)
    with pytest.raises(HTTPException) as ei:
        routes.agent_chat(routes.AgentBody(message="hi"),
                          settings=fx["settings"], conn=fx["conn"])
    assert "No API key" in ei.value.detail


def test_set_entry_date_moves_and_shifts_start(fx):
    entry = _saved_attack(fx, entry_date=date(2026, 11, 3))
    entry.attack.attack_start = datetime(2026, 11, 3, 8, 30, tzinfo=timezone.utc)
    db_mod.save_entry(fx["conn"], entry)
    old_start = entry.attack.attack_start
    result, _ = _run(
        fx, "actually that was yesterday",
        [_action("set_entry_date", {"entry_id": entry.id, "date": "2026-11-02"}),
         _reply("Moved.")])
    assert result.actions[0].ok is True
    stored = db_mod.get_entry(fx["conn"], entry.id)
    assert stored.entry_date == date(2026, 11, 2)
    assert (stored.attack.attack_start.year, stored.attack.attack_start.month,
            stored.attack.attack_start.day) == (2026, 11, 2)
    assert (stored.attack.attack_start.hour, stored.attack.attack_start.minute) == (
        old_start.hour, old_start.minute)
    assert "attack_start" in stored.attack.edited_fields


def test_set_entry_date_rejects_bad_moves(fx):
    entry = _saved_attack(fx)
    checkin = Entry(entry_type=EntryType.NO_ATTACK,
                    created_at=datetime.now(timezone.utc),
                    status=EntryStatus.CONFIRMED)
    db_mod.save_entry(fx["conn"], checkin)
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    for args in [
        {"entry_id": entry.id, "date": tomorrow},
        {"entry_id": entry.id, "date": "2026-13-01"},
        {"entry_id": entry.id, "date": "yesterday"},
        {"entry_id": "0" * 32, "date": "2026-11-01"},
        {"entry_id": checkin.id, "date": "2026-11-01"},
    ]:
        result, _ = _run(
            fx, f"move {args['date']}",
            [_action("set_entry_date", args), _reply("No.")])
        assert result.actions[0].ok is False, args
    assert db_mod.get_entry(fx["conn"], entry.id).entry_date == entry.entry_date


def test_conflicting_date_moves_second_blocked(fx):
    entry = _saved_attack(fx, entry_date=date(2026, 11, 3))
    result, _ = _run(
        fx, "move it",
        [_action("set_entry_date", {"entry_id": entry.id, "date": "2026-11-02"}),
         _action("set_entry_date", {"entry_id": entry.id, "date": "2026-11-01"}),
         _reply("Moved once.")])
    assert len(result.actions) == 1
    assert db_mod.get_entry(fx["conn"], entry.id).entry_date == date(2026, 11, 2)


def test_never_mind_before_action_writes_nothing(fx):
    result, _ = _run(fx, "never mind", [_reply("No problem, nothing changed.")])
    assert result.actions == ()
    assert db_mod.list_entries(fx["conn"]) == []


def test_never_mind_after_write_keeps_entry(fx):
    first, _ = _run(
        fx, "headache",
        [_action("log_attack", {"transcript": "pounding"}), _reply("Saved.")])
    entry_id = first.actions[0].entry_id
    result, _ = _run(
        fx, "never mind",
        [_action("get_entry", {"entry_id": entry_id}), _reply("Kept.")])
    assert result.actions[0].ok is True
    assert db_mod.count_entries(fx["conn"]) == 1
    assert db_mod.get_entry(fx["conn"], entry_id) is not None


def test_bare_followup_details_join_running_attack(fx):
    _run(fx, "start", [_action("timer_start"), _reply("S.")])
    result, _ = _run(
        fx, "Also, I took ibuprofen at noon.",
        [_action("add_note", {"transcript": "Took ibuprofen at noon."}),
         _reply("Added.")])
    assert result.actions[0].ok is True
    rows = db_mod.list_entries(fx["conn"])
    assert len(rows) == 1
    assert "ibuprofen" in (rows[0].transcript or "")


def test_its_over_without_timer_is_honest(fx):
    result, _ = _run(
        fx, "It's over.",
        [_action("timer_end"), _reply("Nothing to stop.")])
    assert result.actions[0].ok is False
    assert result.actions[0].status == "tool_error"
    assert "I couldn't complete" in result.reply


def test_recovery_rules_present_in_prompt():
    prompt = agent.AGENT_SYSTEM_PROMPT
    assert '"Never mind" never deletes anything' in prompt
    assert "set_entry_date" in prompt
    assert "Never claim a move happened without calling it" in prompt
    assert "Never silently pick the newest" in prompt
    assert "never hold a save waiting for details" in prompt


def test_mid_turn_missing_key_completes_without_rerun_risk(fx, monkeypatch):
    from quietlog.llm_service import MissingApiKeyError

    calls = {"n": 0}
    action_json = _action("log_attack", {"transcript": "pounding"})

    def key_then_gone(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return ({"choices": [{"message": {"content": action_json}}]}, False)
        raise MissingApiKeyError("No API key: set QUIETLOG_MODEL_API_KEY in .env.")

    monkeypatch.setattr(agent.llm_service, "_chat", key_then_gone)
    result = agent.run_agent(
        "log it", [], fx["settings"], fx["conn"], process_entry_fn=_fake_process)
    assert len(db_mod.list_entries(fx["conn"])) == 1
    assert result.actions[0].tool == "log_attack"
    assert result.actions[-1].status == "model_error"
    assert "API key" in result.actions[-1].label


def test_missing_key_before_any_action_still_raises(fx, monkeypatch):
    from quietlog.llm_service import MissingApiKeyError

    def no_key(**kwargs):
        raise MissingApiKeyError("No API key: set QUIETLOG_MODEL_API_KEY in .env.")

    monkeypatch.setattr(agent.llm_service, "_chat", no_key)
    with pytest.raises(MissingApiKeyError):
        agent.run_agent("log it", [], fx["settings"], fx["conn"],
                        process_entry_fn=_fake_process)
    assert db_mod.list_entries(fx["conn"]) == []


def test_set_entry_date_preserves_overnight_interval(fx):
    entry = _saved_attack(fx, entry_date=date(2026, 11, 3))
    entry.attack.attack_start = datetime(2026, 11, 3, 23, 0, tzinfo=timezone.utc)
    entry.attack.attack_end = datetime(2026, 11, 4, 1, 0, tzinfo=timezone.utc)
    entry.attack.duration_minutes = 120
    db_mod.save_entry(fx["conn"], entry)
    result, _ = _run(
        fx, "that was actually sunday night",
        [_action("set_entry_date", {"entry_id": entry.id, "date": "2026-11-01"}),
         _reply("Moved.")])
    assert result.actions[0].ok is True
    stored = db_mod.get_entry(fx["conn"], entry.id)
    assert stored.entry_date == date(2026, 11, 1)
    assert (stored.attack.attack_start.day, stored.attack.attack_start.hour) == (1, 23)
    assert (stored.attack.attack_end.day, stored.attack.attack_end.hour) == (2, 1)
    assert stored.attack.duration_minutes == 120


def test_repeated_write_only_saves_once(fx):
    result, _ = _run(fx, 'log headache', [
        _action('log_attack', {'transcript': 'headache'}),
        _action('log_attack', {'transcript': 'headache again'}),
        _reply('Saved.'),
    ])
    assert len(db_mod.list_entries(fx['conn'])) == 1
    assert len(result.actions) == 1


def test_round_limit_reports_completed_action(fx):
    result, _ = _run(fx, 'log headache', [
        _action('log_attack', {'transcript': 'headache'}),
    ], max_rounds=1)
    assert 'Logged attack' in result.reply
    assert "couldn't finish" not in result.reply


def test_model_failure_after_write_preserves_receipt(fx, monkeypatch):
    calls = 0
    def chat(**kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise LLMError('offline')
        return {'choices': [{'message': {'content': _action('log_checkin')}}]}, False
    monkeypatch.setattr(agent.llm_service, '_chat', chat)
    result = agent.run_agent('check in', [], fx['settings'], fx['conn'])
    assert result.actions[0].ok
    assert 'Checked in' in result.reply
    assert len(db_mod.list_entries(fx['conn'])) == 1


def test_processing_exception_keeps_saved_note(fx):
    def broken(*args, **kwargs):
        raise RuntimeError('offline')
    result = agent._tool_log_attack(fx['conn'], fx['settings'],
                                   {'transcript': 'headache'}, broken)
    assert result['ok']
    assert 'retry' in result['label']
    assert db_mod.get_entry(fx['conn'], result['entry_id']).transcript == 'headache'


def test_timer_notes_medication_and_end_are_one_attack(fx):
    from quietlog.processing import process_entry
    from quietlog.models import AcuteMedication
    def process(conn, entry, settings, coords=None):
        return process_entry(conn, entry, settings, extract_fn=lambda text, ref:
                             AttackDetails(pain_0_10=6,
                                           acute_medications=[AcuteMedication(name="ibuprofen")]))
    start = agent._tool_timer_start(fx['conn'], fx['settings'], {}, process)
    first = agent._tool_log_attack(fx['conn'], fx['settings'],
                                  {'transcript': 'Pain is six out of ten'}, process)
    second = agent._tool_add_note(fx['conn'], fx['settings'],
                                  {'transcript': 'I took ibuprofen at noon'}, process)
    assert start['entry_id'] == first['entry_id'] == second['entry_id']
    assert second['data']['receipt']['status'] == 'in_progress'
    assert 'Pain is six' in second['data']['receipt']['transcript']
    assert 'ibuprofen' in second['data']['receipt']['transcript']
    ended = agent._tool_timer_end(fx['conn'], fx['settings'], {}, process)
    assert ended['entry_id'] == start['entry_id']
    assert ended['data']['receipt']['status'] == 'unconfirmed'
    assert ended['data']['receipt']['duration_minutes'] == 1
    assert ended['data']['receipt']['medications'] == ['ibuprofen']
    assert len(db_mod.list_entries(fx['conn'])) == 1


def test_add_note_requires_target_without_timer(fx):
    with pytest.raises(ValueError, match='Which attack'):
        agent._tool_add_note(fx['conn'], fx['settings'],
                             {'transcript': 'also nausea'}, _fake_process)
    assert db_mod.list_entries(fx['conn']) == []


def test_explicit_separate_attack_does_not_change_running_one(fx):
    start = agent._tool_timer_start(fx['conn'], fx['settings'], {}, _fake_process)
    logged = agent._tool_log_attack(fx['conn'], fx['settings'],
                                   {'transcript': 'Yesterday I had a headache', 'new_attack': True},
                                   _fake_process)
    assert logged['entry_id'] != start['entry_id']
    assert db_mod.get_entry(fx['conn'], start['entry_id']).status == EntryStatus.IN_PROGRESS
    assert len(db_mod.list_entries(fx['conn'])) == 2


def test_timer_reuses_note_logged_in_same_turn(fx):
    result, _ = _run(fx, 'log headache and start timing', [
        _action('log_attack', {'transcript': 'headache'}),
        _action('timer_start'), _reply('Saved and timing.'),
    ])
    assert result.actions[0].entry_id == result.actions[1].entry_id
    assert len(db_mod.list_entries(fx['conn'])) == 1
    assert result.state['timer_running']


def test_add_note_after_ending_reuses_entry(fx):
    entry = Entry(entry_type=EntryType.ATTACK, created_at=datetime.now(timezone.utc),
                  transcript='Headache at lunch', status=EntryStatus.UNCONFIRMED,
                  attack=AttackDetails(ongoing=False, duration_minutes=60))
    db_mod.save_entry(fx['conn'], entry)
    result = agent._tool_add_note(fx['conn'], fx['settings'],
                                 {'entry_id': entry.id, 'transcript': 'Also felt nauseous'},
                                 _fake_process)
    assert result['entry_id'] == entry.id
    assert 'Headache at lunch' in result['transcript']
    assert 'Also felt nauseous' in result['transcript']
    assert len(db_mod.list_entries(fx['conn'])) == 1


def test_add_note_failure_still_returns_saved_receipt(fx):
    entry = Entry(entry_type=EntryType.ATTACK, created_at=datetime.now(timezone.utc),
                  transcript='Headache', status=EntryStatus.UNCONFIRMED)
    db_mod.save_entry(fx['conn'], entry)
    def broken(*args, **kwargs):
        raise RuntimeError('offline')
    result = agent._tool_add_note(fx['conn'], fx['settings'],
                                 {'entry_id': entry.id, 'transcript': 'Also nausea'}, broken)
    assert result['ok']
    assert result['data']['receipt']['processing_error']
    assert result['data']['receipt']['status'] == 'needs_processing'
    assert 'Also nausea' in db_mod.get_entry(fx['conn'], entry.id).transcript


def test_add_note_refuses_confirmed_entry(fx):
    """Confirmed history is closed (same rule as the note route): the agent
    must not append, un-confirm, or re-extract it."""
    entry = _saved_attack(fx)
    assert entry.status == EntryStatus.CONFIRMED
    result, _ = _run(
        fx, "add that to the same headache",
        [_action("add_note", {"entry_id": entry.id, "transcript": "also dizzy"}),
         _reply("Done.")])
    assert result.actions[0].ok is False
    stored = db_mod.get_entry(fx["conn"], entry.id)
    assert stored.status == EntryStatus.CONFIRMED
    assert stored.transcript == "migraine since lunch"
    assert stored.attack.pain_0_10 == 6


def _unconfirmed_attack(fx, **attack_kwargs):
    entry = Entry(
        entry_type=EntryType.ATTACK,
        created_at=datetime.now(timezone.utc) - timedelta(days=1),
        transcript="migraine since lunch",
        status=EntryStatus.UNCONFIRMED,
        attack=AttackDetails(**attack_kwargs),
    )
    db_mod.save_entry(fx["conn"], entry)
    return entry


def test_update_entry_corrects_fields(fx):
    entry = _unconfirmed_attack(
        fx,
        pain_0_10=6,
        acute_medications=[AcuteMedication(name="Excedrin", outcome=MedicationOutcome.UNKNOWN)],
    )
    result, _ = _run(
        fx, "actually the pain was 8 and the Excedrin helped",
        [_action("update_entry", {
            "entry_id": entry.id,
            "pain_0_10": 8,
            "duration_minutes": 90,
            "medication": {"name": "excedrin", "outcome": "relief"},
        }), _reply("Updated.")])
    assert result.actions[0].ok is True
    assert result.actions[0].tool == "update_entry"
    stored = db_mod.get_entry(fx["conn"], entry.id)
    assert stored.attack.pain_0_10 == 8
    assert stored.attack.duration_minutes == 90
    assert stored.attack.acute_medications[0].outcome == MedicationOutcome.RELIEF
    assert "pain_0_10" in stored.attack.edited_fields
    assert "duration_minutes" in stored.attack.edited_fields
    assert "acute_medications" in stored.attack.edited_fields
    assert result.actions[0].data["receipt"]["pain"] == 8


def test_update_entry_refuses_confirmed_and_invalid(fx):
    entry = _saved_attack(fx)  # confirmed by default
    result, _ = _run(
        fx, "change the pain to 9",
        [_action("update_entry", {"entry_id": entry.id, "pain_0_10": 9}),
         _reply("Cannot.")])
    assert result.actions[0].ok is False
    assert db_mod.get_entry(fx["conn"], entry.id).attack.pain_0_10 == 6

    open_entry = _unconfirmed_attack(fx, pain_0_10=5)
    for args in [
        {"entry_id": open_entry.id, "pain_0_10": 11},
        {"entry_id": open_entry.id, "pain_0_10": "8"},
        {"entry_id": open_entry.id, "severity_function": 4},
        {"entry_id": open_entry.id, "diagnosis": "migraine"},
        {"entry_id": open_entry.id, "medication": {"name": "Nope", "outcome": "relief"}},
        {"entry_id": open_entry.id, "medication": {"name": "Excedrin", "outcome": "great"}},
    ]:
        result, _ = _run(
            fx, "fix it",
            [_action("update_entry", args), _reply("No.")])
        assert result.actions[0].ok is False, args
    assert db_mod.get_entry(fx["conn"], open_entry.id).attack.pain_0_10 == 5


def test_update_entry_repeat_in_turn_applies_once(fx):
    entry = _unconfirmed_attack(fx, pain_0_10=6)
    args = {"entry_id": entry.id, "pain_0_10": 8}
    result, _ = _run(
        fx, "pain was 8",
        [_action("update_entry", args), _action("update_entry", args),
         _reply("Updated.")])
    assert len(result.actions) == 1
    assert db_mod.get_entry(fx["conn"], entry.id).attack.pain_0_10 == 8


def test_answer_diary_returns_citations(fx):
    entry = _saved_attack(fx)
    result, _ = _run(
        fx, "how many migraines?",
        [_action("answer_diary", {"question": "how many migraines?"}),
         "One migraine, yesterday.",
         _reply("You had one.")])
    assert result.actions[0].ok is True
    citations = result.actions[0].data["citations"]
    assert citations[0]["entry_id"] == entry.id
    assert citations[0]["date"] == entry.entry_date.isoformat()
    assert citations[0]["pain"] == 6


def test_cannot_restart_ended_attack(fx):
    entry = Entry(entry_type=EntryType.ATTACK, created_at=datetime.now(timezone.utc),
                  attack=AttackDetails(ongoing=False, duration_minutes=60))
    db_mod.save_entry(fx['conn'], entry)
    with pytest.raises(ValueError, match='ended'):
        agent._tool_timer_start(fx['conn'], fx['settings'], {'entry_id': entry.id}, _fake_process)
    assert len(db_mod.list_entries(fx['conn'])) == 1
