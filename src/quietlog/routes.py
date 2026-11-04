"""Capture/review/retry HTTP API (M3).

Save-first throughout: `POST /entries` writes audio + text immediately and
answers "Saved"; transcription/extraction run afterward via
`POST /entries/{id}/process` and can be retried any number of times. Nothing
the user saved is ever lost to a model or network failure.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import tempfile
import wave
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from . import agent, assist, db as db_mod
from . import weather_service
from .llm_service import LLMError, ModelConfigError
from .models import AttackDetails, Entry, WeatherSnapshot, mark_edited
from .processing import (
    append_followup_answers,
    process_entry,
    transcribe_entry_recordings,
)
from .settings import Settings, load_settings
from .summary import Summary, compute_summary
from .time_resolve import resolve_attack_start, resolve_duration_range
from .transcribe_service import MAX_BYTES, TranscribeError, load_config, transcribe_file
from .types import EntryStatus, EntryType, TimePrecision

__all__ = ["router", "get_conn", "get_settings"]

logger = logging.getLogger(__name__)

router = APIRouter()


def get_settings() -> Settings:
    return load_settings()


def get_conn(settings: Settings = Depends(get_settings)):
    conn = db_mod.connect(settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def _entry_json(
    entry: Entry, recordings: Optional[list[dict[str, Any]]] = None
) -> dict[str, Any]:
    """Serialize one entry. Single-entry responses pass the entry's recording
    rows so the client can show per-note transcription state; list responses
    leave the default empty list."""
    body = entry.model_dump(mode="json")
    # Always present so clients can flag voice work in any response.
    body["pending_voice"] = bool(recordings) and any(
        row["status"] in ("pending", "transcribing")
        or (row["status"] == "failed" and row["failure_kind"] == "transient")
        for row in recordings or []
    )
    if recordings is not None:
        body["recordings"] = [
            {
                "id": row["id"],
                "status": row["status"],
                "failure_kind": row["failure_kind"],
                "attempts": row["attempts"],
                "next_attempt_at": row["next_attempt_at"],
                "error": row["error"],
                "transcript": row["transcript"],
                "appended": bool(row["appended"]),
            }
            for row in recordings
        ]
    return body


def _clean_recording_id(raw: Optional[str]) -> Optional[str]:
    """Validate a client-generated recording id (stable across retries of one
    upload). Blank means none; overlong is rejected, never truncated."""
    if not isinstance(raw, str):
        return None  # unset (or a bare Form default on direct calls)
    recording_id = raw.strip() or None
    if raw is not None and recording_id is None:
        raise HTTPException(422, "recording_id must not be blank")
    if recording_id is not None and len(recording_id) > 128:
        raise HTTPException(422, "recording_id is too long")
    return recording_id


def _recording_replay(
    conn: sqlite3.Connection, recording_id: str
) -> Optional[Entry]:
    """The entry already saved for this recording id, or None if the id is
    free. A claim whose entry never appeared is an orphaned first attempt
    (crash between row claim and entry save): stale ones are reclaimed so
    the id works again, fresh ones 409 (another attempt is mid-save)."""
    row = db_mod.get_recording(conn, recording_id)
    if row is None:
        return None
    entry = db_mod.get_entry(conn, row["entry_id"])
    if entry is not None:
        return entry
    try:
        created = datetime.fromisoformat(row["created_at"])
    except ValueError:
        created = None
    if created is not None:
        if created.tzinfo is None:
            created = created.replace(tzinfo=datetime.now().astimezone().tzinfo)
        age = (datetime.now().astimezone() - created).total_seconds()
        if age <= 60:
            raise HTTPException(
                409, "this upload is already in progress; try again in a moment")
    db_mod.delete_recording(conn, recording_id)
    return None


def _log_time_weather(
    latitude: Optional[float],
    longitude: Optional[float],
    settings: Settings,
) -> Optional[WeatherSnapshot]:
    """Weather snapshot for a save happening now. Never raises, never blocks
    long: any failure (or missing/disabled weather) yields no snapshot."""
    return weather_service.log_time_snapshot(
        latitude, longitude,
        enabled=settings.weather_enabled, timeout=settings.weather_timeout,
    )


class PatchBody(BaseModel):
    attack: Optional[dict[str, Any]] = None
    confirm: bool = False


class AnswerItem(BaseModel):
    question: str
    answer: str


class AnswerBody(BaseModel):
    answers: list[AnswerItem]


class AskBody(BaseModel):
    question: str
    window_days: int = 90


class NarrativeBody(BaseModel):
    window_days: int = 30


class AgentMessage(BaseModel):
    role: str
    content: str


class AgentContext(BaseModel):
    """Prior turn's state echoed back by the client. Every id is revalidated
    against the database before use; unknown or unsuitable ids are dropped."""

    discussed_entry_id: Optional[str] = None
    active_attack_id: Optional[str] = None
    pending_entry_id: Optional[str] = None


class AgentBody(BaseModel):
    message: str
    history: list[AgentMessage] = []
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    # Stable client-generated id per submitted message. Retries reuse it;
    # the turn runs once and replays its stored result afterwards.
    request_id: Optional[str] = None
    context: Optional[AgentContext] = None


@router.post("/entries", status_code=201)
def create_entry(
    entry_type: str = Form(...),
    transcript: Optional[str] = Form(None),
    created_at: Optional[str] = Form(None),
    audio: Optional[UploadFile] = File(None),
    recording_id: Optional[str] = Form(None),
    latitude: Optional[float] = Form(None),
    longitude: Optional[float] = Form(None),
    settings: Settings = Depends(get_settings),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Save one entry instantly. Attack entries arrive as `needs_processing`;
    check-ins arrive `confirmed`. Always answers while the user waits.

    Voice uploads carry a client-generated `recording_id`: retrying the same
    upload (lost response, dropped connection) returns the original entry
    instead of saving a duplicate — checked before any file is stored.
    """
    try:
        kind = EntryType(entry_type)
    except ValueError:
        raise HTTPException(422, f"entry_type must be 'attack' or 'no_attack', "
                                 f"got {entry_type!r}")
    saved_at = _parse_created_at(created_at)
    text = (transcript or "").strip() or None
    record_id = _clean_recording_id(recording_id)

    if record_id is not None and audio is not None and audio.filename:
        replay = _recording_replay(conn, record_id)
        if replay is not None:
            return JSONResponse(
                {"saved": False,
                 "entry": _entry_json(
                     replay, db_mod.list_recordings(conn, replay.id))},
                status_code=200,
            )

    audio_name: Optional[str] = None
    if kind == EntryType.ATTACK and audio is not None and audio.filename:
        audio_name = _store_audio(audio, settings)
    if kind == EntryType.ATTACK and audio_name is None and text is None:
        raise HTTPException(422, "attack entries need voice audio or typed text")

    entry_id = uuid4().hex
    if audio_name is not None and record_id is not None:
        # Claim the recording id before saving anything: a simultaneous
        # retry of the same upload loses here and replays instead.
        try:
            db_mod.create_recording(conn, record_id, entry_id, audio_name)
        except sqlite3.IntegrityError:
            _remove_audio_file(audio_name, settings)
            replay = _recording_replay(conn, record_id)
            if replay is None:  # pragma: no cover - reclaim raced us; retry
                raise HTTPException(
                    409, "this upload is already in progress; try again in a moment")
            return JSONResponse(
                {"saved": False,
                 "entry": _entry_json(
                     replay, db_mod.list_recordings(conn, replay.id))},
                status_code=200,
            )
    entry = Entry(
        id=entry_id,
        entry_type=kind,
        created_at=saved_at,
        audio_path=audio_name,
        transcript=text,
        status=(EntryStatus.CONFIRMED if kind == EntryType.NO_ATTACK
                else EntryStatus.NEEDS_PROCESSING),
        weather=_log_time_weather(latitude, longitude, settings),
    )
    db_mod.save_entry(conn, entry)
    if audio_name is not None and record_id is None:
        db_mod.create_recording(conn, uuid4().hex, entry.id, audio_name)
    return JSONResponse(
        {"saved": True,
         "entry": _entry_json(entry, db_mod.list_recordings(conn, entry.id))},
        status_code=201,
    )


@router.post("/entries/{entry_id}/process")
def process(
    entry_id: str,
    latitude: Optional[float] = Form(None),
    longitude: Optional[float] = Form(None),
    manual: bool = Form(True),
    settings: Settings = Depends(get_settings),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Run transcription/extraction once. Always returns the entry: success
    moves it to `unconfirmed`, failure keeps it `needs_processing` with
    `processing_error` set for the retry queue. Running timers get a
    transcribe-only pass (the timer keeps running; extraction waits for the
    end). `manual=false` marks background passes, which skip permanent
    failures, exhausted budgets, and unelapsed backoffs."""
    entry = db_mod.get_entry(conn, entry_id)
    if entry is None:
        raise HTTPException(404, f"no entry {entry_id}")
    coords = (latitude, longitude) if latitude is not None and longitude is not None else None
    manual_mode = manual if isinstance(manual, bool) else True
    if entry.status == EntryStatus.IN_PROGRESS:
        updated = transcribe_entry_recordings(
            conn, entry, settings, manual=manual_mode)
        return _entry_json(updated, db_mod.list_recordings(conn, entry_id))
    updated = process_entry(
        conn, entry, settings, coords=coords, manual=manual_mode)
    return _entry_json(updated, db_mod.list_recordings(conn, entry_id))


@router.post("/entries/{entry_id}/transcribe")
def transcribe_entry(
    entry_id: str,
    manual: bool = Form(True),
    settings: Settings = Depends(get_settings),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Transcribe one entry's pending recordings without extracting. Returns
    the entry with per-recording state so the client can show transcripts as
    they land; safe on running timers (nothing stops, nothing resets)."""
    entry = db_mod.get_entry(conn, entry_id)
    if entry is None:
        raise HTTPException(404, f"no entry {entry_id}")
    manual_mode = manual if isinstance(manual, bool) else True
    updated = transcribe_entry_recordings(
        conn, entry, settings, manual=manual_mode)
    return _entry_json(updated, db_mod.list_recordings(conn, entry_id))


@router.post("/entries/start", status_code=201)
def start_attack(
    created_at: Optional[str] = Form(None),
    latitude: Optional[float] = Form(None),
    longitude: Optional[float] = Form(None),
    settings: Settings = Depends(get_settings),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Start the one-tap attack timer: one tap mid-attack, details later.

    Idempotent: while a timer is running, repeat taps return it instead of
    starting a second one. The start moment is stamped exact."""
    saved_at = _parse_created_at(created_at)
    active = db_mod.list_entries(conn, status=EntryStatus.IN_PROGRESS.value,
                                 entry_type=EntryType.ATTACK.value)
    if active:
        return JSONResponse({"saved": False, "entry": _entry_json(active[0])},
                            status_code=200)
    entry = Entry(
        id=uuid4().hex,
        entry_type=EntryType.ATTACK,
        created_at=saved_at,
        status=EntryStatus.IN_PROGRESS,
        attack=AttackDetails(
            attack_start=saved_at,
            attack_start_precision=TimePrecision.EXACT,
            ongoing=True,
            edited_fields=["attack_start", "ongoing"],
        ),
        weather=_log_time_weather(latitude, longitude, settings),
    )
    db_mod.save_entry(conn, entry)
    return JSONResponse({"saved": True, "entry": _entry_json(entry)},
                        status_code=201)


@router.post("/entries/{entry_id}/end")
def end_attack(
    entry_id: str,
    ended_at: Optional[str] = Form(None),
    latitude: Optional[float] = Form(None),
    longitude: Optional[float] = Form(None),
    settings: Settings = Depends(get_settings),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Stop the attack timer: stamps the end, measures the duration from the
    stamped start (phone clock on both taps), then processes inline so no
    recording waits for a manual first attempt. Transcripts and extraction
    land in the response; failures stay `needs_processing` for the retry
    queue (background recovery keeps working them)."""
    entry = db_mod.get_entry(conn, entry_id)
    if entry is None:
        raise HTTPException(404, f"no entry {entry_id}")
    if entry.status != EntryStatus.IN_PROGRESS or entry.attack is None:
        raise HTTPException(422, "only a running attack timer can be ended")
    stopped = _parse_created_at(ended_at)
    start = entry.attack.attack_start or entry.created_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=stopped.tzinfo)
    minutes = max(1, round((stopped - start).total_seconds() / 60))
    entry.attack.attack_end = stopped
    entry.attack.duration_minutes = minutes
    entry.attack.ongoing = False
    mark_edited(entry.attack, ["attack_end", "duration_minutes", "ongoing"])
    entry.status = EntryStatus.NEEDS_PROCESSING
    db_mod.save_entry(conn, entry)
    coords = (latitude, longitude) if latitude is not None and longitude is not None else None
    updated = process_entry(conn, entry, settings, coords=coords, manual=True)
    return _entry_json(updated, db_mod.list_recordings(conn, entry_id))


@router.post("/transcribe")
def transcribe(audio: UploadFile = File(...), settings: Settings = Depends(get_settings)):
    """Transcribe one voice note without saving an entry (chat voice input)."""
    if audio is None or not audio.filename:
        raise HTTPException(422, "an audio file is required")
    data = audio.file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "audio is over the 32 MB one-shot limit")
    try:
        with wave.open(BytesIO(data), "rb") as w:
            w.getnframes()
    except (wave.Error, EOFError):
        raise HTTPException(422, "audio must be a WAV file (converted on the phone)")
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        tmp.write(data)
        tmp.close()
        result = transcribe_file(
            tmp.name,
            config=load_config(cache_dir=settings.cache_dir),
        )
    except TranscribeError as exc:
        # Permanent failures (invalid audio, missing/rejected credentials)
        # are 422: retrying cannot help. Transient ones are 503: the client
        # backs off and retries the same recording.
        raise HTTPException(422 if exc.permanent else 503, str(exc)[:500])
    finally:
        try:
            Path(tmp.name).unlink(missing_ok=True)
        except OSError:
            pass
    return {"transcript": result.transcript.strip()}


@router.post("/entries/{entry_id}/answer")
def answer_followups(
    entry_id: str,
    body: AnswerBody,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Fold follow-up answers into an understood-but-unreviewed entry.

    The answers are appended to the transcript verbatim and the entry returns
    to `needs_processing`, so the next process pass extracts from the fuller
    text. One round only: the app asks once per capture.
    """
    entry = db_mod.get_entry(conn, entry_id)
    if entry is None:
        raise HTTPException(404, f"no entry {entry_id}")
    try:
        append_followup_answers(
            entry, [(item.question, item.answer) for item in body.answers]
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    db_mod.save_entry(conn, entry)
    return _entry_json(entry)


@router.post("/entries/{entry_id}/note")
def attach_note(
    entry_id: str,
    transcript: Optional[str] = Form(None),
    audio: Optional[UploadFile] = File(None),
    recording_id: Optional[str] = Form(None),
    settings: Settings = Depends(get_settings),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Attach a voice note or typed text to a running or ended timer. New
    audio is tracked as its own recording (earlier files are kept, never
    replaced) and transcribed inline without stopping a running timer; typed
    text appends to existing notes instead of replacing them. Retrying with
    the same `recording_id` returns the entry unchanged — no duplicate file,
    row, or transcript addition."""
    entry = db_mod.get_entry(conn, entry_id)
    if entry is None:
        raise HTTPException(404, f"no entry {entry_id}")
    if entry.status not in (EntryStatus.IN_PROGRESS, EntryStatus.NEEDS_PROCESSING):
        raise HTTPException(
            422, "this note is already understood; edit it in review instead")
    record_id = _clean_recording_id(recording_id)
    if record_id is not None and audio is not None and audio.filename:
        existing = db_mod.get_recording(conn, record_id)
        if existing is not None:
            if existing["entry_id"] != entry_id:
                raise HTTPException(
                    422, "this recording_id was already used for another note")
            updated = transcribe_entry_recordings(
                conn, entry, settings, manual=True)
            return _entry_json(
                updated, db_mod.list_recordings(conn, entry_id))
    text = (transcript or "").strip() or None
    audio_name: Optional[str] = None
    if audio is not None and audio.filename:
        audio_name = _store_audio(audio, settings)
    if audio_name is None and text is None:
        raise HTTPException(422, "a note needs voice audio or typed text")
    if audio_name is not None:
        entry.audio_path = audio_name
        try:
            db_mod.create_recording(
                conn, record_id or uuid4().hex, entry.id, audio_name)
        except sqlite3.IntegrityError:
            # Simultaneous retry of the same upload won elsewhere: drop our
            # duplicate file and fall through to the shared row.
            _remove_audio_file(audio_name, settings)
            entry.audio_path = db_mod.get_entry(conn, entry.id).audio_path
    if text is not None:
        base = (entry.transcript or "").rstrip()
        entry.transcript = f"{base}\n\n{text}".strip() if base else text
    entry.processing_error = None
    db_mod.save_entry(conn, entry)
    updated = transcribe_entry_recordings(conn, entry, settings, manual=True)
    return _entry_json(updated, db_mod.list_recordings(conn, entry_id))


@router.delete("/entries/{entry_id}")
def delete_entry(
    entry_id: str,
    settings: Settings = Depends(get_settings),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Delete any entry. Cannot be undone; the app confirms first."""
    entry = db_mod.get_entry(conn, entry_id)
    if entry is None:
        raise HTTPException(404, f"no entry {entry_id}")
    paths = set(db_mod.delete_recordings_for_entry(conn, entry_id))
    if entry.audio_path:
        paths.add(entry.audio_path)
    for audio_path in paths:
        _remove_audio_file(audio_path, settings)
    db_mod.delete_entry(conn, entry_id)
    return {"deleted": True}


@router.get("/entries")
def list_entries(
    status: Optional[str] = None,
    entry_type: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: int = 100,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Newest first. The retry queue is `?status=needs_processing`; the
    review queue is `?status=unconfirmed`. `start`/`end` bound entry_date
    (YYYY-MM-DD, inclusive) for month views."""
    if status is not None and status not in {s.value for s in EntryStatus}:
        raise HTTPException(422, f"unknown status {status!r}")
    if entry_type is not None and entry_type not in {t.value for t in EntryType}:
        raise HTTPException(422, f"unknown entry_type {entry_type!r}")
    try:
        start_date = date.fromisoformat(start) if start else None
        end_date = date.fromisoformat(end) if end else None
    except ValueError:
        raise HTTPException(422, "start/end must be YYYY-MM-DD dates")
    limit = max(1, min(limit, 500))
    entries = db_mod.list_entries(conn, status=status, entry_type=entry_type,
                                  start=start_date, end=end_date)[:limit]
    pending_voice_ids = db_mod.pending_recording_entry_ids(conn)
    items = []
    for entry in entries:
        item = _entry_json(entry)
        item["pending_voice"] = entry.id in pending_voice_ids
        items.append(item)
    return {"entries": items, "count": len(entries)}


@router.get("/entries/{entry_id}")
def get_entry(
    entry_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
):
    entry = db_mod.get_entry(conn, entry_id)
    if entry is None:
        raise HTTPException(404, f"no entry {entry_id}")
    return _entry_json(entry, db_mod.list_recordings(conn, entry_id))


@router.get("/summary")
def summary(
    window_days: int = 30,
    end: Optional[str] = None,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Printable-summary numbers over the trailing window ending `end`
    (default today). Every number is computed by deterministic code from
    stored entries; the model writes nothing here."""
    if window_days not in (30, 60, 90):
        raise HTTPException(422, "window_days must be 30, 60, or 90")
    if end is None:
        end_date = datetime.now().astimezone().date()
    else:
        try:
            end_date = date.fromisoformat(end)
        except ValueError:
            raise HTTPException(422, f"end is not a YYYY-MM-DD date: {end!r}")
    start_date = end_date - timedelta(days=window_days - 1)
    entries = db_mod.list_entries(conn, start=start_date, end=end_date)
    result = compute_summary(
        entries, start_date, end_date,
        generated_at=datetime.now().astimezone(),
    )
    return _summary_json(result)


@router.post("/ask")
def ask(
    body: AskBody,
    settings: Settings = Depends(get_settings),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Answer a question strictly from the diary window. The model reads a
    deterministic digest of stored entries; empty windows answer directly."""
    question = body.question.strip()
    if not question:
        raise HTTPException(422, "a question is required")
    if len(question) > 500:
        raise HTTPException(422, "the question is too long")
    if body.window_days not in (30, 60, 90):
        raise HTTPException(422, "window_days must be 30, 60, or 90")
    end_date = datetime.now().astimezone().date()
    start_date = end_date - timedelta(days=body.window_days - 1)
    entries = db_mod.list_entries(conn, start=start_date, end=end_date)
    try:
        result = assist.ask_diary(question, entries, settings)
    except (LLMError, ModelConfigError) as exc:
        raise HTTPException(500, str(exc)[:500])
    return {
        "answer": result.answer,
        "entries_consulted": result.entries_consulted,
        "window_days": body.window_days,
        "cached": result.cached,
    }


@router.post("/summary/narrative")
def summary_narrative(
    body: NarrativeBody,
    settings: Settings = Depends(get_settings),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Draft a doctor-summary paragraph from the deterministic summary. The
    numbers are computed by code; the model only phrases them."""
    if body.window_days not in (30, 60, 90):
        raise HTTPException(422, "window_days must be 30, 60, or 90")
    end_date = datetime.now().astimezone().date()
    start_date = end_date - timedelta(days=body.window_days - 1)
    entries = db_mod.list_entries(conn, start=start_date, end=end_date)
    summary = compute_summary(
        entries, start_date, end_date,
        generated_at=datetime.now().astimezone(),
    )
    try:
        result = assist.draft_window_narrative(summary, settings)
    except (LLMError, ModelConfigError) as exc:
        raise HTTPException(500, str(exc)[:500])
    return {
        "narrative": result.text,
        "window_days": body.window_days,
        "cached": result.cached,
    }


def _agent_input_hash(message: str, history: list[dict[str, str]]) -> str:
    """Identity of a turn for retry matching, over the NORMALIZED input.

    Coordinates are deliberately excluded: phone GPS jitters between calls,
    and a retry from the same spot must still match.
    """
    canonical = json.dumps(
        {"message": message, "history": history},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _agent_response(result: agent.AgentResult) -> dict[str, Any]:
    turn_status = (
        "model_error"
        if any(a.status == "model_error" for a in result.actions)
        else "ok"
    )
    return {
        "reply": result.reply,
        "actions": [
            {"tool": a.tool, "ok": a.ok, "label": a.label, "entry_id": a.entry_id,
             "followups": [dict(f) for f in a.followups],
             "data": dict(a.data), "status": a.status}
            for a in result.actions
        ],
        "state": result.state or {"timer_running": False},
        "cached": result.cached,
        "status": turn_status,
    }


@router.post("/agent")
def agent_chat(
    body: AgentBody,
    settings: Settings = Depends(get_settings),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """One chat turn with the diary assistant. The model decides; tools act.

    Safe to retry: when the client sends a stable request_id, the turn runs
    at most once — a repeat returns the stored result, an overlap gets 409
    (poll GET /agent/turns/{id}), and a reused id with different input
    gets 422. No transaction is held across model calls: the pending claim
    commits before the turn runs, completion commits after.
    """
    message = body.message.strip()
    if not message:
        raise HTTPException(422, "a message is required")
    if len(message) > 1000:
        raise HTTPException(422, "the message is too long")
    history: list[dict[str, str]] = []
    for item in body.history[-agent.MAX_HISTORY:]:
        if item.role not in ("user", "assistant"):
            raise HTTPException(422, f"bad history role {item.role!r}")
        history.append({"role": item.role, "content": item.content.strip()[:2000]})
    coords = (body.latitude, body.longitude) \
        if body.latitude is not None and body.longitude is not None else None

    request_id = (body.request_id or "").strip() or None
    if body.request_id is not None and request_id is None:
        raise HTTPException(422, "request_id must not be blank")
    if request_id is not None and len(request_id) > 128:
        raise HTTPException(422, "request_id is too long")
    agent_context = body.context.model_dump(exclude_none=True) if body.context else None
    if request_id is None:
        # Legacy client: run unclaimed, exactly like before idempotency.
        try:
            return _run_agent_turn(message, history, settings, conn, coords, replayed=False,
                                   context=agent_context)
        except ModelConfigError as exc:
            raise HTTPException(500, str(exc)[:500])
        except LLMError:
            logger.exception("legacy agent turn failed: model call failed")
            raise HTTPException(500, "The assistant is unavailable right now. Try again in a bit.")

    try:
        existing = db_mod.claim_turn(conn, request_id, _agent_input_hash(message, history))
    except db_mod.TurnInputMismatch:
        raise HTTPException(422, "this request_id was already used with different input")
    if existing is not None:
        status = existing["status"]
        if status == "completed":
            response = json.loads(existing["response_json"])
            response["replayed"] = True
            return response
        if status == "pending":
            raise HTTPException(
                409, f"turn {request_id} is still running; poll GET /agent/turns/{request_id}")
        if status == "expired":
            raise HTTPException(
                409, "this turn could not be confirmed — it may already have saved."
                      " Check History before sending again.")
        db_mod.reclaim_turn(conn, request_id)
    try:
        return _run_agent_turn(message, history, settings, conn, coords, replayed=False,
                               request_id=request_id, context=agent_context)
    except ModelConfigError as exc:
        # Setup problem (e.g. missing API key): actionable, safe to show.
        db_mod.fail_turn(conn, request_id)
        raise HTTPException(500, str(exc)[:500])
    except LLMError:
        # Raised only before any tool ran (zero actions), so a retry is safe.
        # Internal details stay in the server log, not the user message.
        logger.exception("agent turn %s failed: model call failed", request_id)
        db_mod.fail_turn(conn, request_id)
        raise HTTPException(500, "The assistant is unavailable right now. Try again in a bit.")
    except Exception:
        # Unknown failure mid-turn: writes may have happened. Expire rather
        # than fail so a retry can never save twice (409-expired instead).
        logger.exception("agent turn %s failed unexpectedly", request_id)
        db_mod.expire_turn(conn, request_id)
        raise


def _run_agent_turn(message, history, settings, conn, coords, replayed, request_id=None,
                    context=None):
    result = agent.run_agent(message, history, settings, conn, coords=coords, context=context)
    response = _agent_response(result)
    response["replayed"] = replayed
    if request_id is not None:
        db_mod.complete_turn(conn, request_id, response)
    return response


@router.get("/agent/turns/{request_id}")
def agent_turn_status(
    request_id: str,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Poll a turn claimed by POST /agent: pending, completed (with the
    stored response), failed, or expired. Unknown ids are 404."""
    turn = db_mod.get_turn(conn, request_id)
    if turn is None:
        raise HTTPException(404, f"unknown request_id {request_id!r}")
    body: dict[str, Any] = {"request_id": request_id, "status": turn["status"]}
    if turn["status"] == "completed" and turn["response_json"]:
        response = json.loads(turn["response_json"])
        response["replayed"] = True
        body["response"] = response
    return body


@router.patch("/entries/{entry_id}")
def update_entry(
    entry_id: str,
    body: PatchBody,
    settings: Settings = Depends(get_settings),
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Apply user corrections and/or confirm. Edited values replace extracted
    ones (the user is now the source, so stale evidence is dropped); confirming
    a still-unprocessed entry is rejected rather than guessed."""
    entry = db_mod.get_entry(conn, entry_id)
    if entry is None:
        raise HTTPException(404, f"no entry {entry_id}")
    if body.attack is not None:
        if entry.attack is None:
            raise HTTPException(422, "check-ins carry no attack details to edit")
        entry.attack = _apply_attack_patch(entry.attack, body.attack, entry.created_at)
    if body.confirm:
        if entry.status == EntryStatus.NEEDS_PROCESSING:
            raise HTTPException(422, "process (or retry) this entry before confirming it")
        if entry.status == EntryStatus.IN_PROGRESS:
            raise HTTPException(422, "end the attack timer before confirming it")
        entry.status = EntryStatus.CONFIRMED
    db_mod.save_entry(conn, entry)
    return _entry_json(entry)


def _summary_json(summary: Summary) -> dict[str, Any]:
    return {
        "window_start": summary.window_start.isoformat(),
        "window_end": summary.window_end.isoformat(),
        "generated_at": summary.generated_at.isoformat(),
        "attack_days": summary.attack_days,
        "no_attack_days": summary.no_attack_days,
        "not_logged_days": summary.not_logged_days,
        "attack_entries": summary.attack_entries,
        "unconfirmed_entries": summary.unconfirmed_entries,
        "calendar": [
            {"day": day.day.isoformat(), "state": day.state,
             "unconfirmed": day.unconfirmed}
            for day in summary.calendar
        ],
        "duration": {
            "n": summary.duration.n,
            "median_minutes": summary.duration.median_minutes,
            "min_minutes": summary.duration.min_minutes,
            "max_minutes": summary.duration.max_minutes,
        },
        "severity_counts": {str(k): v for k, v in summary.severity_counts.items()},
        "severity_n": summary.severity_n,
        "symptoms": dict(summary.symptoms),
        "symptoms_other": dict(summary.symptoms_other),
        "symptoms_n": summary.symptoms_n,
        "medications": [
            {"display_name": m.display_name, "attack_entries": m.attack_entries,
             "distinct_days": m.distinct_days, "outcomes": dict(m.outcomes)}
            for m in summary.medications
        ],
        "triggers": dict(summary.triggers),
        "triggers_n": summary.triggers_n,
        "pain": {
            "mean": (round(summary.pain_mean, 1)
                     if summary.pain_mean is not None else None),
            "n": summary.pain_n,
        },
        "weather": {
            "n": summary.weather_n,
            "tendency_n": summary.weather_tendency_n,
            "falling_n": summary.weather_falling_n,
        },
    }


def _parse_created_at(raw: Optional[str]) -> datetime:
    if not raw:
        return datetime.now().astimezone()
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(422, f"created_at is not ISO 8601: {raw!r}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def _remove_audio_file(audio_path: Optional[str], settings: Settings) -> None:
    """Best-effort removal of a replaced or discarded note's audio file."""
    if not audio_path:
        return
    try:
        (Path(settings.audio_dir) / Path(audio_path).name).unlink(missing_ok=True)
    except OSError:
        pass


def _store_audio(audio: UploadFile, settings: Settings) -> str:
    data = audio.file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "audio is over the 32 MB one-shot limit")
    try:
        with wave.open(BytesIO(data), "rb") as w:
            w.getnframes()
    except (wave.Error, EOFError):
        raise HTTPException(422, "audio must be a WAV file (converted in the browser)")
    name = f"{uuid4().hex}.wav"
    dest = Path(settings.audio_dir) / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return name


def _apply_attack_patch(
    attack: AttackDetails, patch: dict[str, Any], created_at: datetime
) -> AttackDetails:
    """Merge user edits over extracted details. Unknown keys are rejected."""
    known = set(AttackDetails.model_fields) - {"edited_fields"}
    unknown = set(patch) - known - {"attack_start"}
    if unknown:
        raise HTTPException(422, f"unknown attack fields: {sorted(unknown)}")
    data = attack.model_dump(mode="json")
    edited_paths: list[str] = []
    for key, value in patch.items():
        if key == "attack_start":
            continue  # handled with time_expression below
        if key == "symptoms" and isinstance(value, dict):
            for symptom, flag in value.items():
                if symptom not in data["symptoms"]:
                    raise HTTPException(422, f"unknown symptom {symptom!r}")
                data["symptoms"][symptom] = flag
                edited_paths.append(f"symptoms.{symptom}")
        else:
            data[key] = value
            edited_paths.append(key)
    if "time_expression" in patch:
        start, precision = resolve_attack_start(data.get("time_expression"), created_at)
        data["attack_start"] = start.isoformat() if start else None
        data["attack_start_precision"] = precision.value
        edited_paths.append("attack_start")
        if "duration_minutes" not in patch and data.get("duration_minutes") is None:
            ranged = resolve_duration_range(data.get("time_expression"))
            if ranged is not None:
                data["duration_minutes"] = ranged
                edited_paths.append("duration_minutes")
    elif "attack_start" in patch:
        edited_paths.append("attack_start")
        raw = patch["attack_start"]
        if raw in (None, ""):
            data["attack_start"] = None
            data["attack_start_precision"] = "unknown"
        else:
            try:
                data["attack_start"] = datetime.fromisoformat(raw).isoformat()
            except ValueError:
                raise HTTPException(422, f"attack_start is not ISO 8601: {raw!r}")
            data["attack_start_precision"] = "exact"
    try:
        merged = AttackDetails.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(422, f"invalid attack details: {exc.errors()[0]['msg']}")
    for path in edited_paths:
        merged.evidence.pop(path, None)
        if path in merged.unsupported_fields:
            merged.unsupported_fields.remove(path)
    mark_edited(merged, edited_paths)
    return merged
