"""Capture/review/retry API tests (M3). Offline: processing is stubbed.

Direct route-function calls: this sandbox forbids listen(), so no ASGI
runtime (TestClient, ASGI transport, uvicorn) can run here. Framework-level
form parsing is upstream-tested; these tests cover all route logic:
validation, save-first persistence, processing outcomes, edits, confirm.
"""

import io
import json
import wave
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from quietlog import db as db_mod
from quietlog import routes
from quietlog.models import AttackDetails
from quietlog.processing import process_entry as run_processing
from quietlog.settings import Settings
from quietlog.types import EntryStatus


def _wav_bytes(seconds=1, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00" * rate * 2 * seconds)
    return buf.getvalue()


def _audio(filename="note.wav", data=None):
    return UploadFile(filename=filename, file=io.BytesIO(data or _wav_bytes()))


@pytest.fixture()
def fx(tmp_path, monkeypatch):
    settings = Settings(
        db_path=str(tmp_path / "t.db"),
        audio_dir=str(tmp_path / "audio"),
        cache_dir=str(tmp_path / "cache"),
    )
    monkeypatch.setattr(routes, "process_entry", fake_process_ok)
    # Inline transcription (attach/end) stays hermetic here: no model calls.
    # Real transcribe-phase behavior is covered in tests/test_voice.py.
    monkeypatch.setattr(
        routes, "transcribe_entry_recordings",
        lambda conn, entry, settings, **kw: entry)
    conn = db_mod.connect(settings.db_path)
    yield SimpleNamespace(settings=settings, conn=conn, tmp=tmp_path)
    conn.close()


def fake_process_ok(conn, entry, settings, coords=None, manual=True):
    entry.attack = AttackDetails(
        time_expression="at 2pm",
        severity_function=2,
        evidence={"time_expression": "at 2pm", "severity_function": "moderate"},
    )
    entry.status = EntryStatus.UNCONFIRMED
    entry.processing_error = None
    return db_mod.save_entry(conn, entry)


def fake_process_fail(conn, entry, settings, coords=None, manual=True):
    entry.status = EntryStatus.NEEDS_PROCESSING
    entry.processing_error = "LLMError: model unavailable"
    return db_mod.save_entry(conn, entry)


def _passthrough_process(calls):
    """Inline-processing stub that preserves the entry: end_attack's stamp
    assertions need the entry untouched, with the attempt recorded."""

    def run(conn, entry, settings, coords=None, manual=True):
        calls.append(entry.id)
        return db_mod.save_entry(conn, entry)

    return run


def _created(entry_type="attack", fx=None, transcript="Migraine at 2pm.",
             audio=None, created_at=None):
    response = routes.create_entry(
        entry_type=entry_type, transcript=transcript, created_at=created_at,
        audio=audio, settings=fx.settings, conn=fx.conn)
    assert response.status_code == 201
    body = json.loads(response.body)
    assert body["saved"] is True
    return body["entry"]


def test_save_attack_text(fx):
    entry = _created(fx=fx)
    assert entry["status"] == "needs_processing"
    assert entry["transcript"] == "Migraine at 2pm."
    assert db_mod.get_entry(fx.conn, entry["id"]) is not None


def test_save_attack_audio(fx):
    entry = _created(fx=fx, transcript=None, audio=_audio())
    assert entry["audio_path"].endswith(".wav")
    assert (fx.tmp / "audio" / entry["audio_path"]).exists()


def test_save_checkin_confirmed(fx):
    entry = _created(entry_type="no_attack", fx=fx, transcript=None)
    assert entry["status"] == "confirmed"
    assert entry["attack"] is None


def test_attack_needs_content(fx):
    with pytest.raises(HTTPException) as exc:
        routes.create_entry(entry_type="attack", transcript="  ",
                            created_at=None, audio=None,
                            settings=fx.settings, conn=fx.conn)
    assert exc.value.status_code == 422


def test_bad_entry_type_rejected(fx):
    with pytest.raises(HTTPException) as exc:
        routes.create_entry(entry_type="nap", transcript=None, created_at=None,
                            audio=None, settings=fx.settings, conn=fx.conn)
    assert exc.value.status_code == 422


def test_non_wav_rejected(fx):
    with pytest.raises(HTTPException) as exc:
        routes.create_entry(entry_type="attack", transcript=None,
                            created_at=None, audio=_audio("n.mp3", b"ID3 x"),
                            settings=fx.settings, conn=fx.conn)
    assert exc.value.status_code == 422


def test_process_advances_to_unconfirmed(fx):
    entry = _created(fx=fx)
    out = routes.process(entry["id"], settings=fx.settings, conn=fx.conn)
    assert out["status"] == "unconfirmed"
    assert out["attack"]["severity_function"] == 2
    assert out["processing_error"] is None


def test_process_failure_preserves_saved_entry(fx, monkeypatch):
    monkeypatch.setattr(routes, "process_entry", fake_process_fail)
    entry = _created(fx=fx)
    out = routes.process(entry["id"], settings=fx.settings, conn=fx.conn)
    assert out["status"] == "needs_processing"
    assert "unavailable" in out["processing_error"]
    assert out["transcript"] == "Migraine at 2pm."  # nothing lost
    queue = routes.list_entries(status="needs_processing", entry_type=None,
                                limit=100, conn=fx.conn)
    assert [e["id"] for e in queue["entries"]] == [entry["id"]]


def test_retry_succeeds_after_failure(fx, monkeypatch):
    entry = _created(fx=fx)
    monkeypatch.setattr(routes, "process_entry", fake_process_fail)
    first = routes.process(entry["id"], settings=fx.settings, conn=fx.conn)
    assert first["status"] == "needs_processing"
    monkeypatch.setattr(routes, "process_entry", fake_process_ok)
    second = routes.process(entry["id"], settings=fx.settings, conn=fx.conn)
    assert second["status"] == "unconfirmed"


def test_list_filters(fx):
    _created(fx=fx)
    _created(entry_type="no_attack", fx=fx, transcript=None)
    all_entries = routes.list_entries(status=None, entry_type=None, limit=100, conn=fx.conn)
    assert all_entries["count"] == 2
    confirmed = routes.list_entries(status="confirmed", entry_type=None, limit=100, conn=fx.conn)
    assert confirmed["count"] == 1
    attacks = routes.list_entries(status=None, entry_type="attack", limit=100, conn=fx.conn)
    assert attacks["count"] == 1
    with pytest.raises(HTTPException):
        routes.list_entries(status="bogus", entry_type=None, limit=100, conn=fx.conn)


def test_list_entries_date_range(fx):
    _created(fx=fx, created_at="2026-10-28T09:00:00")
    _created(fx=fx, created_at="2026-10-30T14:00:00")
    _created(fx=fx, created_at="2026-11-02T10:00:00")
    october = routes.list_entries(status=None, entry_type=None,
                                  start="2026-10-29", end="2026-10-31",
                                  limit=100, conn=fx.conn)
    assert october["count"] == 1
    both = routes.list_entries(status=None, entry_type=None,
                               start="2026-10-01", end="2026-11-30",
                               limit=100, conn=fx.conn)
    assert both["count"] == 3
    single = routes.list_entries(status=None, entry_type=None,
                                 start="2026-10-30", end="2026-10-30",
                                 limit=100, conn=fx.conn)
    assert single["count"] == 1
    with pytest.raises(HTTPException) as exc:
        routes.list_entries(status=None, entry_type=None, start="october",
                            end=None, limit=100, conn=fx.conn)
    assert exc.value.status_code == 422


def test_missing_entries_404(fx):
    with pytest.raises(HTTPException) as exc:
        routes.get_entry("nope", conn=fx.conn)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException):
        routes.process("nope", settings=fx.settings, conn=fx.conn)
    with pytest.raises(HTTPException):
        routes.update_entry("nope", routes.PatchBody(confirm=True),
                            settings=fx.settings, conn=fx.conn)


def test_patch_edits_and_confirms(fx):
    entry = _created(fx=fx)
    routes.process(entry["id"], settings=fx.settings, conn=fx.conn)
    out = routes.update_entry(
        entry["id"],
        routes.PatchBody(attack={"severity_function": 3,
                                 "symptoms": {"nausea": True},
                                 "time_expression": "at 3pm"},
                         confirm=True),
        settings=fx.settings, conn=fx.conn)
    assert out["status"] == "confirmed"
    assert out["attack"]["severity_function"] == 3
    assert out["attack"]["symptoms"]["nausea"] is True
    assert out["attack"]["attack_start"] is not None  # re-resolved
    assert "severity_function" not in out["attack"]["evidence"]
    assert "time_expression" not in out["attack"]["evidence"]  # user-edited


def test_patch_time_range_fills_empty_duration(fx):
    entry = _created(fx=fx)
    routes.process(entry["id"], settings=fx.settings, conn=fx.conn)
    out = routes.update_entry(
        entry["id"], routes.PatchBody(attack={"time_expression": "from 8am to 12pm"}),
        settings=fx.settings, conn=fx.conn)
    assert out["attack"]["duration_minutes"] == 240
    explicit = routes.update_entry(
        entry["id"],
        routes.PatchBody(attack={"time_expression": "from 8am to 12pm",
                                 "duration_minutes": 30}),
        settings=fx.settings, conn=fx.conn)
    assert explicit["attack"]["duration_minutes"] == 30


def test_confirm_unprocessed_rejected(fx):
    entry = _created(fx=fx)
    with pytest.raises(HTTPException) as exc:
        routes.update_entry(entry["id"], routes.PatchBody(confirm=True),
                            settings=fx.settings, conn=fx.conn)
    assert exc.value.status_code == 422


def test_patch_unknown_field_rejected(fx):
    entry = _created(fx=fx)
    with pytest.raises(HTTPException) as exc:
        routes.update_entry(entry["id"], routes.PatchBody(attack={"diagnosis": "x"}),
                            settings=fx.settings, conn=fx.conn)
    assert exc.value.status_code == 422


def test_full_flow_save_process_confirm(fx):
    entry = _created(fx=fx)
    processed = routes.process(entry["id"], settings=fx.settings, conn=fx.conn)
    assert processed["status"] == "unconfirmed"
    confirmed = routes.update_entry(entry["id"], routes.PatchBody(confirm=True),
                                    settings=fx.settings, conn=fx.conn)
    assert confirmed["status"] == "confirmed"
    assert confirmed["attack"]["severity_function"] == 2
    assert datetime.fromisoformat(confirmed["created_at"])


def test_summary_counts_and_calendar(fx):
    attack = _created(fx=fx, created_at="2026-10-30T14:00:00")
    routes.process(attack["id"], settings=fx.settings, conn=fx.conn)
    routes.update_entry(attack["id"], routes.PatchBody(confirm=True),
                        settings=fx.settings, conn=fx.conn)
    pending = _created(fx=fx, created_at="2026-10-28T09:00:00")
    routes.process(pending["id"], settings=fx.settings, conn=fx.conn)
    _created(entry_type="no_attack", fx=fx, transcript=None,
             created_at="2026-10-29T21:00:00")
    out = routes.summary(window_days=30, end="2026-10-31", conn=fx.conn)
    assert out["window_start"] == "2026-10-02"
    assert out["window_end"] == "2026-10-31"
    assert out["attack_days"] == 2
    assert out["no_attack_days"] == 1
    assert out["not_logged_days"] == 27
    assert out["attack_entries"] == 2
    assert out["unconfirmed_entries"] == 1
    assert len(out["calendar"]) == 30
    assert out["severity_counts"] == {"1": 0, "2": 2, "3": 0}
    assert out["severity_n"] == 2
    by_day = {d["day"]: d for d in out["calendar"]}
    assert by_day["2026-10-30"]["state"] == "attack"
    assert by_day["2026-10-30"]["unconfirmed"] is False
    assert by_day["2026-10-28"]["unconfirmed"] is True
    assert by_day["2026-10-29"]["state"] == "no_attack"
    assert by_day["2026-10-27"]["state"] == "not_logged"


def test_summary_validation(fx):
    with pytest.raises(HTTPException) as exc:
        routes.summary(window_days=45, end=None, conn=fx.conn)
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        routes.summary(window_days=30, end="yesterday", conn=fx.conn)
    assert exc.value.status_code == 422


def test_summary_empty_window(fx):
    out = routes.summary(window_days=30, end="2026-10-31", conn=fx.conn)
    assert out["attack_days"] == 0
    assert out["not_logged_days"] == 30
    assert out["attack_entries"] == 0
    assert out["duration"]["n"] == 0
    assert out["medications"] == []


def test_created_at_explicit_and_bad(fx):
    entry = _created(fx=fx, created_at="2026-10-24T14:30:00-04:00")
    # Same instant, canonical UTC text: mixed offsets would mis-sort History.
    assert entry["created_at"] == "2026-10-24T18:30:00Z"
    assert entry["entry_date"] == "2026-10-24"
    with pytest.raises(HTTPException) as exc:
        routes.create_entry(entry_type="attack", transcript="x",
                            created_at="not-a-date", audio=None,
                            settings=fx.settings, conn=fx.conn)
    assert exc.value.status_code == 422


def _started(fx, created_at="2026-10-30T14:00:00-04:00"):
    response = routes.start_attack(created_at=created_at,
                                   settings=fx.settings, conn=fx.conn)
    assert response.status_code in (200, 201)
    return json.loads(response.body)["entry"]


def test_start_attack_stamps_exact_start(fx):
    response = routes.start_attack(created_at="2026-10-30T14:00:00-04:00",
                                   settings=fx.settings, conn=fx.conn)
    assert response.status_code == 201
    body = json.loads(response.body)
    assert body["saved"] is True
    entry = body["entry"]
    assert entry["status"] == "in_progress"
    assert entry["attack"]["attack_start"] == "2026-10-30T14:00:00-04:00"
    assert entry["attack"]["attack_start_precision"] == "exact"
    assert entry["attack"]["ongoing"] is True
    assert entry["entry_date"] == "2026-10-30"


def test_start_attack_idempotent_while_active(fx):
    first = _started(fx)
    response = routes.start_attack(created_at=None,
                                   settings=fx.settings, conn=fx.conn)
    assert response.status_code == 200  # second tap returns the live timer
    second = json.loads(response.body)["entry"]
    assert second["id"] == first["id"]
    active = routes.list_entries(status="in_progress", entry_type=None,
                                    limit=100, conn=fx.conn)
    assert active["count"] == 1


def test_end_attack_computes_duration(fx, monkeypatch):
    started = _started(fx)
    attempted = []
    monkeypatch.setattr(
        routes, "process_entry", _passthrough_process(attempted))
    out = routes.end_attack(started["id"],
                            ended_at="2026-10-30T14:47:00-04:00",
                            settings=fx.settings, conn=fx.conn)
    assert attempted == [started["id"]]  # end processes inline, exactly once
    assert out["status"] == "needs_processing"
    assert out["attack"]["attack_end"] == "2026-10-30T14:47:00-04:00"
    assert out["attack"]["duration_minutes"] == 47
    assert out["attack"]["ongoing"] is False
    assert out["attack"]["attack_start"] == "2026-10-30T14:00:00-04:00"


def test_end_attack_instant_still_counts_a_minute(fx, monkeypatch):
    started = _started(fx)
    monkeypatch.setattr(routes, "process_entry", _passthrough_process([]))
    out = routes.end_attack(started["id"],
                            ended_at="2026-10-30T14:00:00-04:00",
                            settings=fx.settings, conn=fx.conn)
    assert out["attack"]["duration_minutes"] == 1


def test_end_attack_before_start_clamps(fx, monkeypatch):
    started = _started(fx)
    monkeypatch.setattr(routes, "process_entry", _passthrough_process([]))
    out = routes.end_attack(started["id"],
                            ended_at="2026-10-30T13:00:00-04:00",
                            settings=fx.settings, conn=fx.conn)
    assert out["attack"]["duration_minutes"] == 1
    assert out["attack"]["attack_end"] == "2026-10-30T13:00:00-04:00"


def test_end_attack_guards(fx):
    entry = _created(fx=fx)  # needs_processing, never started
    with pytest.raises(HTTPException) as exc:
        routes.end_attack(entry["id"], ended_at=None, conn=fx.conn)
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        routes.end_attack("nope", ended_at=None, conn=fx.conn)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        routes.start_attack(created_at="not-a-date",
                            settings=fx.settings, conn=fx.conn)
    assert exc.value.status_code == 422


def test_active_attack_transcribe_only_cannot_confirm(fx):
    started = _started(fx)
    # Processing a running timer transcribes without extracting or stopping it.
    out = routes.process(started["id"], settings=fx.settings, conn=fx.conn)
    assert out["status"] == "in_progress"
    with pytest.raises(HTTPException) as exc:
        routes.update_entry(started["id"], routes.PatchBody(confirm=True),
                            settings=fx.settings, conn=fx.conn)
    assert exc.value.status_code == 422


def test_attach_note_while_active(fx):
    started = _started(fx)
    out = routes.attach_note(started["id"], transcript="Throbbing left side.",
                             audio=None, settings=fx.settings, conn=fx.conn)
    assert out["status"] == "in_progress"  # timer keeps running
    assert out["transcript"] == "Throbbing left side."
    ended = routes.end_attack(started["id"], ended_at=None,
                              settings=fx.settings, conn=fx.conn)
    assert ended["transcript"] == "Throbbing left side."


def test_attach_note_audio_and_guards(fx):
    started = _started(fx)
    out = routes.attach_note(started["id"], transcript=None, audio=_audio(),
                             settings=fx.settings, conn=fx.conn)
    assert out["audio_path"].endswith(".wav")
    assert (fx.tmp / "audio" / out["audio_path"]).exists()
    with pytest.raises(HTTPException) as exc:
        routes.attach_note(started["id"], transcript="  ", audio=None,
                           settings=fx.settings, conn=fx.conn)
    assert exc.value.status_code == 422  # empty note
    with pytest.raises(HTTPException) as exc:
        routes.attach_note("nope", transcript="x", audio=None,
                           settings=fx.settings, conn=fx.conn)
    assert exc.value.status_code == 404
    entry = _created(fx=fx)
    routes.process(entry["id"], settings=fx.settings, conn=fx.conn)
    routes.update_entry(entry["id"], routes.PatchBody(confirm=True),
                        settings=fx.settings, conn=fx.conn)
    with pytest.raises(HTTPException) as exc:
        routes.attach_note(entry["id"], transcript="late words", audio=None,
                           settings=fx.settings, conn=fx.conn)
    assert exc.value.status_code == 422  # understood entries edit in review


def test_delete_entry_removes_it_and_its_audio(fx):
    started = _started(fx)
    noted = routes.attach_note(started["id"], transcript=None, audio=_audio(),
                               settings=fx.settings, conn=fx.conn)
    audio_file = fx.tmp / "audio" / noted["audio_path"]
    out = routes.delete_entry(started["id"], settings=fx.settings, conn=fx.conn)
    assert out == {"deleted": True}
    assert db_mod.get_entry(fx.conn, started["id"]) is None
    assert not audio_file.exists()
    with pytest.raises(HTTPException) as exc:
        routes.delete_entry("nope", settings=fx.settings, conn=fx.conn)
    assert exc.value.status_code == 404


def test_delete_entry_with_saved_content(fx):
    """Any entry can be deleted (user decision, confirmed in the UI):
    content is no guard, only missing ids 404."""
    entry = _created(fx=fx)  # needs_processing with a transcript
    out = routes.delete_entry(entry["id"], settings=fx.settings, conn=fx.conn)
    assert out == {"deleted": True}
    assert db_mod.get_entry(fx.conn, entry["id"]) is None
    confirmed = _created(fx=fx)
    routes.process(confirmed["id"], settings=fx.settings, conn=fx.conn)
    routes.update_entry(confirmed["id"], routes.PatchBody(confirm=True),
                        settings=fx.settings, conn=fx.conn)
    out = routes.delete_entry(confirmed["id"], settings=fx.settings, conn=fx.conn)
    assert out == {"deleted": True}
    assert db_mod.get_entry(fx.conn, confirmed["id"]) is None


def test_processing_preserves_stamped_timing(fx, monkeypatch):
    started = _started(fx)
    routes.attach_note(started["id"],
                       transcript="Migraine since lunch, two hours, still going.",
                       audio=None, settings=fx.settings, conn=fx.conn)
    # End's inline pass is stubbed out: this test pins the stamps through the
    # real re-extraction below, not through end itself.
    monkeypatch.setattr(routes, "process_entry", _passthrough_process([]))
    routes.end_attack(started["id"], ended_at="2026-10-30T14:47:00-04:00",
                      settings=fx.settings, conn=fx.conn)

    def fake_extract(transcript, reference_time):
        assert transcript.startswith("Migraine since lunch")
        return AttackDetails(
            time_expression="since lunch",
            attack_start=datetime(2026, 10, 30, 12, 0),
            attack_start_precision="approximate",
            duration_minutes=120,
            ongoing=True,
            severity_function=2,
            evidence={"duration_minutes": "two hours"},
        )

    out = run_processing(
        fx.conn, db_mod.get_entry(fx.conn, started["id"]), fx.settings,
        extract_fn=fake_extract)
    assert out.status == EntryStatus.UNCONFIRMED
    assert out.attack.attack_start.isoformat() == "2026-10-30T14:00:00-04:00"
    assert out.attack.attack_start_precision.value == "exact"
    assert out.attack.attack_end.isoformat() == "2026-10-30T14:47:00-04:00"
    assert out.attack.duration_minutes == 47
    assert out.attack.ongoing is False
    assert out.attack.severity_function == 2  # extracted fields still apply
    assert "duration_minutes" not in out.attack.evidence  # user is the source


def test_summary_counts_active_attack(fx):
    _started(fx)
    out = routes.summary(window_days=30, end="2026-10-31", conn=fx.conn)
    assert out["attack_entries"] == 1
    assert out["attack_days"] == 1
    by_day = {d["day"]: d for d in out["calendar"]}
    assert by_day["2026-10-30"]["state"] == "attack"
    assert by_day["2026-10-30"]["unconfirmed"] is True


def _upload(filename, data):
    return UploadFile(filename=filename, file=io.BytesIO(data))


def test_transcribe_returns_transcript(fx, monkeypatch):
    monkeypatch.setattr(
        "quietlog.routes.transcribe_file",
        lambda path, config=None: SimpleNamespace(transcript="  hello diary  "),
    )
    out = routes.transcribe(_upload("note.wav", _wav_bytes()), settings=fx.settings)
    assert out == {"transcript": "hello diary"}
    assert db_mod.list_entries(fx.conn) == []


def test_transcribe_rejects_non_wav(fx):
    with pytest.raises(HTTPException) as ei:
        routes.transcribe(_upload("note.txt", b"not audio"), settings=fx.settings)
    assert ei.value.status_code == 422


def test_transcribe_transient_failure_is_503(fx, monkeypatch):
    from quietlog.transcribe_service import TranscribeError

    def fail(path, config=None):
        raise TranscribeError("asr down")

    monkeypatch.setattr("quietlog.routes.transcribe_file", fail)
    with pytest.raises(HTTPException) as ei:
        routes.transcribe(_upload("note.wav", _wav_bytes()), settings=fx.settings)
    assert ei.value.status_code == 503  # retryable: client backs off, same audio

    def fail_permanent(path, config=None):
        raise TranscribeError("bad key", permanent=True)

    monkeypatch.setattr("quietlog.routes.transcribe_file", fail_permanent)
    with pytest.raises(HTTPException) as ei:
        routes.transcribe(_upload("note.wav", _wav_bytes()), settings=fx.settings)
    assert ei.value.status_code == 422  # retrying cannot help
