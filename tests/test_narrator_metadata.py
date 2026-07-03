"""Tests for the 8 narrator-metadata wrapper endpoints (the implementation requirement).

Strategy: monkeypatch `server.fetch_narrator_upstream` so the upstream
call is mocked, and run with an in-memory sqlite users table so the
`X-Web-App-Key` middleware  lets the canonical test user through.

The 8 routes share a single handler (`_serve_narrator_metadata`) and a
single registry (`narrator_metadata.endpoints.ROUTES`), so most assertions
are parameterized across all routes — `route_meta` drives the test data.
Per-endpoint contract-specific tests (e.g. `page` / `size` forwarding on
the v2 list routes) are kept narrow.
"""

from __future__ import annotations

import socket
import sqlite3
from decimal import Decimal
from urllib.error import URLError

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


# Imported here so parametrize can read `ROUTES` at collection time.
from narrator_metadata.endpoints import ROUTES  # noqa: E402
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


# ---------- registration: all 8 routes are reachable ----------


def test_all_routes_registered(client):
    """Every entry in `ROUTES` produces a Flask URL rule. Guards against
    accidental drop / rename when editing `endpoints.py`."""
    import server

    registered = {rule.rule for rule in server.app.url_map.iter_rules()}
    for route_meta in ROUTES:
        assert route_meta.backend_path in registered, (
            f"Route {route_meta.backend_path} not registered"
        )


# ---------- happy path: per-route mocked upstream ----------


@pytest.mark.parametrize("route_meta", ROUTES, ids=lambda r: r.endpoint_label)
def test_happy_path_forwards_upstream_payload_verbatim(
    client, monkeypatch, route_meta
):
    """Upstream returned-payload is forwarded to the client verbatim.
    Confirms (a) the route is wired, (b) Bearer + X-Web-App-Key pass,
    (c) the handler returns 200 + JSON body identical to upstream."""
    payload = {"code": 10000, "data": [{"id": 1, "name": "fixture"}]}
    captured = {}

    def fake_fetch(upstream_path, params, supported_params, config=None):
        captured["upstream_path"] = upstream_path
        captured["supported_params"] = supported_params
        return payload

    import server

    monkeypatch.setattr(server, "fetch_narrator_upstream", fake_fetch)

    response = client.get(route_meta.backend_path, headers=AUTH_HEADERS)
    assert response.status_code == 200, response.get_json()
    assert response.get_json() == payload
    # The handler must call the helper with the registry's upstream path
    # (not the backend path).
    assert captured["upstream_path"] == route_meta.upstream_path
    assert captured["supported_params"] == route_meta.supported_params


# ---------- query-parameter forwarding ----------


def test_param_forwarding_v1_terminal_type(client, monkeypatch):
    """`terminal_type` is forwarded to upstream for narrator-types /
    model-versions routes."""
    captured = {}

    def fake_fetch(upstream_path, params, supported_params, config=None):
        captured["params"] = params
        return {"code": 10000, "data": []}

    import server

    monkeypatch.setattr(server, "fetch_narrator_upstream", fake_fetch)

    response = client.get(
        "/narrator/narrator-types?terminal_type=web",
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 200
    assert captured["params"] == {"terminal_type": "web"}


def test_param_forwarding_v2_page_size(client, monkeypatch):
    """`page` / `size` are forwarded for bgm-list / dubbing-list."""
    captured = {}

    def fake_fetch(upstream_path, params, supported_params, config=None):
        captured["params"] = params
        return {"code": 10000, "data": {"items": []}}

    import server

    monkeypatch.setattr(server, "fetch_narrator_upstream", fake_fetch)

    response = client.get(
        "/narrator/bgm-list?page=2&size=50",
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 200
    assert captured["params"] == {"page": "2", "size": "50"}


def test_param_filter_drops_undocumented(client, monkeypatch):
    """Routes with no supported params (e.g. /narrator/types) ignore any
    query keys sent by the caller; routes with supported params drop
    everything else.

    This is defense against SSRF / parameter smuggling — the upstream
    fetcher only forwards what's in the allowlist.
    """
    captured = {}

    def fake_fetch(upstream_path, params, supported_params, config=None):
        captured["params"] = params
        return {"code": 10000, "data": []}

    import server

    monkeypatch.setattr(server, "fetch_narrator_upstream", fake_fetch)

    # `types` declares no supported params, so even passing `page` should
    # come through as `{"page": None}` in the handler-collected params (the
    # fetcher would then filter it; the dict is built from supported_params
    # only in the handler).
    response = client.get(
        "/narrator/types?page=1&injected=xxx",
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 200
    # Handler builds params from supported_params only, so an empty dict
    # is the right contract here.
    assert captured["params"] == {}


# ---------- auth boundary ----------


def test_rejects_missing_bearer(client):
    """No Authorization header → 401 UNAUTHORIZED (Bearer-tier failure)."""
    response = client.get(
        "/narrator/types",
        headers={"X-Web-App-Key": WEB_APP_KEY},
    )
    assert response.status_code == 401
    body = response.get_json()
    assert body["error"]["code"] == "UNAUTHORIZED"


def test_rejects_missing_web_app_key(client):
    """Bearer but no X-Web-App-Key → 401 WEB_APP_KEY_MISSING."""
    response = client.get(
        "/narrator/types",
        headers={"Authorization": f"Bearer {AUTH_TOKEN}"},
    )
    assert response.status_code == 401
    body = response.get_json()
    assert body["error"]["code"] == "WEB_APP_KEY_MISSING"


def test_rejects_unknown_web_app_key(client):
    """X-Web-App-Key not present in users table → 401 WEB_APP_KEY_UNKNOWN."""
    response = client.get(
        "/narrator/types",
        headers={
            "Authorization": f"Bearer {AUTH_TOKEN}",
            "X-Web-App-Key": "grid_ZZZZZZZZZZZZZZZZZZZZZZ",
        },
    )
    assert response.status_code == 401
    body = response.get_json()
    assert body["error"]["code"] == "WEB_APP_KEY_UNKNOWN"


# ---------- upstream-failure mapping ----------


def _raise_upstream_error(http_status, code, message, *, retryable=False):
    """Helper: return a fetch-fake that raises a typed upstream error."""

    def fake_fetch(*args, **kwargs):
        raise UpstreamNarratorError(
            http_status, code, message, retryable=retryable
        )

    return fake_fetch


def test_upstream_timeout_maps_to_504(client, monkeypatch):
    import server

    monkeypatch.setattr(
        server,
        "fetch_narrator_upstream",
        _raise_upstream_error(
            504, "UPSTREAM_TIMEOUT", "timed out", retryable=True
        ),
    )

    response = client.get("/narrator/types", headers=AUTH_HEADERS)
    assert response.status_code == 504
    body = response.get_json()
    assert body["error"]["code"] == "UPSTREAM_TIMEOUT"
    assert body["error"]["retryable"] is True


def test_upstream_unreachable_maps_to_502(client, monkeypatch):
    import server

    monkeypatch.setattr(
        server,
        "fetch_narrator_upstream",
        _raise_upstream_error(
            502, "UPSTREAM_UNREACHABLE", "ECONNREFUSED", retryable=True
        ),
    )

    response = client.get("/narrator/types", headers=AUTH_HEADERS)
    assert response.status_code == 502
    body = response.get_json()
    assert body["error"]["code"] == "UPSTREAM_UNREACHABLE"


def test_upstream_http_5xx_maps_to_503(client, monkeypatch):
    import server

    monkeypatch.setattr(
        server,
        "fetch_narrator_upstream",
        _raise_upstream_error(
            503, "UPSTREAM_HTTP_ERROR", "upstream HTTP 502", retryable=True
        ),
    )

    response = client.get("/narrator/models", headers=AUTH_HEADERS)
    assert response.status_code == 503
    body = response.get_json()
    assert body["error"]["code"] == "UPSTREAM_HTTP_ERROR"


def test_upstream_decode_error_maps_to_502(client, monkeypatch):
    import server

    monkeypatch.setattr(
        server,
        "fetch_narrator_upstream",
        _raise_upstream_error(
            502, "UPSTREAM_DECODE_ERROR", "non-JSON body"
        ),
    )

    response = client.get("/narrator/bgm", headers=AUTH_HEADERS)
    assert response.status_code == 502
    body = response.get_json()
    assert body["error"]["code"] == "UPSTREAM_DECODE_ERROR"


def test_upstream_response_too_large_maps_to_502(client, monkeypatch):
    import server

    monkeypatch.setattr(
        server,
        "fetch_narrator_upstream",
        _raise_upstream_error(
            502, "UPSTREAM_RESPONSE_TOO_LARGE", "over 2MB"
        ),
    )

    response = client.get("/narrator/template-meta", headers=AUTH_HEADERS)
    assert response.status_code == 502
    body = response.get_json()
    assert body["error"]["code"] == "UPSTREAM_RESPONSE_TOO_LARGE"


# ---------- fetch_narrator_upstream unit-level (no Flask) ----------


def test_fetch_narrator_upstream_filters_unsupported_params():
    """The helper drops query keys not in `supported_params` before
    building the upstream URL. Tests the param-filter directly without
    going through Flask."""
    from narrator_metadata.upstream import _build_query_string

    query = _build_query_string(
        {"page": "1", "size": "20", "injected": "xxx", "terminal_type": "web"},
        ("page", "size"),
    )
    assert "page=1" in query
    assert "size=20" in query
    assert "injected" not in query
    assert "terminal_type" not in query


def test_fetch_narrator_upstream_empty_params_yields_no_query():
    from narrator_metadata.upstream import _build_query_string

    query = _build_query_string({}, ())
    assert query == ""


def test_fetch_narrator_upstream_drops_none_and_empty_string():
    """`None` and `""` should not appear in the query — they're "param
    absent" sentinels, not literal empty-string values."""
    from narrator_metadata.upstream import _build_query_string

    query = _build_query_string(
        {"page": None, "size": "", "terminal_type": "web"},
        ("page", "size", "terminal_type"),
    )
    assert query == "terminal_type=web"


def test_fetch_narrator_upstream_raises_when_unconfigured(monkeypatch):
    """Without OPEN_FASTAPI_BASE / OPEN_FASTAPI_APP_KEY env, the helper
    raises UPSTREAM_NOT_CONFIGURED 503 instead of attempting a wire
    call."""
    from narrator_metadata.upstream import fetch_narrator_upstream

    monkeypatch.delenv("OPEN_FASTAPI_BASE", raising=False)
    monkeypatch.delenv("OPEN_FASTAPI_APP_KEY", raising=False)

    with pytest.raises(UpstreamNarratorError) as exc_info:
        fetch_narrator_upstream("/v1/narrator/types", {}, ())
    assert exc_info.value.http_status == 503
    assert exc_info.value.code == "UPSTREAM_NOT_CONFIGURED"


def test_fetch_narrator_upstream_maps_socket_timeout(monkeypatch):
    """`socket.timeout` from urlopen → UPSTREAM_TIMEOUT 504 retryable."""
    from narrator_metadata import upstream as up

    monkeypatch.setenv("OPEN_FASTAPI_BASE", "https://example.invalid")
    monkeypatch.setenv("OPEN_FASTAPI_APP_KEY", "test-key")

    def fake_urlopen(*args, **kwargs):
        raise socket.timeout("read timed out")

    monkeypatch.setattr(up, "urlopen", fake_urlopen)

    with pytest.raises(UpstreamNarratorError) as exc_info:
        up.fetch_narrator_upstream("/v1/narrator/types", {}, ())
    assert exc_info.value.http_status == 504
    assert exc_info.value.code == "UPSTREAM_TIMEOUT"
    assert exc_info.value.retryable is True


def test_fetch_narrator_upstream_maps_url_error(monkeypatch):
    """`URLError` from urlopen → UPSTREAM_UNREACHABLE 502 retryable."""
    from narrator_metadata import upstream as up

    monkeypatch.setenv("OPEN_FASTAPI_BASE", "https://example.invalid")
    monkeypatch.setenv("OPEN_FASTAPI_APP_KEY", "test-key")

    def fake_urlopen(*args, **kwargs):
        raise URLError("DNS lookup failed")

    monkeypatch.setattr(up, "urlopen", fake_urlopen)

    with pytest.raises(UpstreamNarratorError) as exc_info:
        up.fetch_narrator_upstream("/v1/narrator/types", {}, ())
    assert exc_info.value.http_status == 502
    assert exc_info.value.code == "UPSTREAM_UNREACHABLE"
