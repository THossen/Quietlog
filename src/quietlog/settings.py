"""Server settings from environment (M3).

All paths are local: user data never leaves the machine except for Meta
Model API calls (transcription/extraction) on the standard tier.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    db_path: str = "data/quietlog.db"
    audio_dir: str = "data/audio"
    cache_dir: str = "data/cache"
    host: str = "127.0.0.1"
    port: int = 8000
    weather_enabled: bool = True
    weather_timeout: float = 6.0


def load_settings() -> Settings:
    """Read settings from the environment (see .env.example).

    Loads `.env` from the working tree first, so `uvicorn quietlog.main:app`
    just works. Real environment variables always win over `.env` values.
    """
    load_dotenv()
    return Settings(
        db_path=os.environ.get("QUIETLOG_DB", "data/quietlog.db"),
        audio_dir=os.environ.get("QUIETLOG_AUDIO_DIR", "data/audio"),
        cache_dir=os.environ.get("QUIETLOG_CACHE_DIR", "data/cache"),
        host=os.environ.get("QUIETLOG_HOST", "127.0.0.1"),
        port=int(os.environ.get("QUIETLOG_PORT", "8000")),
        weather_enabled=os.environ.get("QUIETLOG_WEATHER", "1").strip().lower()
        not in ("0", "false", "no", "off"),
        weather_timeout=float(os.environ.get("QUIETLOG_WEATHER_TIMEOUT", "6.0")),
    )
