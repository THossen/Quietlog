"""On-disk response cache for Meta Model API calls (M2).

Both services (`llm_service`, `transcribe_service`) cache every successful
response on local disk, keyed by the SHA-256 hash of the canonical request.
Re-running evaluation against a warm cache costs nothing and needs no key.

Cache entries live under ``<QUIETLOG_CACHE_DIR>/meta/<hash>.json``. A corrupt
or version-mismatched entry is treated as a miss, never as an error.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

CACHE_VERSION = 1


def default_cache_dir() -> Path:
    """Local cache root. Never committed (see .gitignore)."""
    return Path(os.environ.get("QUIETLOG_CACHE_DIR", "data/cache"))


def request_hash(payload: dict[str, Any]) -> str:
    """Stable hash of the canonical request description."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def cache_path(cache_dir: str | Path, key: str) -> Path:
    return Path(cache_dir) / "meta" / f"{key}.json"


def read_cached(cache_dir: str | Path, key: str) -> Optional[dict[str, Any]]:
    """Return the cached response dict, or None on a miss or bad entry."""
    path = cache_path(cache_dir, key)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        entry = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(entry, dict):
        return None
    if entry.get("cache_version") != CACHE_VERSION:
        return None
    if entry.get("request_hash") != key:
        return None
    response = entry.get("response")
    return response if isinstance(response, dict) else None


def write_cached(cache_dir: str | Path, key: str, response: dict[str, Any]) -> Path:
    """Store one response atomically. Returns the entry path."""
    path = cache_path(cache_dir, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "cache_version": CACHE_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "request_hash": key,
        "response": response,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entry, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return path
