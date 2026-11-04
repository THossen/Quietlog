"""Agentic-read tests: follow-up answers, diary Q&A, narrative summaries.

All model calls are stubbed. Nothing here needs an API key or the internet.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from quietlog import assist, db as db_mod
from quietlog import routes
from quietlog.models import AcuteMedication, AttackDetails, Entry
from quietlog.settings import Settings
from quietlog.summary import compute_summary
from quietlog.types import EntryStatus, EntryType


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


def _chat_payload(content):
    return {"choices": [{"message": {"content": content}}]}


def _entry(**kwargs):
    base = dict(
        entry_type=EntryType.ATTACK,
        created_at=datetime.now(timezone.utc) - timedelta(days=2),
        transcript="migraine since lunch",
        status=EntryStatus.UNCONFIRMED,
        attack=AttackDetails(pain_0_10=7),
    )
    base.update(kwargs)
    return Entry(**base)


# --- follow-up answers -------------------------------------------------------


def test_answer_appends_and_requeues(fx):
    entry = _entry()
    db_mod.save_entry(fx["conn"], entry)
    out = routes.answer_followups(
        entry.id,
        routes.AnswerBody(answers=[
            routes.AnswerItem(question="Did the Excedrin help?", answer="Yes, within an hour."),
        ]),
        conn=fx["conn"],
    )
    assert out["status"] == "needs_processing"
    assert "Follow-up answers:" in out["transcript"]
    assert "Q: Did the Excedrin help?" in out["transcript"]
    assert "A: Yes, within an hour." in out["transcript"]
    assert "migraine since lunch" in out["transcript"]


def test_answer_rejects_bad_states(fx):
    with pytest.raises(HTTPException) as ei:
        routes.answer_followups("missing", routes.AnswerBody(answers=[]), conn=fx["conn"])
    assert ei.value.status_code == 404

    confirmed = _entry(status=EntryStatus.CONFIRMED)
    db_mod.save_entry(fx["conn"], confirmed)
    with pytest.raises(HTTPException) as ei:
        routes.answer_followups(
            confirmed.id,
            routes.AnswerBody(answers=[routes.AnswerItem(question="Q?", answer="A.")]),
            conn=fx["conn"],
        )
    assert ei.value.status_code == 422

    pending = _entry(status=EntryStatus.NEEDS_PROCESSING)
    db_mod.save_entry(fx["conn"], pending)
    with pytest.raises(HTTPException) as ei:
        routes.answer_followups(
            pending.id,
            routes.AnswerBody(answers=[routes.AnswerItem(question="Q?", answer="A.")]),
            conn=fx["conn"],
        )
    assert ei.value.status_code == 422


def test_answer_validates_items(fx):
    entry = _entry()
    db_mod.save_entry(fx["conn"], entry)
    with pytest.raises(HTTPException) as ei:
        routes.answer_followups(entry.id, routes.AnswerBody(answers=[]), conn=fx["conn"])
    assert ei.value.status_code == 422
    with pytest.raises(HTTPException):
        routes.answer_followups(
            entry.id,
            routes.AnswerBody(answers=[routes.AnswerItem(question="Q?", answer="  ")]),
            conn=fx["conn"],
        )
    many = [routes.AnswerItem(question=f"Q{i}?", answer="A.") for i in range(4)]
    with pytest.raises(HTTPException):
        routes.answer_followups(entry.id, routes.AnswerBody(answers=many), conn=fx["conn"])


# --- digests -----------------------------------------------------------------


def test_entries_digest_lines_and_caps():
    attack = _entry(
        attack=AttackDetails(
            pain_0_10=8,
            acute_medications=[AcuteMedication(name="Excedrin")],
            reported_triggers=["poor sleep"],
        ),
        transcript="x" * 500,
    )
    checkin = Entry(entry_type=EntryType.NO_ATTACK,
                    created_at=datetime.now(timezone.utc) - timedelta(days=1))
    digest = assist.entries_digest([attack, checkin])
    assert "(attack, awaiting review)" in digest
    assert "pain 8/10" in digest
    assert "Excedrin (outcome unknown)" in digest
    assert "triggers: poor sleep" in digest
    assert "(no headache check-in)" in digest
    assert "x" * 401 not in digest  # transcript truncated
    many = [_entry() for _ in range(50)]
    assert len(assist.entries_digest(many).splitlines()) == assist.MAX_DIGEST_ENTRIES


def test_summary_digest_carries_numbers(fx):
    db_mod.save_entry(fx["conn"], _entry())
    entries = db_mod.list_entries(fx["conn"])
    summary = compute_summary(
        entries,
        entries[0].entry_date,
        entries[0].entry_date,
        generated_at=datetime.now(timezone.utc),
    )
    digest = assist.summary_digest(summary)
    assert "Attack entries: 1" in digest
    assert "1 awaiting review" in digest
    assert "Window:" in digest


# --- ask ---------------------------------------------------------------------


def test_ask_empty_window_answers_without_model(fx):
    out = routes.ask(routes.AskBody(question="How many migraines?"), settings=fx["settings"],
                     conn=fx["conn"])
    assert out["entries_consulted"] == 0
    assert "no entries" in out["answer"].lower()


def test_ask_diary_passes_digest_to_model(fx, monkeypatch):
    monkeypatch.setenv("QUIETLOG_MODEL_API_KEY", "test-key")
    seen = {}

    def fake_post(url, headers=None, payload=None, timeout=None):
        seen["messages"] = payload["messages"]
        return FakeResponse(200, _chat_payload("Two attacks, on Nov 1 and Nov 2."))

    result = assist.ask_diary("How many?", [_entry()], fx["settings"], http_post=fake_post)
    assert result.answer == "Two attacks, on Nov 1 and Nov 2."
    assert result.entries_consulted == 1
    user_text = seen["messages"][1]["content"]
    assert "How many?" in user_text
    assert "migraine since lunch" in user_text


def test_ask_validates(fx):
    db_mod.save_entry(fx["conn"], _entry())
    with pytest.raises(HTTPException) as ei:
        routes.ask(routes.AskBody(question="   "), settings=fx["settings"], conn=fx["conn"])
    assert ei.value.status_code == 422
    with pytest.raises(HTTPException):
        routes.ask(routes.AskBody(question="Q?", window_days=45), settings=fx["settings"],
                   conn=fx["conn"])


def test_ask_model_failure_is_500(fx, monkeypatch):
    db_mod.save_entry(fx["conn"], _entry())

    def fail(url, headers=None, payload=None, timeout=None):
        return FakeResponse(500, {})

    monkeypatch.setattr("quietlog.llm_service._default_http_post", fail)
    monkeypatch.setenv("QUIETLOG_MODEL_API_KEY", "test-key")
    try:
        with pytest.raises(HTTPException) as ei:
            routes.ask(routes.AskBody(question="How many?"), settings=fx["settings"],
                       conn=fx["conn"])
        assert ei.value.status_code == 500
    finally:
        monkeypatch.delenv("QUIETLOG_MODEL_API_KEY", raising=False)


# --- narrative ---------------------------------------------------------------


def test_narrative_empty_window_is_deterministic(fx):
    out = routes.summary_narrative(routes.NarrativeBody(window_days=30),
                                   settings=fx["settings"], conn=fx["conn"])
    assert "No attacks were logged" in out["narrative"]
    assert out["narrative"].endswith("Discuss these patterns with your doctor.")


def test_narrative_uses_model_when_attacks(fx, monkeypatch):
    monkeypatch.setenv("QUIETLOG_MODEL_API_KEY", "test-key")
    db_mod.save_entry(fx["conn"], _entry())

    def fake_post(url, headers=None, payload=None, timeout=None):
        assert "Attack entries: 1" in payload["messages"][1]["content"]
        return FakeResponse(200, _chat_payload("One attack. Discuss these patterns."))

    entries = db_mod.list_entries(fx["conn"])
    summary = compute_summary(
        entries, entries[0].entry_date, entries[0].entry_date,
        generated_at=datetime.now(timezone.utc),
    )
    result = assist.draft_window_narrative(summary, fx["settings"], http_post=fake_post)
    assert result.text == "One attack. Discuss these patterns."
    assert result.cached is False


def test_narrative_route_validates(fx):
    with pytest.raises(HTTPException) as ei:
        routes.summary_narrative(routes.NarrativeBody(window_days=45),
                                 settings=fx["settings"], conn=fx["conn"])
    assert ei.value.status_code == 422
