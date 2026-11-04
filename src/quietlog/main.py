"""Quietlog FastAPI app (M3: capture/review/retry API).

Loopback for simulators; bind 0.0.0.0 for a real phone on your LAN::

    .venv\\Scripts\\Activate.ps1
    uvicorn quietlog.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import db as db_mod
from .routes import get_settings, router
from .settings import Settings, load_settings

logger = logging.getLogger("quietlog")

_FIELDS_PATH = Path(__file__).resolve().parents[2] / "schema" / "fields.yaml"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Crash recovery: recordings stuck mid-transcription when the server
    died go back to pending so the next client contact resumes them."""
    try:
        settings = load_settings()
        conn = db_mod.connect(settings.db_path)
        try:
            stuck = db_mod.reset_inflight_recordings(conn)
        finally:
            conn.close()
        if stuck:
            logger.info("resumed %d interrupted transcription(s)", stuck)
    except Exception:  # noqa: BLE001 - recovery must never block startup
        logger.exception("transcription recovery sweep failed")
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. Tests pass their own settings (tmp dirs)."""
    app = FastAPI(title="Quietlog", version="0.1.0", lifespan=_lifespan)
    if settings is not None:
        app.dependency_overrides[get_settings] = lambda: settings
    else:
        load_settings()  # ensure .env is loaded even before the first request
        if not os.environ.get("QUIETLOG_MODEL_API_KEY"):
            logger.warning(
                "QUIETLOG_MODEL_API_KEY is not set: saving works, but "
                "transcription/extraction will fail until the key is in .env."
            )
    # Local single-user app: loopback origins only, never the open web.
    # (React Native fetch sends no Origin header, so it passes through.)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"https?://(127\.0\.0\.1|localhost)(:\d+)?",
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type"],
    )
    app.include_router(router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/fields")
    def fields() -> JSONResponse:
        """Return the canonical field schema (schema/fields.yaml) as JSON."""
        with _FIELDS_PATH.open(encoding="utf-8") as f:
            return JSONResponse(yaml.safe_load(f))

    return app


app = create_app()
