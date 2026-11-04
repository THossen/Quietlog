"""Muse Voice Transcribe file-endpoint client with response cache (M2).

POSTs a whole recording to the Meta Model API one-shot endpoint
(``{base}/asr/transcribe``) and returns a single transcript. Follows the
endpoint contract from Meta's model cookbook: multipart upload with a JSON
``request`` part and an ``audio`` file part, Bearer auth.

Audio format: WAV container (``audioEncoding: WAV``). Short voice notes fit
the one-shot limits (32 MB body, 10 minutes of audio); longer recordings
should use the realtime WebSocket, which this service does not implement.
Per the product spec, the browser converts recordings to WAV in M3 rather
than requiring ffmpeg on Windows.
"""

from __future__ import annotations

import hashlib
import json
import os
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from . import cache as cache_mod

__all__ = [
    "ASR_PATH",
    "DEFAULT_MODEL",
    "MAX_BYTES",
    "TranscribeConfig",
    "TranscribeError",
    "TranscribeResult",
    "load_config",
    "transcribe_file",
]

DEFAULT_BASE_URL = "https://api.meta.ai/v1"
DEFAULT_MODEL = "muse-voice-transcribe-1.0"
ASR_PATH = "/asr/transcribe"

# Server limits: 32 MB bodies, 10 minutes of audio (cookbook values).
MAX_BYTES = 32 * 1024 * 1024
MODES = ("PUSH_TO_TALK", "ENDPOINTING", "DIARIZATION")

_STATUS_HINTS = {
    400: "audio longer than the 10-minute cap, or a malformed request",
    401: "the credential was not accepted",
    404: "endpoint not found",
    413: "body over 32 MB",
    429: "rate limited; back off and retry",
    500: "the server exceeded its processing budget",
}


class TranscribeError(RuntimeError):
    """The transcription request failed or the audio was rejected.

    `permanent` marks failures a retry cannot fix (invalid audio, missing or
    rejected credentials): automatic passes skip them, while transient
    failures (rate limits, server errors, network drops) get bounded
    backoff retries.
    """

    def __init__(self, message: str = "", *, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


class HttpResponse(Protocol):
    status_code: int
    text: str

    def json(self) -> Any: ...


HttpPost = Callable[..., HttpResponse]


@dataclass
class TranscribeConfig:
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    api_key: Optional[str] = None
    mode: str = "PUSH_TO_TALK"
    timeout: int = 300
    cache_dir: str = "data/cache"


@dataclass
class TranscribeResult:
    transcript: str
    audio_duration_ms: Optional[int] = None
    session_id: Optional[str] = None
    turns: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    cached: bool = False


def load_config(
    *,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    mode: Optional[str] = None,
    cache_dir: Optional[str] = None,
) -> TranscribeConfig:
    """Build config: explicit arguments win, environment fills the rest."""
    cfg = TranscribeConfig(
        base_url=(base_url or os.environ.get("QUIETLOG_MODEL_BASE_URL")
                  or DEFAULT_BASE_URL).rstrip("/"),
        model=model or os.environ.get("QUIETLOG_VOICE_MODEL") or DEFAULT_MODEL,
        api_key=api_key or os.environ.get("QUIETLOG_MODEL_API_KEY") or None,
        mode=mode or "PUSH_TO_TALK",
        cache_dir=cache_dir or os.environ.get("QUIETLOG_CACHE_DIR") or "data/cache",
    )
    if cfg.mode not in MODES:
        raise TranscribeError(
            f"Unknown ASR mode {cfg.mode!r}; expected one of {MODES}.",
            permanent=True,
        )
    return cfg


def _default_http_post(
    url: str,
    *,
    headers: dict[str, str],
    request_json: str,
    audio_name: str,
    audio_bytes: bytes,
    timeout: int,
) -> HttpResponse:
    import httpx  # local import: only needed for real network calls

    with httpx.Client(timeout=timeout) as client:
        return client.post(
            url,
            headers=headers,
            files={
                "request": (None, request_json, "application/json"),
                "audio": (audio_name, audio_bytes, "audio/wav"),
            },
        )


def _describe_wav(audio_bytes: bytes) -> str:
    """Human-readable WAV properties, or the reason it is not a WAV."""
    import io

    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as w:
            return (f"{w.getnchannels()}ch/{w.getsampwidth() * 8}bit/"
                    f"{w.getframerate()}Hz/{w.getnframes()}frames")
    except (wave.Error, EOFError) as exc:
        raise TranscribeError(
            f"Not a readable WAV file ({exc}). Quietlog records WAV "
            "(mono 16-bit, 16 or 24 kHz preferred); conversion happens in "
            "the browser so Windows needs no ffmpeg.",
            permanent=True,
        ) from exc


def transcribe_file(
    path: str | Path,
    *,
    config: Optional[TranscribeConfig] = None,
    keywords: Optional[list[str]] = None,
    language_bias: Optional[list[str]] = None,
    session_id: Optional[str] = None,
    http_post: Optional[HttpPost] = None,
    use_cache: bool = True,
) -> TranscribeResult:
    """Transcribe one audio file. Returns the transcript plus metadata."""
    cfg = config or load_config()
    audio_path = Path(path)
    try:
        audio_bytes = audio_path.read_bytes()
    except OSError as exc:
        raise TranscribeError(
            f"Cannot read audio file {audio_path}: {exc}", permanent=True
        ) from exc
    if len(audio_bytes) > MAX_BYTES:
        raise TranscribeError(
            f"{audio_path} is {len(audio_bytes) / 1024 / 1024:.1f} MB; the one-shot "
            "limit is 32 MB. Split the recording or stream it instead.",
            permanent=True,
        )
    _describe_wav(audio_bytes)  # fail fast on non-WAV input

    key = cache_mod.request_hash({
        "service": "asr/transcribe",
        "model": cfg.model,
        "mode": cfg.mode,
        "audio_sha256": hashlib.sha256(audio_bytes).hexdigest(),
        "keywords": keywords or [],
        "language_bias": language_bias or [],
    })
    if use_cache:
        hit = cache_mod.read_cached(cfg.cache_dir, key)
        if hit is not None:
            return _to_result(hit, cfg.model, cached=True)
    if not cfg.api_key:
        raise TranscribeError(
            "No API key: set QUIETLOG_MODEL_API_KEY in .env (see .env.example). "
            "Cached responses replay without a key.",
            permanent=True,
        )

    request_part: dict[str, Any] = {
        "mode": cfg.mode,
        "model": cfg.model,
        "audioEncoding": "WAV",
    }
    if keywords:
        request_part["keywords"] = keywords
    if language_bias:
        request_part["languageBias"] = language_bias

    post = http_post or _default_http_post
    url = cfg.base_url + ASR_PATH
    if session_id:
        url += f"?sessionId={session_id}"
    try:
        response = post(
            url,
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            request_json=json.dumps(request_part),
            audio_name=audio_path.name,
            audio_bytes=audio_bytes,
            timeout=cfg.timeout,
        )
    except TranscribeError:
        raise
    except Exception as exc:
        raise TranscribeError(f"Transcription request failed: {exc}") from exc
    if response.status_code != 200:
        hint = _STATUS_HINTS.get(response.status_code, "unexpected response")
        raise TranscribeError(
            f"[{response.status_code}] {hint}: {response.text[:300]}",
            permanent=response.status_code in (400, 401, 404, 413),
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise TranscribeError(f"Transcription returned non-JSON: {response.text[:200]}") from exc
    if use_cache:
        cache_mod.write_cached(cfg.cache_dir, key, data)
    return _to_result(data, cfg.model, cached=False)


def _to_result(data: dict[str, Any], model: str, *, cached: bool) -> TranscribeResult:
    transcript = data.get("transcript")
    if not isinstance(transcript, str):
        raise TranscribeError(f"Transcription response has no transcript: {str(data)[:200]}")
    turns = data.get("turns") or []
    return TranscribeResult(
        transcript=transcript,
        audio_duration_ms=data.get("audioDurationMs"),
        session_id=data.get("sessionId"),
        turns=turns if isinstance(turns, list) else [],
        model=model,
        cached=cached,
    )
