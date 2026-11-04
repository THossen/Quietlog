"""Route-table wiring tests (M3).

The sandbox forbids listen(), so no request can be served here. Instead these
tests assert every endpoint path+method matches the router and dispatches to
the intended function, using Starlette's synchronous matcher.
"""

from fastapi import FastAPI
from starlette.routing import Match

from quietlog.main import create_app

EXPECTED = {
    ("POST", "/entries"): "create_entry",
    ("POST", "/entries/abc/process"): "process",
    ("POST", "/entries/abc/transcribe"): "transcribe_entry",
    ("GET", "/entries"): "list_entries",
    ("GET", "/entries/abc"): "get_entry",
    ("PATCH", "/entries/abc"): "update_entry",
    ("POST", "/entries/start"): "start_attack",
    ("POST", "/entries/abc/end"): "end_attack",
    ("POST", "/entries/abc/note"): "attach_note",
    ("DELETE", "/entries/abc"): "delete_entry",
    ("GET", "/health"): "health",
    ("GET", "/fields"): "fields",
    ("GET", "/summary"): "summary",
}


def _match(app: FastAPI, method: str, path: str):
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 5000),
        "server": ("127.0.0.1", 8000),
        "app": app,
    }
    routes = list(app.routes)
    for route in routes:
        # FastAPI 0.162+ keeps included routers lazy: descend to resolve.
        inner = getattr(route, "original_router", None)
        candidates = inner.routes if inner is not None else [route]
        for candidate in candidates:
            match, child = candidate.matches(scope)
            if match == Match.FULL:
                return child.get("endpoint"), child.get("path_params", {})
    return None, {}


def test_all_endpoints_wired():
    app = create_app()
    for (method, path), func_name in EXPECTED.items():
        endpoint, params = _match(app, method, path)
        assert endpoint is not None, f"{method} {path} does not match"
        assert endpoint.__name__ == func_name, f"{method} {path} -> {endpoint!r}"
    _, params = _match(app, "GET", "/entries/abc")
    assert params == {"entry_id": "abc"}


def test_openapi_lists_all_paths():
    app = create_app()
    paths = app.openapi()["paths"]
    assert set(paths) >= {"/entries", "/entries/{entry_id}",
                           "/entries/{entry_id}/process",
                           "/entries/{entry_id}/transcribe", "/entries/start",
                           "/entries/{entry_id}/end", "/entries/{entry_id}/note",
                           "/health", "/fields", "/summary"}
