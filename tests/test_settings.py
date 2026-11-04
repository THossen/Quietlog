"""Settings load .env automatically; real environment wins."""

from quietlog import settings as settings_mod
from quietlog.settings import load_settings


def test_load_settings_loads_dotenv(monkeypatch):
    calls = []
    monkeypatch.setattr(settings_mod, "load_dotenv", lambda *a, **k: calls.append(True))
    load_settings()
    assert calls == [True]


def test_real_environment_is_honored(monkeypatch):
    monkeypatch.setenv("QUIETLOG_DB", "data/from-env.db")
    monkeypatch.setenv("QUIETLOG_PORT", "8123")
    assert load_settings().db_path == "data/from-env.db"
    assert load_settings().port == 8123
