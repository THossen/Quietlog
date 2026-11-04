"""Save-first processing pipeline (M3).

Capture writes the entry immediately; this module runs afterward (and again
on every retry) in two separated phases. Transcription folds each recording's
transcript into the entry the moment it arrives; structured extraction then
runs over the full text with the quote verifier. Extraction failure never
costs a retranscription: persisted transcripts stay persisted. Any failure
leaves the saved entry waiting in the retry queue (`needs_processing`) with
`processing_error` naming the blocking problem, so nothing is ever lost.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Optional

from . import db as db_mod
from . import llm_service, transcribe_service, weather_service
from .models import AttackDetails, Entry
from .settings import Settings
from .transcribe_service import TranscribeError
from .types import EntryStatus, EntryType

__all__ = [
    "append_followup_answers",
    "process_entry",
    "transcribe_entry_recordings",
]

_locks_guard = threading.Lock()
_entry_locks: dict[str, threading.RLock] = {}


def _entry_lock(entry_id: str) -> threading.RLock:
    """Per-entry pass lock: overlapping automatic and manual attempts on the
    same entry serialize instead of interleaving. Reentrant: process_entry
    runs the transcription phase while holding it."""
    with _locks_guard:
        lock = _entry_locks.get(entry_id)
        if lock is None:
            lock = threading.RLock()
            _entry_locks[entry_id] = lock
        return lock


def append_followup_answers(entry: Entry, pairs: list[tuple[str, str]]) -> None:
    """Fold Q/A pairs into the transcript and requeue for processing.

    Raises ValueError when the entry is not awaiting review or the pairs
    are blank, too long, or too many. The caller saves the entry.
    """
    if entry.status != EntryStatus.UNCONFIRMED:
        raise ValueError("answers apply to an understood note awaiting review")
    cleaned: list[tuple[str, str]] = []
    for question, answer in pairs:
        q, a = question.strip(), answer.strip()
        if not q or not a:
            raise ValueError("each answer needs a question and a non-blank answer")
        if len(q) > 500 or len(a) > 1000:
            raise ValueError("question or answer is too long")
        cleaned.append((q, a))
    if not cleaned:
        raise ValueError("at least one answer is required")
    if len(cleaned) > 3:
        raise ValueError("at most three answers per round")
    block = "\n".join(f"Q: {q}\nA: {a}" for q, a in cleaned)
    entry.transcript = (
        (entry.transcript or "").rstrip() + "\n\nFollow-up answers:\n" + block
    ).strip()
    entry.status = EntryStatus.NEEDS_PROCESSING
    entry.processing_error = None


def _restore_edited_fields(
    previous: AttackDetails | None, fresh: AttackDetails
) -> None:
    """Keep user-set fields across extraction.

    Review edits and timer stamps are user actions, so they win over
    anything guessed from words (the user is the source of truth, and
    stale evidence for restored fields is dropped). Fields the user never
    touched follow the fresh extraction — including old extraction guesses,
    so corrected words ("actually it started later") take effect. The
    edited-fields tracking itself carries forward.
    """
    if previous is None:
        return
    fresh.edited_fields = sorted(set(previous.edited_fields))
    for path in previous.edited_fields:
        if path.startswith("symptoms."):
            name = path[len("symptoms."):]
            if hasattr(previous.symptoms, name):
                setattr(fresh.symptoms, name, getattr(previous.symptoms, name))
        elif hasattr(AttackDetails, path) or hasattr(previous, path):
            setattr(fresh, path, getattr(previous, path))
        else:
            continue
        if path == "attack_start":
            fresh.attack_start_precision = previous.attack_start_precision
        fresh.evidence.pop(path, None)
        if path in fresh.unsupported_fields:
            fresh.unsupported_fields.remove(path)


def _append_block(existing: Optional[str], addition: str) -> str:
    base = (existing or "").rstrip()
    return f"{base}\n\n{addition}".strip() if base else addition.strip()


def _adopt_legacy_audio(conn, entry: Entry) -> None:
    """Track a pre-recordings `audio_path` as a recording row so it flows
    through the same transcribe-once path as new uploads. Deterministic id
    per file: adoption is idempotent."""
    if not entry.audio_path:
        return
    if db_mod.recording_for_audio(conn, entry.id, entry.audio_path):
        return
    digest = hashlib.sha256(entry.audio_path.encode("utf-8")).hexdigest()[:8]
    recording_id = f"legacy-{entry.id[:8]}-{digest}"
    try:
        db_mod.create_recording(conn, recording_id, entry.id, entry.audio_path)
    except sqlite3.IntegrityError:
        pass  # adopted already


def _attempt_transcription(
    conn,
    recording: dict,
    settings: Settings,
    transcribe_fn: Optional[Callable[[str], str]],
) -> Optional[str]:
    """Transcribe one claimed recording. Returns the transcript, or None
    after recording the classified failure on the row (never raises for
    model/audio problems)."""
    recording_id = recording["id"]
    audio_file = Path(settings.audio_dir) / Path(recording["audio_path"]).name
    if not audio_file.exists():
        db_mod.fail_recording(
            conn, recording_id,
            f"audio file is missing: {recording['audio_path']}",
            permanent=True,
        )
        return None
    try:
        if transcribe_fn is None:
            result = transcribe_service.transcribe_file(
                audio_file,
                config=transcribe_service.load_config(
                    cache_dir=settings.cache_dir),
            )
            text = (result.transcript or "").strip() or None
        else:
            text = (transcribe_fn(str(audio_file)) or "").strip() or None
    except TranscribeError as exc:
        db_mod.fail_recording(
            conn, recording_id, f"{type(exc).__name__}: {exc}"[:500],
            permanent=exc.permanent,
        )
        return None
    except Exception as exc:  # noqa: BLE001 - unexpected failures stay bounded data
        db_mod.fail_recording(
            conn, recording_id, f"{type(exc).__name__}: {exc}"[:500],
            permanent=False,
        )
        return None
    if text is None:
        # Silence (or an empty model reply) will not change on retry.
        db_mod.fail_recording(
            conn, recording_id,
            "Transcription came back empty: nothing heard to save.",
            permanent=True,
        )
        return None
    return text


def _transcribe_blocker(conn, entry_id: str) -> Optional[str]:
    """The entry-level error when transcription cannot proceed further, else
    None while work remains (pending, claimed, or retryable)."""
    rows = db_mod.list_recordings(conn, entry_id)
    if not rows:
        return None
    for row in rows:
        if row["status"] == "transcribing":
            return None
        if row["status"] == "pending":
            if (row["attempts"] or 0) < db_mod.MAX_AUTO_ATTEMPTS:
                return None
            continue  # budget exhausted: terminal, surfaced below
        if (
            row["status"] == "failed"
            and row["failure_kind"] == "transient"
            and (row["attempts"] or 0) < db_mod.MAX_AUTO_ATTEMPTS
        ):
            return None
    errors = [row["error"] for row in rows
              if row["status"] == "failed" and row["error"]]
    if errors:
        return errors[0]
    if any(row["status"] == "pending" for row in rows):
        return ("Transcription is still failing after several attempts;"
                " check the server and retry manually.")
    return None


def transcribe_entry_recordings(
    conn,
    entry: Entry,
    settings: Settings,
    *,
    transcribe_fn: Optional[Callable[[str], str]] = None,
    manual: bool = False,
) -> Entry:
    """Run the transcription phase over an entry's recordings. Returns the
    stored entry.

    Each untranscribed recording is claimed (so overlapping passes never
    transcribe twice), transcribed, persisted immediately, and folded into
    entry.transcript exactly once. Never touches timer state, attack details,
    or entry status; confirmed history is never rewritten. `manual`
    distinguishes user-driven passes (retry anything untranscribed) from
    background passes (skip permanent failures, exhausted budgets, and
    unelapsed backoffs).
    """
    if entry.entry_type != EntryType.ATTACK:
        return entry  # check-ins have nothing to transcribe
    with _entry_lock(entry.id):
        fresh = db_mod.get_entry(conn, entry.id)
        if fresh is None:
            return entry  # deleted (or never saved): never recreate it
        if fresh.status == EntryStatus.CONFIRMED:
            return fresh
        _adopt_legacy_audio(conn, fresh)
        pending_appends: list[tuple[str, str]] = []
        for row in db_mod.list_recordings(conn, fresh.id):
            if row["status"] == "transcribed":
                if not row["appended"] and (row["transcript"] or "").strip():
                    pending_appends.append(
                        (row["id"], row["transcript"].strip()))
                continue
            claimed = db_mod.claim_recording(conn, row["id"], manual)
            if claimed is None:
                continue
            text = _attempt_transcription(conn, claimed, settings, transcribe_fn)
            if text is not None:
                db_mod.complete_recording(conn, claimed["id"], text)
                pending_appends.append((claimed["id"], text))
        try:
            for recording_id, text in pending_appends:
                if db_mod.mark_recording_appended(
                        conn, recording_id, commit=False):
                    fresh.transcript = _append_block(fresh.transcript, text)
            fresh.processing_error = _transcribe_blocker(conn, fresh.id)
            if db_mod.get_entry(conn, fresh.id) is None:
                conn.rollback()  # deleted mid-pass: drop the marks, keep it gone
                return fresh
            return db_mod.save_entry(conn, fresh)
        except Exception:
            conn.rollback()
            raise


def process_entry(
    conn,
    entry: Entry,
    settings: Settings,
    *,
    transcribe_fn: Optional[Callable[[str], str]] = None,
    extract_fn=None,
    coords: Optional[tuple[float, float]] = None,
    manual: bool = False,
) -> Entry:
    """Run one processing pass over a saved entry. Returns the stored entry.

    `transcribe_fn(audio_path) -> transcript` and
    `extract_fn(transcript, reference_time) -> AttackDetails` default to the
    real Meta Model API services; tests inject fakes. `coords` is the phone's
    location at process time, used only for a weather snapshot and never
    stored. `manual` marks user-driven passes for transcription backoff
    purposes (see transcribe_entry_recordings). The entry is saved before
    returning in every outcome.
    """
    if entry.entry_type != EntryType.ATTACK:
        return entry  # check-ins have nothing to extract
    if entry.status == EntryStatus.CONFIRMED:
        return entry  # confirmed history is never rewritten

    with _entry_lock(entry.id):
        entry = transcribe_entry_recordings(
            conn, entry, settings, transcribe_fn=transcribe_fn, manual=manual)
        if db_mod.get_entry(conn, entry.id) is None:
            return entry  # deleted mid-pass: never recreate it
        transcript = (entry.transcript or "").strip() or None
        if transcript is None:
            if entry.status != EntryStatus.IN_PROGRESS:
                entry.status = EntryStatus.NEEDS_PROCESSING
            rows = db_mod.list_recordings(conn, entry.id)
            if not rows and not entry.audio_path:
                entry.processing_error = (
                    "ValueError: no transcript and no audio to transcribe")
            # Otherwise the transcription phase already set the honest error
            # (or None while recordings are still being worked).
            return db_mod.save_entry(conn, entry)

        try:
            if extract_fn is None:
                attack = llm_service.extract_attack(
                    transcript,
                    reference_time=entry.created_at,
                    config=llm_service.load_config(cache_dir=settings.cache_dir),
                ).attack
            else:
                attack = extract_fn(transcript, entry.created_at)

            _restore_edited_fields(entry.attack, attack)
            entry.attack = attack
            entry.status = EntryStatus.UNCONFIRMED
            entry.processing_error = None
        except Exception as exc:  # noqa: BLE001 - every failure is retryable data
            entry.status = EntryStatus.NEEDS_PROCESSING
            entry.processing_error = f"{type(exc).__name__}: {exc}"[:500]
            if db_mod.get_entry(conn, entry.id) is None:
                return entry  # deleted during extraction: keep it gone
            return db_mod.save_entry(conn, entry)
        else:
            if db_mod.get_entry(conn, entry.id) is None:
                return entry  # deleted during extraction: keep it gone
            # Backdated attacks get historical weather for their own hour. This
            # runs outside the try: weather is fail-soft and must never flip a
            # successful extraction back to needs_processing.
            if coords is not None and settings.weather_enabled:
                try:
                    entry.weather = weather_service.refresh_for_attack(
                        entry.weather,
                        coords[0],
                        coords[1],
                        entry.attack.attack_start if entry.attack else None,
                        timeout=settings.weather_timeout,
                    )
                except Exception:  # noqa: BLE001 - keep whatever snapshot exists
                    pass
        return db_mod.save_entry(conn, entry)
