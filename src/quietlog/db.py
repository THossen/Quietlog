"""SQLite persistence for Quietlog entries (stdlib sqlite3, no ORM).

One table, one row per saved entry. Attack details are stored as a JSON
document so the schema stays stable while extraction evolves. All dates are
stored as ISO strings; created_at is normalized to UTC so text ordering is
chronological (mixed offsets would mis-sort; see _normalize_created_at).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from .models import AttackDetails, Entry, WeatherSnapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id TEXT PRIMARY KEY,
    entry_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    entry_date TEXT NOT NULL,
    audio_path TEXT,
    transcript TEXT,
    status TEXT NOT NULL,
    attack_json TEXT,
    processing_error TEXT,
    weather_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_entries_date ON entries (entry_date);
CREATE INDEX IF NOT EXISTS idx_entries_type ON entries (entry_type);
CREATE TABLE IF NOT EXISTS agent_turns (
    request_id TEXT PRIMARY KEY,
    input_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    response_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recordings (
    id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL,
    audio_path TEXT NOT NULL,
    status TEXT NOT NULL,
    failure_kind TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    error TEXT,
    transcript TEXT,
    appended INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recordings_entry ON recordings (entry_id);
"""

# A turn claimed but never completed (crash, lost response) stops blocking
# retries after this long. Checked lazily on access; no reaper thread.
TURN_TTL_SECONDS = 600


class TurnInputMismatch(Exception):
    """Same request_id reused with different input: reject, never replay."""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open the database, creating parent dirs and tables as needed."""
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    # FastAPI runs sync dependencies in a worker threadpool and may finalize
    # get_conn on a DIFFERENT thread than the one that opened it, so the
    # same-thread check must stay off (each request still gets its own
    # connection, used sequentially, never shared concurrently).
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(entries)")}
    if "weather_json" not in columns:
        conn.execute("ALTER TABLE entries ADD COLUMN weather_json TEXT")
    _normalize_created_at(conn)
    return conn


def _normalize_created_at(conn: sqlite3.Connection) -> None:
    """Repair pre-UTC rows: rewrite created_at in canonical UTC ISO text.

    Rows written before the UTC rule mix +00:00 and local offsets, and
    ORDER BY on mixed-offset text is not chronological. Same instants, same
    dates — only the text changes. Idempotent; unparseable rows are left
    alone so a bad row can never break connect().
    """
    rows = conn.execute(
        "SELECT id, created_at FROM entries WHERE created_at NOT LIKE '%+00:00'"
    ).fetchall()
    for row in rows:
        try:
            parsed = datetime.fromisoformat(row["created_at"])
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
        conn.execute(
            "UPDATE entries SET created_at = ? WHERE id = ?",
            (parsed.astimezone(timezone.utc).isoformat(), row["id"]),
        )
    conn.commit()


def _row_to_entry(row: sqlite3.Row) -> Entry:
    attack = None
    if row["attack_json"]:
        attack = AttackDetails.model_validate(json.loads(row["attack_json"]))
    weather = None
    try:
        raw_weather = row["weather_json"]
    except IndexError:
        raw_weather = None
    if raw_weather:
        weather = WeatherSnapshot.model_validate(json.loads(raw_weather))
    return Entry(
        id=row["id"],
        entry_type=row["entry_type"],
        created_at=row["created_at"],
        entry_date=row["entry_date"],
        audio_path=row["audio_path"],
        transcript=row["transcript"],
        status=row["status"],
        attack=attack,
        processing_error=row["processing_error"],
        weather=weather,
    )


def save_entry(conn: sqlite3.Connection, entry: Entry) -> Entry:
    """Insert or replace one entry. Returns the entry as stored."""
    created = entry.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=datetime.now().astimezone().tzinfo)
    entry.created_at = created.astimezone(timezone.utc)
    conn.execute(
        """
        INSERT INTO entries
            (id, entry_type, created_at, entry_date, audio_path, transcript,
             status, attack_json, processing_error, weather_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            entry_type = excluded.entry_type,
            created_at = excluded.created_at,
            entry_date = excluded.entry_date,
            audio_path = excluded.audio_path,
            transcript = excluded.transcript,
            status = excluded.status,
            attack_json = excluded.attack_json,
            processing_error = excluded.processing_error,
            weather_json = excluded.weather_json
        """,
        (
            entry.id,
            entry.entry_type.value,
            entry.created_at.isoformat(),
            entry.entry_date.isoformat() if entry.entry_date else None,
            entry.audio_path,
            entry.transcript,
            entry.status.value,
            entry.attack.model_dump_json() if entry.attack else None,
            entry.processing_error,
            entry.weather.model_dump_json() if entry.weather else None,
        ),
    )
    conn.commit()
    return entry


def get_entry(conn: sqlite3.Connection, entry_id: str) -> Optional[Entry]:
    row = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    return _row_to_entry(row) if row else None


def list_entries(
    conn: sqlite3.Connection,
    *,
    start: Optional[date] = None,
    end: Optional[date] = None,
    entry_type: Optional[str] = None,
    status: Optional[str] = None,
) -> list[Entry]:
    """List entries, newest first, with optional inclusive date-range filter."""
    clauses: list[str] = []
    params: list[str] = []
    if start is not None:
        clauses.append("entry_date >= ?")
        params.append(start.isoformat())
    if end is not None:
        clauses.append("entry_date <= ?")
        params.append(end.isoformat())
    if entry_type is not None:
        clauses.append("entry_type = ?")
        params.append(entry_type)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    sql = "SELECT * FROM entries"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC"
    return [_row_to_entry(r) for r in conn.execute(sql, params)]


def delete_entry(conn: sqlite3.Connection, entry_id: str) -> bool:
    cur = conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    conn.commit()
    return cur.rowcount > 0


def delete_all(conn: sqlite3.Connection) -> int:
    """Delete every entry. Backs the SHOULD-tier delete-all-my-data control."""
    cur = conn.execute("DELETE FROM entries")
    conn.commit()
    return cur.rowcount


def count_entries(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM entries").fetchone()
    return int(row["n"])


def save_many(conn: sqlite3.Connection, entries: Iterable[Entry]) -> int:
    n = 0
    for entry in entries:
        save_entry(conn, entry)
        n += 1
    return n


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_turn(conn: sqlite3.Connection, request_id: str) -> Optional[dict]:
    """Latest known state of a turn, or None. Lazily expires stale pending."""
    row = conn.execute(
        "SELECT request_id, input_hash, status, response_json, created_at, updated_at"
        " FROM agent_turns WHERE request_id = ?",
        (request_id,),
    ).fetchone()
    if row is None:
        return None
    turn = dict(row)
    if turn["status"] == "pending" and _turn_age_seconds(turn) > TURN_TTL_SECONDS:
        conn.execute(
            "UPDATE agent_turns SET status = 'expired', updated_at = ?"
            " WHERE request_id = ? AND status = 'pending'",
            (_utcnow_iso(), request_id),
        )
        conn.commit()
        turn["status"] = "expired"
    return turn


def _turn_age_seconds(turn: dict) -> float:
    try:
        updated = datetime.fromisoformat(turn["updated_at"])
    except ValueError:
        return 0.0
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - updated).total_seconds()


def claim_turn(conn: sqlite3.Connection, request_id: str, input_hash: str) -> Optional[dict]:
    """Atomically claim a request_id for this caller.

    Returns None when the claim succeeded (caller must run the turn, then
    complete_turn/fail_turn it). Otherwise returns the existing turn row:
    completed (replay it), pending (another request is running it), failed
    (reclaim_turn, then run), expired (tell the user to check History;
    never re-run, it may already have written).
    Raises TurnInputMismatch when the id was used with different input.
    Commits before returning; never holds a transaction over model calls.
    """
    now = _utcnow_iso()
    try:
        conn.execute(
            "INSERT INTO agent_turns"
            " (request_id, input_hash, status, response_json, created_at, updated_at)"
            " VALUES (?, ?, 'pending', NULL, ?, ?)",
            (request_id, input_hash, now, now),
        )
        conn.commit()
        return None
    except sqlite3.IntegrityError:
        pass
    turn = get_turn(conn, request_id)
    assert turn is not None  # just conflicted on it
    if turn["input_hash"] != input_hash:
        raise TurnInputMismatch(
            f"request_id {request_id!r} was already used with different input"
        )
    return turn


def reclaim_turn(conn: sqlite3.Connection, request_id: str) -> None:
    """Reset a failed turn to pending so a retry may run it.

    Only failed turns are reclaimable: failed means the turn raised before
    completing, while expired means it may have written (response lost), so
    re-running an expired turn could save twice. Expired turns stay expired;
    the client tells the user to check History instead of resending.
    """
    conn.execute(
        "UPDATE agent_turns SET status = 'pending', response_json = NULL,"
        " updated_at = ? WHERE request_id = ? AND status = 'failed'",
        (_utcnow_iso(), request_id),
    )
    conn.commit()


def complete_turn(conn: sqlite3.Connection, request_id: str, response: dict) -> None:
    conn.execute(
        "UPDATE agent_turns SET status = 'completed', response_json = ?,"
        " updated_at = ? WHERE request_id = ?",
        (json.dumps(response), _utcnow_iso(), request_id),
    )
    conn.commit()


def expire_turn(conn: sqlite3.Connection, request_id: str) -> None:
    """Mark a pending turn expired: something unexpected happened mid-turn,
    so writes may have occurred and re-running could save twice. Retries
    get 409-expired (check History) instead of a re-run."""
    conn.execute(
        "UPDATE agent_turns SET status = 'expired', updated_at = ?"
        " WHERE request_id = ? AND status = 'pending'",
        (_utcnow_iso(), request_id),
    )
    conn.commit()


def fail_turn(conn: sqlite3.Connection, request_id: str) -> None:
    """Mark a pending turn failed so the next retry with its id runs again."""
    conn.execute(
        "UPDATE agent_turns SET status = 'failed', updated_at = ?"
        " WHERE request_id = ? AND status = 'pending'",
        (_utcnow_iso(), request_id),
    )
    conn.commit()


# --- voice recordings --------------------------------------------------------

# Automatic (background) transcription stops touching a recording after this
# many attempts; deliberate manual retries always get one more try.
MAX_AUTO_ATTEMPTS = 5

# A transcribing claim older than this is treated as a crashed worker and may
# be reclaimed. Fresh claims are left alone so overlapping passes never
# transcribe twice.
CLAIM_STALE_SECONDS = 600


def create_recording(
    conn: sqlite3.Connection, recording_id: str, entry_id: str, audio_path: str
) -> dict:
    """Insert one pending recording. Raises sqlite3.IntegrityError when the
    id already exists (a retried upload: fetch the existing row instead)."""
    now = _utcnow_iso()
    conn.execute(
        "INSERT INTO recordings"
        " (id, entry_id, audio_path, status, attempts, appended,"
        " created_at, updated_at)"
        " VALUES (?, ?, ?, 'pending', 0, 0, ?, ?)",
        (recording_id, entry_id, audio_path, now, now),
    )
    conn.commit()
    row = get_recording(conn, recording_id)
    assert row is not None
    return row


def get_recording(conn: sqlite3.Connection, recording_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM recordings WHERE id = ?", (recording_id,)).fetchone()
    return dict(row) if row else None


def list_recordings(conn: sqlite3.Connection, entry_id: str) -> list[dict]:
    """An entry's recordings, oldest first (transcript append order)."""
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM recordings WHERE entry_id = ?"
            " ORDER BY created_at, id",
            (entry_id,),
        )
    ]


def pending_recording_entry_ids(conn: sqlite3.Connection) -> set[str]:
    """Entries with untranscribed (or auto-retryable) recordings, in any
    status. One query so the background sweep can flag list responses
    without an N+1."""
    rows = conn.execute(
        "SELECT DISTINCT entry_id FROM recordings"
        " WHERE status IN ('pending', 'transcribing')"
        " OR (status = 'failed' AND failure_kind = 'transient')"
    )
    return {row["entry_id"] for row in rows}


def recording_for_audio(
    conn: sqlite3.Connection, entry_id: str, audio_path: str
) -> Optional[dict]:
    """The row already tracking this file, if any (legacy adoption check)."""
    row = conn.execute(
        "SELECT * FROM recordings WHERE entry_id = ? AND audio_path = ?",
        (entry_id, audio_path),
    ).fetchone()
    return dict(row) if row else None


def _backoff_due(row: dict) -> bool:
    if not row["next_attempt_at"]:
        return True
    try:
        due = datetime.fromisoformat(row["next_attempt_at"])
    except ValueError:
        return True
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= due


def claim_recording(
    conn: sqlite3.Connection, recording_id: str, manual: bool
) -> Optional[dict]:
    """Atomically claim a recording for one transcription attempt.

    Manual (user-driven) passes may attempt anything untranscribed except a
    fresh claim. Automatic (background) passes additionally skip permanent
    failures, exhausted attempt budgets, and unelapsed backoffs. Returns the
    claimed row, or None when another pass holds it or it is not eligible.
    The claim commits before returning; model calls happen after.
    """
    row = get_recording(conn, recording_id)
    if row is None or row["status"] == "transcribed":
        return None
    if row["status"] == "transcribing" and not _claim_stale(row):
        return None
    if not manual:
        if row["status"] == "failed" and row["failure_kind"] == "permanent":
            return None
        # The budget bounds total attempts, including crash residue (a claim
        # that died without completing leaves attempts incremented).
        if (row["attempts"] or 0) >= MAX_AUTO_ATTEMPTS:
            return None
        if row["status"] == "failed" and not _backoff_due(row):
            return None
    now = _utcnow_iso()
    cutoff = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - CLAIM_STALE_SECONDS,
        tz=timezone.utc,
    ).isoformat()
    cur = conn.execute(
        "UPDATE recordings SET status = 'transcribing', attempts = attempts + 1,"
        " updated_at = ? WHERE id = ?"
        " AND (status IN ('pending', 'failed')"
        " OR (status = 'transcribing' AND updated_at < ?))",
        (now, recording_id, cutoff),
    )
    conn.commit()
    if cur.rowcount != 1:
        return None  # lost the race: another pass claimed it first
    claimed = get_recording(conn, recording_id)
    assert claimed is not None
    return claimed


def _claim_stale(row: dict) -> bool:
    try:
        updated = datetime.fromisoformat(row["updated_at"])
    except (ValueError, TypeError):
        return True
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - updated).total_seconds()
    return age > CLAIM_STALE_SECONDS


def complete_recording(
    conn: sqlite3.Connection, recording_id: str, transcript: str
) -> None:
    """Persist a transcript the moment it arrives (extraction may still fail
    later; that must never cost a retranscription). Commits immediately."""
    conn.execute(
        "UPDATE recordings SET status = 'transcribed', transcript = ?,"
        " error = NULL, failure_kind = NULL, updated_at = ? WHERE id = ?",
        (transcript, _utcnow_iso(), recording_id),
    )
    conn.commit()


def fail_recording(
    conn: sqlite3.Connection, recording_id: str, error: str, permanent: bool
) -> None:
    """Record a failed attempt. Transient failures get an exponential backoff
    (5s doubling to 5min); permanent ones never auto-retry. Commits."""
    row = get_recording(conn, recording_id)
    attempts = row["attempts"] if row else 0
    if permanent:
        next_attempt = None
        kind = "permanent"
    else:
        delay = min(300, 5 * 2 ** max(0, attempts - 1))
        next_attempt = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() + delay,
            tz=timezone.utc,
        ).isoformat()
        kind = "transient"
    conn.execute(
        "UPDATE recordings SET status = 'failed', failure_kind = ?, error = ?,"
        " next_attempt_at = ?, updated_at = ? WHERE id = ?",
        (kind, error[:500], next_attempt, _utcnow_iso(), recording_id),
    )
    conn.commit()


def mark_recording_appended(
    conn: sqlite3.Connection, recording_id: str, *, commit: bool = True
) -> bool:
    """Mark a transcript folded into entry.transcript, at most once. Returns
    False when another pass already marked it (skip the append). With
    commit=False the mark joins the caller's transaction so the mark and the
    entry save commit atomically."""
    cur = conn.execute(
        "UPDATE recordings SET appended = 1, updated_at = ?"
        " WHERE id = ? AND appended = 0",
        (_utcnow_iso(), recording_id),
    )
    if commit:
        conn.commit()
    return cur.rowcount == 1


def reset_inflight_recordings(conn: sqlite3.Connection) -> int:
    """Crash recovery: stuck transcribing claims go back to pending. Runs
    once at server startup (a dead worker never completes its claim)."""
    cur = conn.execute(
        "UPDATE recordings SET status = 'pending', updated_at = ?"
        " WHERE status = 'transcribing'",
        (_utcnow_iso(),),
    )
    conn.commit()
    return cur.rowcount


def delete_recording(conn: sqlite3.Connection, recording_id: str) -> None:
    """Delete one recording row (orphaned claims only; entries keep theirs)."""
    conn.execute("DELETE FROM recordings WHERE id = ?", (recording_id,))
    conn.commit()


def delete_recordings_for_entry(
    conn: sqlite3.Connection, entry_id: str
) -> list[str]:
    """Delete an entry's recording rows, returning their audio paths so the
    caller can remove the files too."""
    paths = [
        row["audio_path"]
        for row in conn.execute(
            "SELECT audio_path FROM recordings WHERE entry_id = ?", (entry_id,))
    ]
    conn.execute("DELETE FROM recordings WHERE entry_id = ?", (entry_id,))
    conn.commit()
    return paths
