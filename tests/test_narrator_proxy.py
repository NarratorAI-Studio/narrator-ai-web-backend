"""Tests for group G + group E narrator proxy endpoints (the implementation requirement).

Strategy: monkeypatch `server.proxy_narrator_upstream` and run with an
in-memory sqlite users table, mirroring test_narrator_metadata.py.

Coverage:
  - All static routes return 200 and forward payload verbatim
  - All dynamic routes (query/<task_id>) substitute task_id in upstream path
  - POST routes receive and forward JSON body
  - /narrator/commentary/writing dispatches GET vs POST to different upstreams
  - search-media uses 90-second timeout
  - Missing Bearer → 401; unrecognised X-Web-App-Key → 401
  - Upstream UpstreamNarratorError maps to correct HTTP status
"""

from __future__ import annotations

import logging
import sqlite3
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text

sqlite3.register_adapter(Decimal, str)

SQLITE_USERS_SCHEMA = """
CREATE TABLE users (
    app_key TEXT PRIMARY KEY,
    id INTEGER UNIQUE,
    balance_points NUMERIC NOT NULL DEFAULT 1000 CHECK (balance_points >= 0),
    nickname TEXT,
    mobile TEXT,
    email TEXT,
    company_name TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

AUTH_TOKEN = "test-bff-token"
WEB_APP_KEY = "grid_AbCdEfGhIjKlMnOpQrStUv"
AUTH_HEADERS = {
    "Authorization": f"Bearer {AUTH_TOKEN}",
    "X-Web-App-Key": WEB_APP_KEY,
}

from narrator_proxy.routes import (  # noqa: E402
    COMMENTARY_CREATE_TIMEOUT_SECONDS,
    COMMENTARY_WRITING_GET,
    COMMENTARY_WRITING_POST,
    DYNAMIC_ROUTES,
    STATIC_ROUTES,
)
from narrator_metadata.upstream import UpstreamNarratorError  # noqa: E402


@pytest.fixture()
def sqlite_engine():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    with engine.begin() as conn:
        conn.execute(text(SQLITE_USERS_SCHEMA))
        conn.execute(
            text(
                "INSERT INTO users (app_key, id, balance_points) "
                "VALUES (:k, 1, 1000)"
            ),
            {"k": WEB_APP_KEY},
        )
    yield engine
    engine.dispose()


@pytest.fixture()
def client(sqlite_engine, monkeypatch):
    import server

    monkeypatch.setattr(server, "get_db_engine", lambda: sqlite_engine)
    monkeypatch.setattr(
        server, "get_db_core_connection", lambda: sqlite_engine.connect()
    )
    monkeypatch.setenv("PRICING_BFF_AUTH_TOKEN", AUTH_TOKEN)
    return server.app.test_client()


# ---------- registration ----------

def test_all_static_routes_registered(client):
    import server
    registered = {rule.rule for rule in server.app.url_map.iter_rules()}
    for route_meta in STATIC_ROUTES:
        assert route_meta.backend_path in registered, (
            f"Static route {route_meta.backend_path} not registered"
        )


def test_all_dynamic_routes_registered(client):
    import server
    registered = {rule.rule for rule in server.app.url_map.iter_rules()}
    for route_meta in DYNAMIC_ROUTES:
        # Flask stores <var> in angle brackets
        assert route_meta.backend_path in registered, (
            f"Dynamic route {route_meta.backend_path} not registered"
        )


def test_commentary_writing_registered(client):
    import server
    registered = {rule.rule for rule in server.app.url_map.iter_rules()}
    assert "/narrator/commentary/writing" in registered


# ---------- happy path: static routes ----------

@pytest.mark.parametrize("route_meta", STATIC_ROUTES, ids=lambda r: r.endpoint_label)
def test_static_route_happy_path(client, monkeypatch, route_meta):
    payload = {"code": 10000, "data": {"items": []}}
    captured = {}

    def fake_proxy(upstream_path, *, method, query_params, body, timeout_seconds):
        captured["upstream_path"] = upstream_path
        captured["method"] = method
        captured["body"] = body
        return payload

    import server
    monkeypatch.setattr(server, "proxy_narrator_upstream", fake_proxy)

    if route_meta.method == "GET":
        resp = client.get(route_meta.backend_path, headers=AUTH_HEADERS)
    else:
        resp = client.post(
            route_meta.backend_path,
            json={"key": "value"},
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json() == payload
    assert captured["upstream_path"] == route_meta.upstream_path
    assert captured["method"] == route_meta.method


def test_movie_sucai_maps_page_size_to_upstream_size(client, monkeypatch):
    captured = {}

    def fake_proxy(upstream_path, *, method, query_params, body, timeout_seconds):
        captured["upstream_path"] = upstream_path
        captured["query_params"] = query_params
        return {"code": 10000, "data": {"items": []}}

    import server
    monkeypatch.setattr(server, "proxy_narrator_upstream", fake_proxy)

    resp = client.get(
        "/narrator/movie-sucai?page=1&page_size=21",
        headers=AUTH_HEADERS,
    )

    assert resp.status_code == 200, resp.get_json()
    assert captured["upstream_path"] == "/v2/res/movie-sucai"
    assert captured["query_params"] == {
        "page": "1",
        "size": "21",
        "name": None,
    }


# ---------- dynamic routes: task_id substitution ----------

@pytest.mark.parametrize("route_meta", DYNAMIC_ROUTES, ids=lambda r: r.endpoint_label)
def test_dynamic_route_substitutes_task_id(client, monkeypatch, route_meta):
    captured = {}

    def fake_proxy(upstream_path, *, method, query_params, body, timeout_seconds):
        captured["upstream_path"] = upstream_path
        return {"code": 10000, "data": {}}

    import server
    monkeypatch.setattr(server, "proxy_narrator_upstream", fake_proxy)

    test_task_id = "task-abc-123"
    url = route_meta.backend_path.replace("<task_id>", test_task_id)
    resp = client.get(url, headers=AUTH_HEADERS)

    assert resp.status_code == 200, resp.get_json()
    expected_upstream = route_meta.upstream_path.replace("{task_id}", test_task_id)
    assert captured["upstream_path"] == expected_upstream


def test_dynamic_route_url_encodes_path_args(client, monkeypatch):
    """task_id arriving with reserved chars (`?`, `/`, `&`) must NOT break out
    of the path segment when formatted into the upstream URL — otherwise a
    caller can smuggle arbitrary upstream query params past the BFF allowlist
    (regression coverage security-sensitive).
    """
    captured = {}

    def fake_proxy(upstream_path, *, method, query_params, body, timeout_seconds):
        captured["upstream_path"] = upstream_path
        return {"code": 10000, "data": {}}

    import server
    monkeypatch.setattr(server, "proxy_narrator_upstream", fake_proxy)

    # Send a percent-encoded `?` — Flask decodes path segments before the view
    # sees them, so `path_args["task_id"]` arrives here as a literal `?...`.
    # If we then naively `.format()` it into upstream_path, the proxy URL
    # becomes `/v2/.../query/abc?smuggled=1?legit=...` and `smuggled=1` ends
    # up as an unsanctioned upstream query param.
    resp = client.get(
        "/narrator/ocr-extraction/query/abc%3Fsmuggled=1",
        headers=AUTH_HEADERS,
    )

    assert resp.status_code == 200, resp.get_json()
    # The literal `?` must be re-encoded to `%3F` before being formatted into
    # the upstream path; no raw `?` may appear in the upstream URL path.
    assert "?" not in captured["upstream_path"]
    assert "%3F" in captured["upstream_path"]


# ---------- writing route: GET vs POST dispatch ----------

def test_commentary_writing_get_dispatches_correctly(client, monkeypatch):
    captured = {}

    def fake_proxy(upstream_path, *, method, query_params, body, timeout_seconds):
        captured["upstream_path"] = upstream_path
        captured["method"] = method
        return {"code": 10000, "data": {}}

    import server
    monkeypatch.setattr(server, "proxy_narrator_upstream", fake_proxy)

    resp = client.get(
        "/narrator/commentary/writing?task_id=t1",
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    assert captured["upstream_path"] == COMMENTARY_WRITING_GET.upstream_path
    assert captured["method"] == "GET"


def test_commentary_writing_post_dispatches_correctly(client, monkeypatch):
    captured = {}

    def fake_proxy(upstream_path, *, method, query_params, body, timeout_seconds):
        captured["upstream_path"] = upstream_path
        captured["method"] = method
        captured["body"] = body
        return {"code": 10000, "data": {}}

    import server
    monkeypatch.setattr(server, "proxy_narrator_upstream", fake_proxy)

    payload = {"task_id": "t1", "file_id": "f1", "content": []}
    resp = client.post(
        "/narrator/commentary/writing",
        json=payload,
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    assert captured["upstream_path"] == COMMENTARY_WRITING_POST.upstream_path
    assert captured["body"] == payload


# ---------- search-media uses 90-second timeout ----------

def test_search_media_uses_90s_timeout(client, monkeypatch):
    captured = {}

    def fake_proxy(upstream_path, *, method, query_params, body, timeout_seconds):
        captured["timeout_seconds"] = timeout_seconds
        return {"code": 10000, "data": {}}

    import server
    monkeypatch.setattr(server, "proxy_narrator_upstream", fake_proxy)

    resp = client.get(
        "/narrator/commentary/search-media?query=test",
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    assert captured["timeout_seconds"] == 90.0


@pytest.mark.parametrize(
    ("backend_path", "upstream_path", "payload"),
    [
        (
            "/narrator/commentary/create-popular-learning",
            "/v2/task/commentary/create_popular_learning",
            {"video_srt_path": "srt-1", "narrator_type": "original"},
        ),
        (
            "/narrator/commentary/create-subsync",
            "/v2/task/commentary/create_subsync_task",
            {"episodes_data": [{"video_oss_key": "v", "srt_oss_key": "s"}]},
        ),
        (
            "/narrator/commentary/create-generate-writing",
            "/v2/task/commentary/create_generate_writing",
            {"learning_model_id": "model-1"},
        ),
        (
            "/narrator/commentary/create-clip-data",
            "/v2/task/commentary/create_generate_clip_data",
            {"task_order_num": "generate_writing_test"},
        ),
        (
            "/narrator/commentary/create-fast-writing",
            "/v2/task/commentary/create_fast_generate_writing",
            {"native_srt": "srt-1"},
        ),
        (
            "/narrator/commentary/create-fast-writing-clip-data",
            "/v2/task/commentary/create_generate_fast_writing_clip_data",
            {"native_srt": "srt-1"},
        ),
        (
            "/narrator/commentary/create-video-composing",
            "/v2/task/commentary/create_video_composing",
            {"order_num": "clip-data-task"},
        ),
    ],
)
def test_create_chain_routes_use_long_create_timeout(
    client,
    monkeypatch,
    backend_path,
    upstream_path,
    payload,
):
    captured = {}

    def fake_proxy(upstream_path, *, method, query_params, body, timeout_seconds):
        captured["upstream_path"] = upstream_path
        captured["timeout_seconds"] = timeout_seconds
        return {"code": 10000, "data": {"task_id": "create-task"}}

    import server
    monkeypatch.setattr(server, "proxy_narrator_upstream", fake_proxy)

    resp = client.post(
        backend_path,
        json=payload,
        headers=AUTH_HEADERS,
    )

    assert resp.status_code == 200, resp.get_json()
    assert captured["upstream_path"] == upstream_path
    assert captured["timeout_seconds"] == COMMENTARY_CREATE_TIMEOUT_SECONDS


# ---------- POST body forwarding ----------

def test_post_body_forwarded_verbatim(client, monkeypatch):
    captured = {}

    def fake_proxy(upstream_path, *, method, query_params, body, timeout_seconds):
        captured["body"] = body
        return {}

    import server
    monkeypatch.setattr(server, "proxy_narrator_upstream", fake_proxy)

    payload = {"native_video": "v.mp4", "native_srt": "s.srt", "narrator_type": "t1"}
    resp = client.post(
        "/narrator/commentary/material-verification",
        json=payload,
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    assert captured["body"] == payload


def test_post_json_array_body_returns_400(client, monkeypatch):
    """Valid JSON but not a dict (e.g. an array) must return 400 envelope."""
    captured = {"called": False}

    def fake_proxy(*a, **kw):
        captured["called"] = True
        return {}

    import server
    monkeypatch.setattr(server, "proxy_narrator_upstream", fake_proxy)

    resp = client.post(
        "/narrator/ocr-extraction/create",
        json=["not", "a", "dict"],
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "BAD_REQUEST"
    assert not captured["called"]


def test_post_malformed_json_body_returns_400(client, monkeypatch):
    """Malformed JSON body must be rejected as 400 with our envelope, not
    silently forwarded as an empty body to upstream (regression coverage
    caution — earlier `silent=True` masked client input errors as upstream
    failures).
    """
    captured = {"called": False}

    def fake_proxy(*a, **kw):
        captured["called"] = True
        return {}

    import server
    monkeypatch.setattr(server, "proxy_narrator_upstream", fake_proxy)

    resp = client.post(
        "/narrator/ocr-extraction/create",
        data='{"not": valid json',
        content_type="application/json",
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "BAD_REQUEST"
    assert not captured["called"]


def test_post_wrong_content_type_returns_400_envelope(client, monkeypatch):
    """POST without `Content-Type: application/json` must return the route
    family's 400 BAD_REQUEST envelope, not Flask 3's default 415 HTML page
    (regression coverage caution — Flask 3 raises UnsupportedMediaType
    when content type is missing/wrong, which `except BadRequest` does not
    catch).
    """
    captured = {"called": False}

    def fake_proxy(*a, **kw):
        captured["called"] = True
        return {}

    import server
    monkeypatch.setattr(server, "proxy_narrator_upstream", fake_proxy)

    # Send text/plain body — Flask 3 raises UnsupportedMediaType on get_json.
    resp = client.post(
        "/narrator/ocr-extraction/create",
        data="not json at all",
        content_type="text/plain",
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body is not None, "response must be JSON envelope, not Flask default 415 HTML"
    assert body["error"]["code"] == "BAD_REQUEST"
    assert not captured["called"]


# ---------- query-param forwarding ----------

def test_commentary_list_forwards_query_params(client, monkeypatch):
    captured = {}

    def fake_proxy(upstream_path, *, method, query_params, body, timeout_seconds):
        captured["query_params"] = query_params
        return {"code": 10000, "data": {"items": []}}

    import server
    monkeypatch.setattr(server, "proxy_narrator_upstream", fake_proxy)

    resp = client.get(
        "/narrator/commentary/list?page=2&limit=10&status=1",
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    assert captured["query_params"]["page"] == "2"
    assert captured["query_params"]["limit"] == "10"
    assert captured["query_params"]["status"] == "1"


# ---------- auth guards ----------

def test_missing_bearer_returns_401(client, monkeypatch):
    # Track upstream-call attempts so we can assert the auth gate short-circuits
    # before the proxy fires (regression coverage — the previous version
    # used `assert fake_proxy not in []` which is always true and accepted
    # `in (401, 503)` which would have masked a wrong status mapping).
    captured = {"called": False}

    def fake_proxy(*a, **kw):
        captured["called"] = True
        return {}

    import server
    monkeypatch.setattr(server, "proxy_narrator_upstream", fake_proxy)

    resp = client.get(
        "/narrator/commentary/list",
        headers={"X-Web-App-Key": WEB_APP_KEY},  # no Authorization
    )
    assert resp.status_code == 401
    assert not captured["called"]


def test_unknown_app_key_returns_401(client, monkeypatch):
    captured = {"called": False}

    def fake_proxy(*a, **kw):
        captured["called"] = True
        return {}

    import server
    monkeypatch.setattr(server, "proxy_narrator_upstream", fake_proxy)

    resp = client.get(
        "/narrator/commentary/list",
        headers={
            "Authorization": f"Bearer {AUTH_TOKEN}",
            "X-Web-App-Key": "grid_UNKNOWN_KEY",
        },
    )
    assert resp.status_code == 401
    assert not captured["called"]


# ---------- upstream error mapping ----------

def test_upstream_error_propagates_http_status(client, monkeypatch):
    def fake_proxy(*a, **kw):
        raise UpstreamNarratorError(
            503, "UPSTREAM_TIMEOUT", "upstream timed out", retryable=True
        )

    import server
    monkeypatch.setattr(server, "proxy_narrator_upstream", fake_proxy)

    resp = client.get("/narrator/commentary/list", headers=AUTH_HEADERS)
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["error"]["code"] == "UPSTREAM_TIMEOUT"
    assert body["error"]["retryable"] is True


def test_upstream_unreachable_logs_reason(client, monkeypatch, caplog):
    def fake_proxy(*a, **kw):
        raise UpstreamNarratorError(
            502,
            "UPSTREAM_UNREACHABLE",
            "Upstream is unreachable.",
            retryable=True,
            details={
                "reason": "[Errno 101] Network is unreachable",
                "upstream_path": "/v2/task/commentary/create_video_composing",
            },
        )

    import server
    monkeypatch.setattr(server, "proxy_narrator_upstream", fake_proxy)

    with caplog.at_level(logging.caution, logger=server.app.logger.name):
        resp = client.post(
            "/narrator/commentary/create-video-composing",
            json={"task_order_num": "fast_writing_clip_data_test"},
            headers=AUTH_HEADERS,
        )

    assert resp.status_code == 502
    body = resp.get_json()
    assert body["error"]["code"] == "UPSTREAM_UNREACHABLE"
    assert any(
        "Narrator upstream error" in record.getMessage()
        and "UPSTREAM_UNREACHABLE" in record.getMessage()
        and "[Errno 101] Network is unreachable" in record.getMessage()
        and "/v2/task/commentary/create_video_composing" in record.getMessage()
        for record in caplog.records
    )


# ---------- master-key identity model (the implementation requirement) ----------

def test_upstream_uses_master_app_key_not_user_app_key(client, monkeypatch):
    """The reseller `X-Web-App-Key` is identity-only — it must NEVER reach
    upstream. Upstream sees the backend's master `OPEN_FASTAPI_APP_KEY`.
    Patches `urlopen` at the bottom of `narrator_proxy.upstream` and asserts
    the outgoing `app-key` header is the master key (not WEB_APP_KEY).
    """
    from narrator_proxy import upstream as up

    monkeypatch.setenv("OPEN_FASTAPI_BASE", "https://example.invalid")
    monkeypatch.setenv("OPEN_FASTAPI_APP_KEY", "backend-master-key")

    captured: dict = {}

    class _FakeResponse:
        status = 200
        headers = {"Content-Length": "27"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n=-1):
            return b'{"code":10000,"data":{}}'  # noqa: F501

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["method"] = request.get_method()
        captured["body"] = request.data
        return _FakeResponse()

    monkeypatch.setattr(up, "urlopen", fake_urlopen)
    # Bypass the cached server fixture's monkeypatch — call into the real
    # proxy_narrator_upstream so this test exercises env-loading + headers.
    result = up.proxy_narrator_upstream(
        "/v2/res/movie-sucai", method="GET", query_params={"page": 1}
    )

    assert result == {"code": 10000, "data": {}}
    # Header keys come back title-cased by urllib.request — match case-insensitively.
    headers_lower = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers_lower["app-key"] == "backend-master-key"
    # Defense-in-depth: the reseller key must not appear anywhere in the
    # outgoing request (header value, URL, or body).
    assert WEB_APP_KEY not in str(captured["headers"].values())
    assert WEB_APP_KEY not in captured["url"]
    if captured["body"]:
        assert WEB_APP_KEY.encode() not in captured["body"]
