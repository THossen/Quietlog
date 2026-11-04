"""Weather snapshot tests: mapping, tendency, parsing, fail-soft, storage.

All network is stubbed. Nothing here needs the internet.
"""

from datetime import date, datetime, timedelta, timezone

from quietlog import db as db_mod
from quietlog import weather_service
from quietlog.models import AttackDetails, Entry, WeatherSnapshot
from quietlog.processing import process_entry
from quietlog.settings import Settings
from quietlog.summary import compute_summary
from quietlog.types import EntryStatus, EntryType


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


def _current_payload(*, hour="2026-11-03T14:00", now="2026-11-03T14:20",
                     pressure=1012.0, code=3, offset=-18000):
    hours = [f"2026-11-03T{h:02d}:00" for h in range(24)]
    pressures = [1015.0 - 0.25 * h for h in range(24)]
    return {
        "utc_offset_seconds": offset,
        "current": {
            "time": now,
            "temperature_2m": 18.5,
            "relative_humidity_2m": 62.0,
            "pressure_msl": pressure,
            "weather_code": code,
        },
        "hourly": {"time": hours, "pressure_msl": pressures},
    }


def _archive_payload(*, day="2026-10-30", hour=8, pressure=1008.0, code=61,
                     offset=-18000):
    prev = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    times, temps, humids, pressures, codes = [], [], [], [], []
    for d in (prev, day):
        for h in range(24):
            times.append(f"{d}T{h:02d}:00")
            temps.append(15.0)
            humids.append(70.0)
            pressures.append(pressure + 0.1 * h)
            codes.append(code)
    return {
        "utc_offset_seconds": offset,
        "hourly": {
            "time": times,
            "temperature_2m": temps,
            "relative_humidity_2m": humids,
            "pressure_msl": pressures,
            "weather_code": codes,
        },
    }


def _entry(**kwargs):
    base = dict(
        entry_type=EntryType.ATTACK,
        created_at=datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc),
        status=EntryStatus.UNCONFIRMED,
        attack=AttackDetails(),
    )
    base.update(kwargs)
    return Entry(**base)


# --- mapping ---------------------------------------------------------------


def test_condition_labels():
    assert weather_service.condition_label(0) == "Clear"
    assert weather_service.condition_label(3) == "Overcast"
    assert weather_service.condition_label(61) == "Rain"
    assert weather_service.condition_label(95) == "Thunderstorm"
    assert weather_service.condition_label(999) == "Unknown"
    assert weather_service.condition_label(None) == "Unknown"
    assert weather_service.condition_label("bogus") == "Unknown"


def test_pressure_tendency_thresholds():
    series = [1010.0] * 10
    assert weather_service.pressure_tendency(series, 5) == "steady"
    series[5] = 1010.6
    assert weather_service.pressure_tendency(series, 5) == "rising"
    series[5] = 1009.4
    assert weather_service.pressure_tendency(series, 5) == "falling"
    assert weather_service.pressure_tendency(series, 2) is None  # too early
    assert weather_service.pressure_tendency(series, 99) is None
    assert weather_service.pressure_tendency([1010.0] * 4, 3) == "steady"
    assert weather_service.pressure_tendency(None, 5) is None
    gap = [1010.0] * 10
    gap[2] = None
    assert weather_service.pressure_tendency(gap, 5) is None


def test_valid_coords():
    assert weather_service.valid_coords(40.7, -74.0)
    assert not weather_service.valid_coords(91.0, 0.0)
    assert not weather_service.valid_coords(0.0, 181.0)
    assert not weather_service.valid_coords(float("nan"), 0.0)
    assert not weather_service.valid_coords(None, 0.0)
    assert not weather_service.valid_coords("not-a-number", 0.0)


# --- current -----------------------------------------------------------------


def test_fetch_current_parses_snapshot_and_tendency():
    calls = []

    def fake_get(url, params, timeout):
        calls.append((url, params))
        return FakeResponse(200, _current_payload())

    snap = weather_service.fetch_current_weather(40.7, -74.0, get=fake_get)
    assert snap is not None
    assert snap.temperature_c == 18.5
    assert snap.humidity_pct == 62.0
    assert snap.pressure_hpa == 1012.0
    assert snap.condition == "Overcast"
    assert snap.basis == "log_time"
    assert snap.source == "open-meteo"
    # 1015 - 0.25h: three hours earlier was 0.75 hPa higher -> falling.
    assert snap.pressure_tendency == "falling"
    assert snap.observed_at is not None
    assert calls and calls[0][0] == weather_service.FORECAST_URL


def test_fetch_current_fail_soft():
    assert weather_service.fetch_current_weather(
        40.7, -74.0, get=lambda *a: FakeResponse(500, None)) is None
    assert weather_service.fetch_current_weather(
        40.7, -74.0, get=lambda *a: FakeResponse(200, None)) is None
    assert weather_service.fetch_current_weather(
        40.7, -74.0, get=lambda *a: FakeResponse(200, {"current": {}})) is None
    assert weather_service.fetch_current_weather(999.0, 0.0) is None

    def boom(*args):
        raise ConnectionError("offline")

    assert weather_service.fetch_current_weather(40.7, -74.0, get=boom) is None


def test_fetch_current_drops_implausible_fields():
    payload = _current_payload()
    payload["current"]["temperature_2m"] = 999.0  # bad sensor value
    snap = weather_service.fetch_current_weather(
        40.7, -74.0, get=lambda *a: FakeResponse(200, payload))
    assert snap is not None
    assert snap.temperature_c is None
    assert snap.pressure_hpa == 1012.0


# --- historical --------------------------------------------------------------


def test_fetch_historical_matches_attack_hour():
    old_day = (datetime.now(timezone.utc) - timedelta(days=10)).date()

    def fake_get(url, params, timeout):
        assert url == weather_service.ARCHIVE_URL
        assert params["start_date"] == (old_day - timedelta(days=1)).isoformat()
        assert params["end_date"] == old_day.isoformat()
        return FakeResponse(200, _archive_payload(day=old_day.isoformat()))

    # Naive attack times are location wall-clock.
    when = datetime(old_day.year, old_day.month, old_day.day, 8, 15)
    snap = weather_service.fetch_historical_weather(40.7, -74.0, when, get=fake_get)
    assert snap is not None
    assert snap.condition == "Rain"
    assert snap.basis == "attack_time"
    assert snap.observed_at is not None
    assert snap.observed_at.hour == 8
    # 1008 + 0.1h: three hours earlier was 0.3 lower -> steady.
    assert snap.pressure_tendency == "steady"


def test_fetch_historical_recent_uses_forecast_past_days():
    recent_day = (datetime.now(timezone.utc) - timedelta(days=2)).date()
    seen = {}

    def fake_get(url, params, timeout):
        seen["url"] = url
        seen.update(params)
        return FakeResponse(200, _archive_payload(day=recent_day.isoformat()))

    when = datetime(recent_day.year, recent_day.month, recent_day.day, 8, 0)
    snap = weather_service.fetch_historical_weather(40.7, -74.0, when, get=fake_get)
    assert snap is not None
    assert snap.basis == "attack_time"
    assert seen["url"] == weather_service.FORECAST_URL
    assert seen["past_days"] == 8


def test_fetch_historical_shifts_aware_times_by_offset():
    old_day = (datetime.now(timezone.utc) - timedelta(days=10)).date()

    def fake_get(url, params, timeout):
        return FakeResponse(200, _archive_payload(day=old_day.isoformat()))

    # 13:00 UTC is 08:00 at UTC-5.
    when = datetime(old_day.year, old_day.month, old_day.day, 13, 0,
                    tzinfo=timezone.utc)
    snap = weather_service.fetch_historical_weather(40.7, -74.0, when, get=fake_get)
    assert snap is not None
    assert snap.observed_at is not None
    assert (snap.observed_at.hour, snap.observed_at.minute) == (8, 0)


def test_fetch_historical_missing_hour_is_none():
    old_day = (datetime.now(timezone.utc) - timedelta(days=10)).date()
    payload = _archive_payload(day=old_day.isoformat())
    payload["hourly"]["time"] = [t for t in payload["hourly"]["time"]
                                 if not t.startswith(f"{old_day.isoformat()}T08")]
    snap = weather_service.fetch_historical_weather(
        40.7, -74.0, datetime(old_day.year, old_day.month, old_day.day, 8, 0),
        get=lambda *a: FakeResponse(200, payload))
    assert snap is None


# --- refresh rule ------------------------------------------------------------


def test_refresh_backdated_attack_gets_history(monkeypatch):
    historical = WeatherSnapshot(pressure_hpa=1008.0, pressure_tendency="falling",
                                 basis="attack_time", source="open-meteo")
    monkeypatch.setattr(
        weather_service, "fetch_historical_weather", lambda *a, **k: historical)
    old = datetime.now(timezone.utc) - timedelta(days=4)
    out = weather_service.refresh_for_attack(None, 40.7, -74.0, old)
    assert out == historical


def test_refresh_recent_attack_keeps_log_snapshot(monkeypatch):
    existing = WeatherSnapshot(pressure_hpa=1012.0, basis="log_time",
                               source="open-meteo")

    def boom(*args, **kwargs):
        raise AssertionError("no network needed")

    monkeypatch.setattr(weather_service, "fetch_historical_weather", boom)
    monkeypatch.setattr(weather_service, "fetch_current_weather", boom)
    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    assert weather_service.refresh_for_attack(existing, 40.7, -74.0, recent) == existing


def test_refresh_failure_keeps_existing(monkeypatch):
    existing = WeatherSnapshot(pressure_hpa=1012.0, basis="log_time",
                               source="open-meteo")
    monkeypatch.setattr(
        weather_service, "fetch_historical_weather", lambda *a, **k: None)
    old = datetime.now(timezone.utc) - timedelta(days=4)
    assert weather_service.refresh_for_attack(existing, 40.7, -74.0, old) == existing


# --- storage -----------------------------------------------------------------


def test_weather_roundtrips_through_db(tmp_path):
    conn = db_mod.connect(tmp_path / "t.db")
    try:
        entry = _entry(weather=WeatherSnapshot(
            observed_at=datetime(2026, 11, 3, 14, 0, tzinfo=timezone.utc),
            temperature_c=18.5,
            pressure_hpa=1012.0,
            pressure_tendency="falling",
            condition="Overcast",
            basis="log_time",
            source="open-meteo",
        ))
        db_mod.save_entry(conn, entry)
        back = db_mod.get_entry(conn, entry.id)
        assert back is not None and back.weather is not None
        assert back.weather.pressure_hpa == 1012.0
        assert back.weather.pressure_tendency == "falling"
        assert back.weather.condition == "Overcast"
    finally:
        conn.close()


def test_migration_adds_column_to_old_db(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.executescript("""
    CREATE TABLE entries (
        id TEXT PRIMARY KEY, entry_type TEXT NOT NULL, created_at TEXT NOT NULL,
        entry_date TEXT NOT NULL, audio_path TEXT, transcript TEXT,
        status TEXT NOT NULL, attack_json TEXT, processing_error TEXT);
    """)
    conn.commit()
    conn.close()
    conn = db_mod.connect(path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(entries)")}
        assert "weather_json" in columns
        db_mod.save_entry(conn, _entry())
        assert db_mod.get_entry(conn, _entry().id) is None  # different id
        assert db_mod.count_entries(conn) == 1
    finally:
        conn.close()


# --- processing + summary ----------------------------------------------------


def test_process_backdated_attack_attaches_history(tmp_path, monkeypatch):
    historical = WeatherSnapshot(pressure_hpa=1008.0, basis="attack_time",
                                 source="open-meteo")
    monkeypatch.setattr(
        weather_service, "fetch_historical_weather", lambda *a, **k: historical)
    old_start = datetime.now(timezone.utc) - timedelta(days=4)
    conn = db_mod.connect(tmp_path / "t.db")
    try:
        entry = _entry(transcript="old headache",
                       created_at=datetime.now(timezone.utc))
        db_mod.save_entry(conn, entry)
        out = process_entry(
            conn, entry,
            Settings(db_path=str(tmp_path / "t.db")),
            extract_fn=lambda transcript, ref: AttackDetails(attack_start=old_start),
            coords=(40.7, -74.0),
        )
        assert out.status == EntryStatus.UNCONFIRMED
        assert out.weather is not None
        assert out.weather.basis == "attack_time"
    finally:
        conn.close()


def test_process_weather_failure_never_fails_extraction(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise ConnectionError("offline")

    monkeypatch.setattr(weather_service, "fetch_current_weather", boom)
    conn = db_mod.connect(tmp_path / "t.db")
    try:
        entry = _entry(transcript="fresh headache",
                       created_at=datetime.now(timezone.utc))
        db_mod.save_entry(conn, entry)
        out = process_entry(
            conn, entry,
            Settings(db_path=str(tmp_path / "t.db")),
            extract_fn=lambda transcript, ref: AttackDetails(
                attack_start=datetime.now(timezone.utc)),
            coords=(40.7, -74.0),
        )
        assert out.status == EntryStatus.UNCONFIRMED
        assert out.processing_error is None
        assert out.weather is None
    finally:
        conn.close()


def test_summary_counts_falling_pressure():
    falling = _entry(weather=WeatherSnapshot(pressure_tendency="falling"))
    steady = _entry(weather=WeatherSnapshot(pressure_tendency="steady"))
    no_tendency = _entry(weather=WeatherSnapshot(pressure_hpa=1010.0))
    plain = _entry()
    s = compute_summary([falling, steady, no_tendency, plain],
                        date(2026, 11, 1), date(2026, 11, 7),
                        generated_at=datetime(2026, 11, 7, tzinfo=timezone.utc))
    assert s.weather_n == 3
    assert s.weather_tendency_n == 2
    assert s.weather_falling_n == 1
