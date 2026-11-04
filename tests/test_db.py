"""SQLite round-trip: nothing saved is ever lost."""

from datetime import datetime, timezone

from quietlog import db
from quietlog.models import AttackDetails, Entry, EntryStatus, EntryType


def _attack(entry_id="e1", day=24, **kw):
    params = {"time_expression": "at 2pm", "severity_function": 2}
    params.update(kw)
    attack = AttackDetails(**params)
    return Entry(id=entry_id, entry_type=EntryType.ATTACK,
                 created_at=datetime(2026, 10, day, 14, 0),
                 transcript="Migraine at 2pm, moderate.", status=EntryStatus.UNCONFIRMED,
                 attack=attack)


def test_save_and_get_round_trip():
    conn = db.connect(":memory:")
    entry = _attack()
    db.save_entry(conn, entry)
    loaded = db.get_entry(conn, "e1")
    assert loaded is not None
    assert loaded.model_dump() == entry.model_dump()


def test_save_replaces_and_lists_newest_first():
    conn = db.connect(":memory:")
    db.save_entry(conn, _attack("e1", day=23))
    db.save_entry(conn, _attack("e2", day=24))
    db.save_entry(conn, _attack("e1", day=23, severity_function=3))  # update
    assert db.count_entries(conn) == 2
    assert db.get_entry(conn, "e1").attack.severity_function == 3
    listed = db.list_entries(conn)
    assert [e.id for e in listed] == ["e2", "e1"]
    assert db.get_entry(conn, "missing") is None


def test_entry_date_uses_local_day_for_utc_timestamps():
    # Phones send UTC: the entry belongs to the user's local calendar day,
    # not the UTC date (a 9pm US entry must not land on tomorrow).
    created = datetime(2026, 10, 5, 2, 0, tzinfo=timezone.utc)
    entry = Entry(id="tz", entry_type=EntryType.ATTACK, created_at=created,
                  transcript="Migraine at 2pm.")
    assert entry.entry_date == created.astimezone().date()


def test_connection_survives_fastapi_thread_handoff():
    """get_conn may be finalized on a different worker thread than the one
    that opened it; the connection must survive that handoff (500 repro:
    "SQLite objects created in a thread can only be used in that thread")."""
    import threading
    conn = db.connect(":memory:")
    db.save_entry(conn, _attack())
    errors: list[Exception] = []

    def use_elsewhere():
        try:
            assert db.get_entry(conn, "e1") is not None
            conn.close()
        except Exception as exc:  # noqa: BLE001 - collected, then asserted
            errors.append(exc)

    worker = threading.Thread(target=use_elsewhere)
    worker.start()
    worker.join()
    assert errors == []


def test_connect_sets_busy_timeout():
    conn = db.connect(":memory:")
    row = conn.execute("PRAGMA busy_timeout").fetchone()
    assert row[0] == 5000


def test_mixed_offsets_list_chronologically_and_store_utc():
    """Regression: +00:00 and -05:00 rows must order by instant, not text.

    Reproduces History showing an older UTC-stamped entry above a newer
    local-stamped one (text sort compares wall clocks, ignoring offsets).
    """
    from datetime import timedelta

    conn = db.connect(":memory:")
    est = timezone(timedelta(hours=-5))
    late_local = Entry(
        id="late",
        entry_type=EntryType.ATTACK,
        created_at=datetime(2026, 11, 3, 17, 58, tzinfo=est),
        transcript="Evening headache.",
    )
    early_utc = Entry(
        id="early",
        entry_type=EntryType.ATTACK,
        created_at=datetime(2026, 11, 3, 19, 35, tzinfo=timezone.utc),
        transcript="Afternoon headache.",
    )
    db.save_entry(conn, early_utc)
    db.save_entry(conn, late_local)
    assert [e.id for e in db.list_entries(conn)] == ["late", "early"]
    stored = {
        row["id"]: row["created_at"]
        for row in conn.execute("SELECT id, created_at FROM entries")
    }
    assert all(value.endswith("+00:00") for value in stored.values())


def test_connect_normalizes_legacy_offsets(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = db.connect(db_path)
    conn.execute(
        "INSERT INTO entries (id, entry_type, created_at, entry_date, status)"
        " VALUES (?, ?, ?, ?, ?)",
        ("old", "attack", "2026-11-03T17:58:06-05:00", "2026-11-03", "confirmed"),
    )
    conn.commit()
    conn.close()
    conn = db.connect(db_path)
    row = conn.execute("SELECT created_at FROM entries WHERE id = 'old'").fetchone()
    assert row["created_at"] == "2026-11-03T22:58:06+00:00"
    conn.close()


def test_claim_turn_is_atomic_under_threads(tmp_path):
    import threading

    conn = db.connect(tmp_path / "turns.db")
    conn.close()  # each thread opens its own connection, like requests do
    results: list = []

    def attempt():
        thread_conn = db.connect(tmp_path / "turns.db")
        try:
            results.append(db.claim_turn(thread_conn, "r-race", "h"))
        finally:
            thread_conn.close()

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(1 for r in results if r is None) == 1
    assert sum(1 for r in results if r is not None and r["status"] == "pending") == 7


def test_list_filters_and_delete():
    conn = db.connect(":memory:")
    db.save_entry(conn, _attack("e1", day=23))
    db.save_entry(conn, Entry(id="c1", entry_type=EntryType.NO_ATTACK,
                              created_at=datetime(2026, 10, 24, 21, 0)))
    from datetime import date
    assert len(db.list_entries(conn, start=date(2026, 10, 24))) == 1
    assert len(db.list_entries(conn, entry_type="no_attack")) == 1
    assert db.delete_entry(conn, "e1") is True
    assert db.delete_entry(conn, "e1") is False
    assert db.delete_all(conn) == 1
    assert db.count_entries(conn) == 0
