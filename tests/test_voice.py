"""Voice-note integration tests: recordings, transcription, recovery.

Every test uses synthetic WAV bytes and stubbed model services — no API key,
no network. Recording rows are the source of truth for transcription state;
entry.transcript is the appended dead-drop extraction reads from.
"""

import io
import json
import threading
import wave
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from quietlog import db as db_mod
from quietlog import routes
from quietlog.models import AttackDetails
from quietlog.settings import Settings
from quietlog.transcribe_service import TranscribeError


def _wav_bytes(seconds=1):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x01" * 16000 * 2 * seconds)
    return buf.getvalue()


def _audio(name="note.wav", data=None):
    return UploadFile(filename=name, file=io.BytesIO(data or _wav_bytes()))


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


def _created(fx, transcript=None, audio=None, recording_id=None):
    kwargs = {}
    if recording_id is not None:
        kwargs["recording_id"] = recording_id
    response = routes.create_entry(
        entry_type="attack", transcript=transcript, created_at=None,
        audio=audio, settings=fx["settings"], conn=fx["conn"], **kwargs)
    assert response.status_code in (200, 201)
    return json.loads(response.body)


def _started(fx):
    response = routes.start_attack(created_at=None, settings=fx["settings"],
                                   conn=fx["conn"])
    return json.loads(response.body)["entry"]


def _fake_services(monkeypatch, transcript="spoken note", calls=None,
                   extract=None, fail_with=None):
    """Stub the model services behind processing with call counting."""
    seen = calls if calls is not None else []

    def fake_transcribe(path, config=None, **kw):
        seen.append(str(path))
        if fail_with is not None:
            raise fail_with
        return SimpleNamespace(transcript=transcript)

    def fake_extract(text, reference_time=None, config=None, **kw):
        return SimpleNamespace(
            attack=extract if extract is not None else AttackDetails())

    monkeypatch.setattr(
        "quietlog.processing.transcribe_service.transcribe_file", fake_transcribe)
    monkeypatch.setattr(
        "quietlog.processing.llm_service.extract_attack", fake_extract)
    return seen


def _recording(status, fx, entry_id, recording_id):
    rows = db_mod.list_recordings(fx["conn"], entry_id)
    match = [r for r in rows if r["id"] == recording_id]
    assert match, f"no recording {recording_id}"
    assert match[0]["status"] == status
    return match[0]


# --- automatic transcription -------------------------------------------------


def test_standalone_recording_transcribed_and_extracted(fx, monkeypatch):
    calls = _fake_services(monkeypatch, transcript="pounding since lunch")
    body = _created(fx, audio=_audio(), recording_id="rec-1")
    assert body["saved"] is True
    assert [r["id"] for r in body["entry"]["recordings"]] == ["rec-1"]
    assert body["entry"]["recordings"][0]["status"] == "pending"

    out = routes.process(body["entry"]["id"], settings=fx["settings"],
                         conn=fx["conn"])
    assert out["status"] == "unconfirmed"
    assert out["transcript"] == "pounding since lunch"
    assert out["processing_error"] is None
    assert len(calls) == 1
    row = _recording("transcribed", fx, body["entry"]["id"], "rec-1")
    assert row["transcript"] == "pounding since lunch"
    assert row["appended"] == 1


def test_transcribe_only_route_leaves_timer_running(fx, monkeypatch):
    _fake_services(monkeypatch, transcript="still pounding")
    started = _started(fx)
    attached = routes.attach_note(
        started["id"], transcript=None, audio=_audio(),
        recording_id="rec-run", settings=fx["settings"], conn=fx["conn"])
    assert attached["status"] == "in_progress"
    assert attached["transcript"] == "still pounding"
    assert attached["attack"]["attack_start"] == started["attack"]["attack_start"]
    assert attached["attack"]["ongoing"] is True
    assert "attack_start" in attached["attack"]["edited_fields"]

    out = routes.process(started["id"], settings=fx["settings"], conn=fx["conn"])
    assert out["status"] == "in_progress"  # transcribe-only, never extracts
    assert out["transcript"] == "still pounding"


def test_home_timer_end_processes_attached_audio(fx, monkeypatch):
    """End attempts transcription+extraction inline: nothing waits for a manual
    first attempt. The attach-time attempt fails so the end-time attempt is
    what processes the recording."""
    calls = []
    failures = [TranscribeError("asr down")]

    def flaky(path, config=None, **kw):
        calls.append(str(path))
        if failures:
            raise failures.pop(0)
        return SimpleNamespace(transcript="ache all afternoon")

    monkeypatch.setattr(
        "quietlog.processing.transcribe_service.transcribe_file", flaky)
    monkeypatch.setattr(
        "quietlog.processing.llm_service.extract_attack",
        lambda text, reference_time=None, config=None, **kw: SimpleNamespace(
            attack=AttackDetails()))
    started = _started(fx)
    attached = routes.attach_note(
        started["id"], transcript=None, audio=_audio(),
        recording_id="rec-end", settings=fx["settings"], conn=fx["conn"])
    assert attached["transcript"] is None  # attach-time attempt failed
    assert len(calls) == 1

    out = routes.end_attack(started["id"], ended_at=None,
                            settings=fx["settings"], conn=fx["conn"])
    assert out["status"] == "unconfirmed"
    assert out["transcript"] == "ache all afternoon"
    assert out["processing_error"] is None
    assert len(calls) == 2
    _recording("transcribed", fx, started["id"], "rec-end")


def test_multiple_recordings_append_with_existing_text(fx, monkeypatch):
    texts = iter(["spoken one", "spoken two"])
    calls = _fake_services(
        monkeypatch, transcript="",
        calls=None)  # placeholder, replaced below
    calls.clear()

    def scripted(path, config=None, **kw):
        calls.append(str(path))
        return SimpleNamespace(transcript=next(texts))

    monkeypatch.setattr(
        "quietlog.processing.transcribe_service.transcribe_file", scripted)
    body = _created(fx, transcript="typed words")
    entry_id = body["entry"]["id"]
    routes.attach_note(entry_id, transcript=None, audio=_audio("a.wav"),
                       recording_id="rec-a",
                       settings=fx["settings"], conn=fx["conn"])
    routes.attach_note(entry_id, transcript=None, audio=_audio("b.wav"),
                       recording_id="rec-b",
                       settings=fx["settings"], conn=fx["conn"])
    stored = db_mod.get_entry(fx["conn"], entry_id)
    assert stored.transcript == "typed words\n\nspoken one\n\nspoken two"
    assert len(calls) == 2

    out = routes.process(entry_id, settings=fx["settings"], conn=fx["conn"])
    assert out["status"] == "unconfirmed"
    assert out["transcript"] == "typed words\n\nspoken one\n\nspoken two"
    assert len(calls) == 2  # reprocessing transcribes nothing again


def test_extract_failure_keeps_transcript_without_retranscribe(fx, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "quietlog.processing.transcribe_service.transcribe_file",
        lambda path, config=None, **kw: calls.append(path) or SimpleNamespace(
            transcript="pounding"))
    boom = RuntimeError("extract boom")
    monkeypatch.setattr(
        "quietlog.processing.llm_service.extract_attack",
        lambda *a, **k: (_ for _ in ()).throw(boom))
    body = _created(fx, audio=_audio(), recording_id="rec-x")
    out = routes.process(body["entry"]["id"], settings=fx["settings"],
                         conn=fx["conn"])
    assert out["status"] == "needs_processing"
    assert "extract boom" in out["processing_error"]
    assert out["transcript"] == "pounding"  # transcript persisted anyway
    _recording("transcribed", fx, body["entry"]["id"], "rec-x")
    assert len(calls) == 1

    monkeypatch.setattr(
        "quietlog.processing.llm_service.extract_attack",
        lambda *a, **k: SimpleNamespace(attack=AttackDetails()))
    out = routes.process(body["entry"]["id"], settings=fx["settings"],
                         conn=fx["conn"])
    assert out["status"] == "unconfirmed"
    assert len(calls) == 1  # retry extracted only, never retranscribed


# --- idempotency, restart, overlap --------------------------------------------


def test_retry_same_recording_id_is_idempotent(fx, monkeypatch):
    _fake_services(monkeypatch, transcript="same words")
    started = _started(fx)
    first = routes.attach_note(
        started["id"], transcript=None, audio=_audio("a.wav"),
        recording_id="rec-dup", settings=fx["settings"], conn=fx["conn"])
    files_before = sorted((fx["tmp"] / "audio").iterdir())
    second = routes.attach_note(
        started["id"], transcript=None, audio=_audio("b.wav"),
        recording_id="rec-dup", settings=fx["settings"], conn=fx["conn"])
    assert second["audio_path"] == first["audio_path"]
    assert second["transcript"] == "same words"  # appended exactly once
    assert sorted((fx["tmp"] / "audio").iterdir()) == files_before
    assert len(db_mod.list_recordings(fx["conn"], started["id"])) == 1

    again = _created(fx, audio=_audio("c.wav"), recording_id="rec-new")
    assert again["saved"] is True
    replay = _created(fx, audio=_audio("d.wav"), recording_id="rec-new")
    assert replay["saved"] is False
    assert replay["entry"]["id"] == again["entry"]["id"]
    assert db_mod.count_entries(fx["conn"]) == 2  # timer + one note


def test_restart_resumes_stuck_transcribing(fx, monkeypatch):
    calls = _fake_services(monkeypatch, transcript="after crash")
    body = _created(fx, audio=_audio(), recording_id="rec-crash")
    entry_id = body["entry"]["id"]
    assert db_mod.claim_recording(fx["conn"], "rec-crash", manual=True)
    # ... the process dies here without completing; on next boot:
    assert db_mod.reset_inflight_recordings(fx["conn"]) == 1
    out = routes.process(entry_id, settings=fx["settings"], conn=fx["conn"])
    assert out["status"] == "unconfirmed"
    assert out["transcript"] == "after crash"
    assert len(calls) == 1


def test_transcribed_but_unappended_appends_once(fx, monkeypatch):
    """A crash between persisting the transcript and appending it must heal
    by appending exactly once, never retranscribing."""
    calls = _fake_services(monkeypatch, transcript="healed words")
    body = _created(fx, audio=_audio(), recording_id="rec-half")
    entry_id = body["entry"]["id"]
    assert db_mod.claim_recording(fx["conn"], "rec-half", manual=True)
    db_mod.complete_recording(fx["conn"], "rec-half", "healed words")
    # ... crash before the append; next pass must append without transcribing:
    out = routes.process(entry_id, settings=fx["settings"], conn=fx["conn"])
    assert out["transcript"] == "healed words"
    assert len(calls) == 0
    out = routes.process(entry_id, settings=fx["settings"], conn=fx["conn"])
    assert out["transcript"] == "healed words"


def test_overlapping_process_single_transcription(fx, monkeypatch):
    import sqlite3

    calls = _fake_services(monkeypatch, transcript="one voice")
    body = _created(fx, audio=_audio(), recording_id="rec-race")
    entry_id = body["entry"]["id"]
    barrier = threading.Barrier(2)
    errors = []

    def worker():
        try:
            conn = db_mod.connect(fx["settings"].db_path)
            try:
                barrier.wait(timeout=10)
                routes.process(entry_id, settings=fx["settings"], conn=conn)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    assert len(calls) == 1
    stored = db_mod.get_entry(fx["conn"], entry_id)
    assert stored.transcript == "one voice"


# --- permanent vs transient --------------------------------------------------


def test_permanent_failure_never_auto_retries(fx, monkeypatch):
    calls = _fake_services(
        monkeypatch, fail_with=TranscribeError("bad key", permanent=True))
    body = _created(fx, audio=_audio(), recording_id="rec-perm")
    entry_id = body["entry"]["id"]
    out = routes.process(entry_id, manual=False, settings=fx["settings"],
                         conn=fx["conn"])
    assert out["status"] == "needs_processing"
    assert "bad key" in out["processing_error"]
    row = _recording("failed", fx, entry_id, "rec-perm")
    assert row["failure_kind"] == "permanent"
    assert len(calls) == 1
    routes.process(entry_id, manual=False, settings=fx["settings"],
                   conn=fx["conn"])
    assert len(calls) == 1  # automatic passes leave permanent failures alone
    routes.process(entry_id, manual=True, settings=fx["settings"],
                   conn=fx["conn"])
    assert len(calls) == 2  # a deliberate retry may still try once


def test_transient_failure_backs_off_then_manual_retries(fx, monkeypatch):
    calls = _fake_services(
        monkeypatch, fail_with=TranscribeError("asr down"))
    body = _created(fx, audio=_audio(), recording_id="rec-tr")
    entry_id = body["entry"]["id"]
    routes.process(entry_id, manual=False, settings=fx["settings"],
                   conn=fx["conn"])
    row = _recording("failed", fx, entry_id, "rec-tr")
    assert row["failure_kind"] == "transient"
    assert row["next_attempt_at"] is not None
    assert len(calls) == 1
    routes.process(entry_id, manual=False, settings=fx["settings"],
                   conn=fx["conn"])
    assert len(calls) == 1  # backoff not elapsed: automatic pass skips
    routes.process(entry_id, manual=True, settings=fx["settings"],
                   conn=fx["conn"])
    assert len(calls) == 2  # manual retries bypass the backoff


def test_transient_attempts_are_capped_for_auto_passes(fx, monkeypatch):
    calls = _fake_services(
        monkeypatch, fail_with=TranscribeError("asr down"))
    body = _created(fx, audio=_audio(), recording_id="rec-cap")
    entry_id = body["entry"]["id"]
    fx["conn"].execute(
        "UPDATE recordings SET attempts = 99, next_attempt_at = '2000-01-01T00:00:00+00:00'"
        " WHERE id = 'rec-cap'")
    fx["conn"].commit()
    routes.process(entry_id, manual=False, settings=fx["settings"],
                   conn=fx["conn"])
    assert len(calls) == 0  # budget exhausted: automatic passes stop
    routes.process(entry_id, manual=True, settings=fx["settings"],
                   conn=fx["conn"])
    assert len(calls) == 1  # manual recovery is always available


def test_empty_transcript_is_permanent(fx, monkeypatch):
    calls = _fake_services(monkeypatch, transcript="   ")
    body = _created(fx, audio=_audio(), recording_id="rec-empty")
    entry_id = body["entry"]["id"]
    routes.process(entry_id, settings=fx["settings"], conn=fx["conn"])
    row = _recording("failed", fx, entry_id, "rec-empty")
    assert row["failure_kind"] == "permanent"
    assert len(calls) == 1
    routes.process(entry_id, manual=False, settings=fx["settings"],
                   conn=fx["conn"])
    assert len(calls) == 1


def test_service_classifies_failures(tmp_path, monkeypatch):
    from quietlog import transcribe_service

    audio = tmp_path / "note.wav"
    audio.write_bytes(_wav_bytes())
    cfg = transcribe_service.TranscribeConfig(
        api_key="test-key", cache_dir=str(tmp_path / "cache"))

    class Resp:
        def __init__(self, status, body="err"):
            self.status_code = status
            self.text = body

        def json(self):
            return {}

    for status in (400, 401, 404, 413):
        with pytest.raises(TranscribeError) as exc:
            transcribe_service.transcribe_file(
                audio, config=cfg, use_cache=False,
                http_post=lambda *a, **k: Resp(status))
        assert exc.value.permanent is True, status
    for status in (429, 500, 502):
        with pytest.raises(TranscribeError) as exc:
            transcribe_service.transcribe_file(
                audio, config=cfg, use_cache=False,
                http_post=lambda *a, **k: Resp(status))
        assert exc.value.permanent is False, status

    def boom(*a, **k):
        raise ConnectionError("offline")

    with pytest.raises(TranscribeError) as exc:
        transcribe_service.transcribe_file(
            audio, config=cfg, use_cache=False, http_post=boom)
    assert exc.value.permanent is False

    monkeypatch.delenv("QUIETLOG_MODEL_API_KEY", raising=False)
    no_key = transcribe_service.load_config(cache_dir=str(tmp_path / "cache"))
    assert no_key.api_key is None
    with pytest.raises(TranscribeError) as exc:
        transcribe_service.transcribe_file(audio, config=no_key, use_cache=False)
    assert exc.value.permanent is True

    with pytest.raises(TranscribeError) as exc:
        transcribe_service.transcribe_file(
            tmp_path / "missing.wav", config=cfg, use_cache=False)
    assert exc.value.permanent is True


def test_chat_transcribe_maps_failure_kind(fx, monkeypatch):
    def permanent(path, config=None):
        raise TranscribeError("bad key", permanent=True)

    def transient(path, config=None):
        raise TranscribeError("asr down")

    monkeypatch.setattr("quietlog.routes.transcribe_file", permanent)
    with pytest.raises(HTTPException) as exc:
        routes.transcribe(_audio(), settings=fx["settings"])
    assert exc.value.status_code == 422

    monkeypatch.setattr("quietlog.routes.transcribe_file", transient)
    with pytest.raises(HTTPException) as exc:
        routes.transcribe(_audio(), settings=fx["settings"])
    assert exc.value.status_code == 503


# --- preservation ------------------------------------------------------------


def test_attach_never_deletes_earlier_audio(fx, monkeypatch):
    _fake_services(monkeypatch, transcript="words")
    started = _started(fx)
    first = routes.attach_note(
        started["id"], transcript=None, audio=_audio("a.wav"),
        recording_id="rec-keep-a", settings=fx["settings"], conn=fx["conn"])
    second = routes.attach_note(
        started["id"], transcript=None, audio=_audio("b.wav"),
        recording_id="rec-keep-b", settings=fx["settings"], conn=fx["conn"])
    assert (fx["tmp"] / "audio" / first["audio_path"]).exists()
    assert (fx["tmp"] / "audio" / second["audio_path"]).exists()
    assert len(db_mod.list_recordings(fx["conn"], started["id"])) == 2


def test_delete_removes_all_recording_files(fx, monkeypatch):
    _fake_services(monkeypatch, transcript="words")
    started = _started(fx)
    routes.attach_note(started["id"], transcript=None, audio=_audio("a.wav"),
                       recording_id="rec-del-a",
                       settings=fx["settings"], conn=fx["conn"])
    routes.attach_note(started["id"], transcript=None, audio=_audio("b.wav"),
                       recording_id="rec-del-b",
                       settings=fx["settings"], conn=fx["conn"])
    files = list((fx["tmp"] / "audio").iterdir())
    assert len(files) == 2
    routes.delete_entry(started["id"], settings=fx["settings"], conn=fx["conn"])
    assert list((fx["tmp"] / "audio").iterdir()) == []
    assert db_mod.list_recordings(fx["conn"], started["id"]) == []


def test_legacy_audio_adopted_unless_confirmed(fx, monkeypatch):
    calls = _fake_services(monkeypatch, transcript="legacy words")
    body = _created(fx, transcript="typed earlier")
    entry_id = body["entry"]["id"]
    # Simulate a pre-recordings row: audio on disk, no recording row.
    legacy_name = "legacy-note.wav"
    (fx["tmp"] / "audio").mkdir(exist_ok=True)
    (fx["tmp"] / "audio" / legacy_name).write_bytes(_wav_bytes())
    stored = db_mod.get_entry(fx["conn"], entry_id)
    stored.audio_path = legacy_name
    db_mod.save_entry(fx["conn"], stored)

    out = routes.process(entry_id, settings=fx["settings"], conn=fx["conn"])
    assert out["transcript"] == "typed earlier\n\nlegacy words"
    assert len(calls) == 1
    rows = db_mod.list_recordings(fx["conn"], entry_id)
    assert len(rows) == 1 and rows[0]["status"] == "transcribed"

    # Confirmed history is never silently rewritten by adoption.
    routes.update_entry(entry_id, routes.PatchBody(confirm=True),
                        settings=fx["settings"], conn=fx["conn"])
    (fx["tmp"] / "audio" / "late.wav").write_bytes(_wav_bytes())
    stored = db_mod.get_entry(fx["conn"], entry_id)
    stored.audio_path = "late.wav"
    db_mod.save_entry(fx["conn"], stored)
    out = routes.process(entry_id, settings=fx["settings"], conn=fx["conn"])
    assert out["transcript"] == "typed earlier\n\nlegacy words"
    assert len(calls) == 1
    assert len(db_mod.list_recordings(fx["conn"], entry_id)) == 1


def test_process_never_rewrites_confirmed_entries(fx, monkeypatch):
    """Manual edits are final: a stray reprocess must not re-extract or
    un-confirm a confirmed entry."""
    from quietlog.processing import process_entry

    _fake_services(monkeypatch, transcript="pounding",
                   extract=AttackDetails(pain_0_10=3))
    body = _created(fx, transcript="pounding", audio=_audio(),
                    recording_id="rec-conf")
    entry_id = body["entry"]["id"]
    run = routes.process(entry_id, settings=fx["settings"], conn=fx["conn"])
    assert run["status"] == "unconfirmed"
    routes.update_entry(entry_id, routes.PatchBody(confirm=True),
                        settings=fx["settings"], conn=fx["conn"])

    _fake_services(monkeypatch, transcript="pounding",
                   extract=AttackDetails(pain_0_10=9))
    stored = process_entry(fx["conn"], db_mod.get_entry(fx["conn"], entry_id),
                           fx["settings"])
    assert stored.status == "confirmed"
    assert stored.attack.pain_0_10 == 3  # extraction never re-ran


def test_pending_voice_flag_tracks_untranscribed_work(fx, monkeypatch):
    """List responses flag entries with untranscribed recordings so the
    background sweep can find them in any status (incl. unconfirmed)."""
    failing = {"on": True}
    calls = []

    def fake_transcribe(path, config=None, **kw):
        calls.append(str(path))
        if failing["on"]:
            raise TranscribeError("asr down")
        return SimpleNamespace(transcript="spoken words")

    monkeypatch.setattr(
        "quietlog.processing.transcribe_service.transcribe_file", fake_transcribe)
    monkeypatch.setattr(
        "quietlog.processing.llm_service.extract_attack",
        lambda *a, **k: SimpleNamespace(attack=AttackDetails()))
    body = _created(fx, transcript="typed words", audio=_audio(),
                    recording_id="rec-flag")
    entry_id = body["entry"]["id"]
    out = routes.process(entry_id, settings=fx["settings"], conn=fx["conn"])
    assert out["status"] == "unconfirmed"  # typed text extracted fine
    listed = routes.list_entries(status="unconfirmed", conn=fx["conn"])
    flagged = {e["id"]: e["pending_voice"] for e in listed["entries"]}
    assert flagged[entry_id] is True  # sweep must find it here

    failing["on"] = False
    routes.process(entry_id, manual=True, settings=fx["settings"],
                   conn=fx["conn"])
    listed = routes.list_entries(status="unconfirmed", conn=fx["conn"])
    flagged = {e["id"]: e["pending_voice"] for e in listed["entries"]}
    assert flagged[entry_id] is False
    stored = db_mod.get_entry(fx["conn"], entry_id)
    assert stored.transcript == "typed words\n\nspoken words"


def test_process_deleted_entry_never_recreates(fx, monkeypatch):
    from quietlog.processing import process_entry

    calls = _fake_services(monkeypatch, transcript="ghost words")
    body = _created(fx, audio=_audio(), recording_id="rec-ghost")
    stale = db_mod.get_entry(fx["conn"], body["entry"]["id"])
    routes.delete_entry(body["entry"]["id"], settings=fx["settings"],
                        conn=fx["conn"])
    out = process_entry(fx["conn"], stale, fx["settings"])
    assert out.id == stale.id
    assert db_mod.get_entry(fx["conn"], stale.id) is None
    assert len(calls) == 0


def test_typed_attach_appends_instead_of_replacing(fx):
    started = _started(fx)
    routes.attach_note(started["id"], transcript="first words", audio=None,
                       settings=fx["settings"], conn=fx["conn"])
    out = routes.attach_note(started["id"], transcript="second words",
                             audio=None, settings=fx["settings"], conn=fx["conn"])
    assert out["transcript"] == "first words\n\nsecond words"
