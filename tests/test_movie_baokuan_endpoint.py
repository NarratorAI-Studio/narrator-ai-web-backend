"""Tests for upstream endpoint `GET /pricing/movie-baokuan`.

Strategy: monkeypatch `server.fetch_movie_baokuan` so the upstream call is
mocked, and run the DB tier against an in-memory sqlite engine with the
v2 pricing catalog schema.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text


sqlite3.register_adapter(Decimal, str)

REPO_ROOT = Path(__file__).resolve().parents[1]


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS pricing_template_v2 (
    template_id TEXT PRIMARY KEY,
    template_family_id TEXT,
    tier_multiplier NUMERIC(6, 4) NOT NULL,
    enabled INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS pricing_catalog_v2_entry (
    catalog_entry_id TEXT PRIMARY KEY,
    template_id TEXT NOT NULL,
    tier_code TEXT NOT NULL,
    product_line TEXT NOT NULL,
    mode TEXT,
    quality TEXT,
    flash_pro_axis TEXT NOT NULL,
    manual_price INTEGER NOT NULL,
    pro_surcharge_display INTEGER,
    system_reference_price INTEGER NOT NULL,
    currency_unit TEXT NOT NULL,
    raw_rate NUMERIC(10, 5) NOT NULL,
    final_rate NUMERIC(6, 2) NOT NULL,
    rounding_rule_version TEXT NOT NULL,
    manual_override_warning INTEGER NOT NULL,
    enabled INTEGER NOT NULL,
    effective_version INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    app_key TEXT PRIMARY KEY,
    balance_points NUMERIC NOT NULL DEFAULT 1000 CHECK (balance_points >= 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


TIER_CODES = (
    "derivative",
    "original_mix_flash",
    "original_mix_pro",
    "original_narration_flash",
    "original_narration_pro",
)


def _tier_shape(tier_code: str) -> dict[str, str | None]:
    if tier_code == "derivative":
        return {
            "product_line": "derivative",
            "mode": None,
            "quality": None,
            "flash_pro_axis": "optional",
        }
    _, mode, quality = tier_code.split("_")
    return {
        "product_line": "original",
        "mode": mode,
        "quality": quality,
        "flash_pro_axis": "required",
    }


def _seed_catalog(
    engine,
    template_ids: list[int],
    *,
    rounding_rule_version: str = "v2.0-round-half-up",
    missing_tiers: dict[int, set[str]] | None = None,
):
    """Insert pricing_template_v2 rows plus deterministic 5-tier catalog rows."""
    missing_tiers = missing_tiers or {}
    with engine.begin() as conn:
        for template_id in template_ids:
            conn.execute(
                text(
                    "INSERT INTO pricing_template_v2 "
                    "(template_id, template_family_id, tier_multiplier, enabled) "
                    "VALUES (:tid, NULL, 1.0000, 1)"
                ),
                {"tid": str(template_id)},
            )
            for tier_code in TIER_CODES:
                if tier_code in missing_tiers.get(template_id, set()):
                    continue
                idx = TIER_CODES.index(tier_code)
                shape = _tier_shape(tier_code)
                manual_price = idx * 100 + template_id
                system_reference_price = manual_price + 10
                conn.execute(
                    text(
                        "INSERT INTO pricing_catalog_v2_entry "
                        "(catalog_entry_id, template_id, tier_code, product_line, "
                        " mode, quality, flash_pro_axis, manual_price, "
                        " pro_surcharge_display, system_reference_price, "
                        " currency_unit, raw_rate, final_rate, rounding_rule_version, "
                        " manual_override_warning, enabled, effective_version, updated_by) "
                        "VALUES (:id, :tid, :tier_code, :product_line, :mode, "
                        " :quality, :axis, :manual_price, :surcharge, "
                        " :system_reference_price, 'web_point', 1.00000, 1.00, "
                        " :rrv, 0, 1, 1, 'test')"
                    ),
                    {
                        "id": f"{template_id}-{tier_code}-v1",
                        "tid": str(template_id),
                        "tier_code": tier_code,
                        "product_line": shape["product_line"],
                        "mode": shape["mode"],
                        "quality": shape["quality"],
                        "axis": shape["flash_pro_axis"],
                        "manual_price": manual_price,
                        "surcharge": 5 if tier_code.endswith("_pro") else None,
                        "system_reference_price": system_reference_price,
                        "rrv": rounding_rule_version,
                    },
                )


AUTH_TOKEN = "test-bff-token"
WEB_APP_KEY = "grid_AbCdEfGhIjKlMnOpQrStUv"  # valid grid_<22 base62> format
AUTH_HEADERS = {
    "Authorization": f"Bearer {AUTH_TOKEN}",
    "X-Web-App-Key": WEB_APP_KEY,
}


@pytest.fixture()
def sqlite_engine():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    with engine.begin() as conn:
        for stmt in SQLITE_SCHEMA.strip().split(";"):
            if stmt.strip():
                conn.execute(text(stmt))
        # Seed the canonical test user so AUTH_HEADERS passes through both
        # the Bearer check and the web-app-key middleware.
        conn.execute(
            text("INSERT INTO users (app_key, balance_points) VALUES (:k, 1000)"),
            {"k": WEB_APP_KEY},
        )
    yield engine
    engine.dispose()


@pytest.fixture()
def client(sqlite_engine, monkeypatch):
    import server

    monkeypatch.setattr(server, "get_db_engine", lambda: sqlite_engine)
    monkeypatch.setattr(server, "get_db_core_connection", lambda: sqlite_engine.connect())
    monkeypatch.setenv("PRICING_BFF_AUTH_TOKEN", AUTH_TOKEN)
    return server.app.test_client()


# ---------- happy path ----------


def test_returns_upstream_items_augmented_with_v2_catalog_tiers(
    sqlite_engine, client, monkeypatch
):
    _seed_catalog(sqlite_engine, [1, 89])

    upstream = {
        "code": 10000,
        "message": "success",
        "data": {
            "total": 2,
            "items": [
                {"id": 164, "code": "xy0001", "name": "Sample template A"},
                {"id": 200, "code": "xy0089", "name": "Sample template B"},
            ],
        },
    }
    import server
    monkeypatch.setattr(server, "fetch_movie_baokuan", lambda params: upstream)

    response = client.get(
        "/pricing/movie-baokuan?platform_id=2&page=1&size=20",
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 200

    body = response.get_json()
    assert body["code"] == 10000
    items = body["data"]["items"]
    assert len(items) == 2

    by_code = {item["code"]: item for item in items}
    # Flask 3.x DefaultJSONProvider serializes Decimal as str — matches the
    # existing /pricing/hard-price contract and upstream's `profit` field.
    # template_id=1 → original_narration_flash price = 0*100 + 1 + 0.5 = 1.5
    assert set(by_code["xy0001"]["tiers"]) == set(TIER_CODES)
    assert by_code["xy0001"]["tiers"]["original_narration_flash"]["manual_price"] == 301
    assert (
        by_code["xy0001"]["tiers"]["original_narration_flash"][
            "system_reference_price"
        ]
        == 311
    )
    assert (
        by_code["xy0001"]["tiers"]["original_narration_flash"][
            "pricing_rule_version"
        ]
        == "v2.0-round-half-up"
    )
    assert by_code["xy0001"]["pricing_rule_version"] == "v2.0-round-half-up"
    assert by_code["xy0089"]["tiers"]["derivative"]["manual_price"] == 89


def test_query_params_forwarded_to_upstream(client, monkeypatch):
    captured = {}

    def capture(params):
        captured.update(params)
        return {"code": 10000, "message": "success", "data": {"total": 0, "items": []}}

    import server
    monkeypatch.setattr(server, "fetch_movie_baokuan", capture)

    response = client.get(
        "/pricing/movie-baokuan?platform_id=2&category_id=4&name=test&page=3&size=15",
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 200
    assert captured["platform_id"] == "2"
    assert captured["category_id"] == "4"
    assert captured["name"] == "test"
    assert captured["page"] == "3"
    assert captured["size"] == "15"


# ---------- unmatched / missing-code items ----------


def test_item_without_complete_catalog_is_omitted(
    sqlite_engine, client, monkeypatch
):
    _seed_catalog(sqlite_engine, [1])

    upstream = {
        "code": 10000,
        "message": "success",
        "data": {
            "total": 2,
            "items": [
                {"id": 1, "code": "xy0001", "name": "priced"},
                {"id": 2, "code": "xy9999", "name": "catalog missing"},
            ],
        },
    }
    import server
    monkeypatch.setattr(server, "fetch_movie_baokuan", lambda params: upstream)

    body = client.get("/pricing/movie-baokuan", headers=AUTH_HEADERS).get_json()
    items = body["data"]["items"]
    assert len(items) == 1
    by_code = {item["code"]: item for item in items}
    assert by_code["xy0001"]["tiers"] is not None
    assert "xy9999" not in by_code
    # Preserve upstream total for pagination; filtering only changes the
    # current page's item list.
    assert body["data"]["total"] == 2


def test_total_preserves_upstream_count_when_all_items_dropped(
    sqlite_engine, client, monkeypatch
):
    """Edge case: no item survives the catalog filter — total should
    drop to 0, not stay at the upstream pre-filter value."""
    _seed_catalog(sqlite_engine, [])  # no catalog rows seeded

    upstream = {
        "code": 10000,
        "message": "success",
        "data": {
            "total": 3,
            "items": [
                {"id": 1, "code": "xy0001", "name": "catalog missing a"},
                {"id": 2, "code": "xy0002", "name": "catalog missing b"},
                {"id": 3, "code": "xy0003", "name": "catalog missing c"},
            ],
        },
    }
    import server
    monkeypatch.setattr(server, "fetch_movie_baokuan", lambda params: upstream)

    body = client.get("/pricing/movie-baokuan", headers=AUTH_HEADERS).get_json()
    assert body["data"]["items"] == []
    assert body["data"]["total"] == 3


def test_item_with_null_code_is_omitted(sqlite_engine, client, monkeypatch):
    """自定义 placeholder rows (code=None) must not bring down the response."""
    _seed_catalog(sqlite_engine, [1])

    upstream = {
        "code": 10000,
        "message": "success",
        "data": {
            "total": 1,
            "items": [{"id": 1, "code": None, "name": "自定义"}],
        },
    }
    import server
    monkeypatch.setattr(server, "fetch_movie_baokuan", lambda params: upstream)

    body = client.get("/pricing/movie-baokuan", headers=AUTH_HEADERS).get_json()
    assert body["data"]["items"] == []


def test_item_with_incomplete_catalog_is_omitted(sqlite_engine, client, monkeypatch):
    _seed_catalog(
        sqlite_engine,
        [1, 2],
        missing_tiers={2: {"original_mix_pro"}},
    )

    upstream = {
        "code": 10000,
        "message": "success",
        "data": {
            "total": 2,
            "items": [
                {"id": 1, "code": "xy0001", "name": "complete"},
                {"id": 2, "code": "xy0002", "name": "incomplete"},
            ],
        },
    }
    import server
    monkeypatch.setattr(server, "fetch_movie_baokuan", lambda params: upstream)

    body = client.get("/pricing/movie-baokuan", headers=AUTH_HEADERS).get_json()
    assert [item["code"] for item in body["data"]["items"]] == ["xy0001"]


def test_empty_upstream_items_returns_empty_list(client, monkeypatch):
    upstream = {"code": 10000, "message": "success", "data": {"total": 0, "items": []}}
    import server
    monkeypatch.setattr(server, "fetch_movie_baokuan", lambda params: upstream)

    body = client.get("/pricing/movie-baokuan", headers=AUTH_HEADERS).get_json()
    assert body["code"] == 10000
    assert body["data"]["items"] == []


# ---------- upstream failure modes ----------


def test_upstream_non_10000_code_is_forwarded(client, monkeypatch):
    """If upstream itself reports a business error, forward it untouched —
    Web already knows how to display upstream errors."""
    upstream = {"code": 40001, "message": "invalid platform_id", "data": None}
    import server
    monkeypatch.setattr(server, "fetch_movie_baokuan", lambda params: upstream)

    response = client.get(
        "/pricing/movie-baokuan?platform_id=999", headers=AUTH_HEADERS
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["code"] == 40001
    assert body["message"] == "invalid platform_id"


def test_upstream_timeout_returns_504(client, monkeypatch):
    from pricing.upstream_baokuan import UpstreamBaokuanError
    import server

    def raise_timeout(params):
        raise UpstreamBaokuanError(
            504, "UPSTREAM_TIMEOUT", "timeout", retryable=True
        )

    monkeypatch.setattr(server, "fetch_movie_baokuan", raise_timeout)

    response = client.get("/pricing/movie-baokuan", headers=AUTH_HEADERS)
    assert response.status_code == 504
    body = response.get_json()
    assert body["error"]["code"] == "UPSTREAM_TIMEOUT"
    assert body["error"]["retryable"] is True


def test_upstream_not_configured_returns_503(client, monkeypatch):
    from pricing.upstream_baokuan import UpstreamBaokuanError
    import server

    def raise_not_configured(params):
        raise UpstreamBaokuanError(
            503,
            "UPSTREAM_NOT_CONFIGURED",
            "missing env",
            retryable=False,
            details={"base_set": False, "app_key_set": False},
        )

    monkeypatch.setattr(server, "fetch_movie_baokuan", raise_not_configured)

    response = client.get("/pricing/movie-baokuan", headers=AUTH_HEADERS)
    assert response.status_code == 503
    body = response.get_json()
    assert body["error"]["code"] == "UPSTREAM_NOT_CONFIGURED"
    assert body["error"]["retryable"] is False


# ---------- upstream payload schema validation ----------


def test_upstream_data_not_object_returns_502_schema_error(client, monkeypatch):
    """Upstream claims success (code=10000) but `data` is a string — without
    validation this would crash the route with AttributeError. With the
    schema guard, surface as 502 UPSTREAM_SCHEMA_ERROR (non-retryable)."""
    upstream = {"code": 10000, "message": "success", "data": "not-an-object"}
    import server
    monkeypatch.setattr(server, "fetch_movie_baokuan", lambda params: upstream)

    response = client.get("/pricing/movie-baokuan", headers=AUTH_HEADERS)
    assert response.status_code == 502
    body = response.get_json()
    assert body["error"]["code"] == "UPSTREAM_SCHEMA_ERROR"
    assert body["error"]["retryable"] is False
    assert "data" in body["error"]["details"]["reason"]


def test_upstream_data_null_with_success_code_returns_502(client, monkeypatch):
    """Some legitimate upstream APIs use `data: null` for empty results, but
    when paired with code=10000 it violates the documented contract that
    success always carries {total, items}. Surface 502 rather than silently
    returning a contract-broken response."""
    upstream = {"code": 10000, "message": "success", "data": None}
    import server
    monkeypatch.setattr(server, "fetch_movie_baokuan", lambda params: upstream)

    response = client.get("/pricing/movie-baokuan", headers=AUTH_HEADERS)
    assert response.status_code == 502
    body = response.get_json()
    assert body["error"]["code"] == "UPSTREAM_SCHEMA_ERROR"


def test_upstream_items_not_list_returns_502_schema_error(client, monkeypatch):
    """`data.items` must be a list. A string or null here would crash item
    iteration; surface as 502 instead of unaugmented forwarding."""
    upstream = {
        "code": 10000,
        "message": "success",
        "data": {"total": 0, "items": "should-be-a-list"},
    }
    import server
    monkeypatch.setattr(server, "fetch_movie_baokuan", lambda params: upstream)

    response = client.get("/pricing/movie-baokuan", headers=AUTH_HEADERS)
    assert response.status_code == 502
    body = response.get_json()
    assert body["error"]["code"] == "UPSTREAM_SCHEMA_ERROR"
    assert "items" in body["error"]["details"]["reason"]


def test_upstream_payload_not_object_returns_502(client, monkeypatch):
    """A non-dict top-level payload (e.g. an array) must NOT leak as 200
    business-error pass-through — return 502 SCHEMA_ERROR so Web's
    documented MovieBaokuanResponse contract holds at the boundary."""
    import server
    monkeypatch.setattr(server, "fetch_movie_baokuan", lambda params: ["not", "an", "object"])

    response = client.get("/pricing/movie-baokuan", headers=AUTH_HEADERS)
    assert response.status_code == 502
    body = response.get_json()
    assert body["error"]["code"] == "UPSTREAM_SCHEMA_ERROR"
    assert "payload" in body["error"]["details"]["reason"]


def test_upstream_items_contains_non_object_entry_returns_502(client, monkeypatch):
    """A single bad item shouldn't be silently skipped — every entry is part
    of the contract. Returning the response with some items augmented and
    others missing tiers would break Web's UI assumptions."""
    upstream = {
        "code": 10000,
        "message": "success",
        "data": {
            "total": 2,
            "items": [
                {"id": 1, "code": "xy0001", "name": "ok"},
                42,  # malformed entry
            ],
        },
    }
    import server
    monkeypatch.setattr(server, "fetch_movie_baokuan", lambda params: upstream)

    response = client.get("/pricing/movie-baokuan", headers=AUTH_HEADERS)
    assert response.status_code == 502
    body = response.get_json()
    assert body["error"]["code"] == "UPSTREAM_SCHEMA_ERROR"
    assert "items[1]" in body["error"]["details"]["reason"]


# ---------- Web→backend authorization (HP-XX AC.4) ----------


def test_missing_authorization_header_returns_401(client, monkeypatch):
    """No Authorization header → 401, and upstream is NOT called (prevents
    burning upstream quota on unauthorized traffic)."""
    called = []
    import server
    monkeypatch.setattr(
        server, "fetch_movie_baokuan", lambda params: called.append(params) or {}
    )

    response = client.get("/pricing/movie-baokuan")
    assert response.status_code == 401
    body = response.get_json()
    assert body["error"]["code"] == "UNAUTHORIZED"
    assert body["error"]["retryable"] is False
    assert called == []  # upstream client was not invoked


def test_wrong_authorization_header_returns_401(client, monkeypatch):
    called = []
    import server
    monkeypatch.setattr(
        server, "fetch_movie_baokuan", lambda params: called.append(params) or {}
    )

    response = client.get(
        "/pricing/movie-baokuan",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "UNAUTHORIZED"
    assert called == []


def test_non_ascii_pricing_bff_token_treated_as_unconfigured(
    sqlite_engine, monkeypatch
):
    """`hmac.compare_digest` raises on non-ASCII str args. Treat a non-ASCII
    server token as misconfigured (503) so it can never trigger a 500
    (review security-sensitive: cheap unauthenticated 500 path)."""
    import server

    monkeypatch.setattr(server, "get_db_engine", lambda: sqlite_engine)
    monkeypatch.setattr(
        server, "get_db_core_connection", lambda: sqlite_engine.connect()
    )
    monkeypatch.setenv("PRICING_BFF_AUTH_TOKEN", "中文-token")
    called = []
    monkeypatch.setattr(
        server, "fetch_movie_baokuan", lambda params: called.append(params) or {}
    )

    response = server.app.test_client().get(
        "/pricing/movie-baokuan",
        headers={"Authorization": "Bearer 中文-token"},
    )
    assert response.status_code == 503
    body = response.get_json()
    assert body["error"]["code"] == "PRICING_BFF_NOT_CONFIGURED"
    assert called == []


def test_non_ascii_authorization_header_returns_401_not_500(client, monkeypatch):
    """A request with a non-ASCII Authorization header must NOT crash the
    compare with TypeError → 500. It's an unauthorized request, period."""
    called = []
    import server
    monkeypatch.setattr(
        server, "fetch_movie_baokuan", lambda params: called.append(params) or {}
    )

    response = client.get(
        "/pricing/movie-baokuan",
        headers={"Authorization": "Bearer 中文-token"},
    )
    assert response.status_code == 401
    body = response.get_json()
    assert body["error"]["code"] == "UNAUTHORIZED"
    assert called == []  # upstream never invoked


def test_unconfigured_pricing_bff_token_returns_503(sqlite_engine, monkeypatch):
    """When PRICING_BFF_AUTH_TOKEN is unset on the server we refuse the
    request (503 retryable) — fail-closed rather than open."""
    import server

    monkeypatch.setattr(server, "get_db_engine", lambda: sqlite_engine)
    monkeypatch.setattr(
        server, "get_db_core_connection", lambda: sqlite_engine.connect()
    )
    monkeypatch.delenv("PRICING_BFF_AUTH_TOKEN", raising=False)
    called = []
    monkeypatch.setattr(
        server, "fetch_movie_baokuan", lambda params: called.append(params) or {}
    )

    response = server.app.test_client().get(
        "/pricing/movie-baokuan", headers=AUTH_HEADERS
    )
    assert response.status_code == 503
    body = response.get_json()
    assert body["error"]["code"] == "PRICING_BFF_NOT_CONFIGURED"
    assert body["error"]["retryable"] is True
    assert called == []


# ---------- web-app-key middleware integration on movie-baokuan  ----------


def test_movie_baokuan_rejects_missing_web_app_key(client, monkeypatch):
    """Bearer present, X-Web-App-Key absent → 401 WEB_APP_KEY_MISSING.
    Upstream must not be invoked."""
    called = []
    import server

    monkeypatch.setattr(
        server, "fetch_movie_baokuan", lambda params: called.append(params) or {}
    )

    response = client.get(
        "/pricing/movie-baokuan",
        headers={"Authorization": f"Bearer {AUTH_TOKEN}"},  # no X-Web-App-Key
    )
    assert response.status_code == 401
    body = response.get_json()
    assert body["error"]["code"] == "WEB_APP_KEY_MISSING"
    assert called == []


def test_movie_baokuan_rejects_unknown_web_app_key(client, monkeypatch):
    """Bearer present, X-Web-App-Key well-formed but not in users → 401
    WEB_APP_KEY_UNKNOWN. Upstream must not be invoked."""
    called = []
    import server

    monkeypatch.setattr(
        server, "fetch_movie_baokuan", lambda params: called.append(params) or {}
    )

    response = client.get(
        "/pricing/movie-baokuan",
        headers={
            "Authorization": f"Bearer {AUTH_TOKEN}",
            "X-Web-App-Key": "grid_ZyXwVuTsRqPoNmLkJiHgFe",  # valid format, not seeded
        },
    )
    assert response.status_code == 401
    body = response.get_json()
    assert body["error"]["code"] == "WEB_APP_KEY_UNKNOWN"
    assert called == []


# ---------- upstream client unit tests (no mock, but config-only) ----------


def test_load_upstream_config_raises_when_missing(monkeypatch):
    from pricing.upstream_baokuan import load_upstream_config, UpstreamBaokuanError

    monkeypatch.delenv("OPEN_FASTAPI_BASE", raising=False)
    monkeypatch.delenv("OPEN_FASTAPI_APP_KEY", raising=False)

    with pytest.raises(UpstreamBaokuanError) as exc:
        load_upstream_config()
    assert exc.value.http_status == 503
    assert exc.value.code == "UPSTREAM_NOT_CONFIGURED"


def test_load_upstream_config_strips_trailing_slash(monkeypatch):
    from pricing.upstream_baokuan import load_upstream_config

    monkeypatch.setenv("OPEN_FASTAPI_BASE", "https://openapi.example.com/")
    monkeypatch.setenv("OPEN_FASTAPI_APP_KEY", "test-key")
    cfg = load_upstream_config()
    assert cfg.base_url == "https://openapi.example.com"
    assert cfg.app_key == "test-key"


def test_load_upstream_config_defaults_invalid_timeout(monkeypatch):
    from pricing.upstream_baokuan import load_upstream_config

    monkeypatch.setenv("OPEN_FASTAPI_BASE", "https://openapi.example.com")
    monkeypatch.setenv("OPEN_FASTAPI_APP_KEY", "k")
    monkeypatch.setenv("OPEN_FASTAPI_TIMEOUT_SECONDS", "not-a-number")
    cfg = load_upstream_config()
    assert cfg.timeout_seconds == 60.0


class _FakeUpstreamResponse:
    """Minimal stand-in for an urllib HTTPResponse — supports the read(N)
    + headers + status surface fetch_movie_baokuan touches, plus context
    manager protocol."""

    def __init__(self, body: bytes, content_length_header: str | None, status: int = 200):
        self._body = body
        self._read_called = False
        self.status = status
        self.headers = {"Content-Length": content_length_header} if content_length_header is not None else {}

    def read(self, n: int = -1) -> bytes:
        self._read_called = True
        if n < 0 or n >= len(self._body):
            return self._body
        return self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_movie_baokuan_rejects_oversized_content_length(monkeypatch):
    """Declared Content-Length > MAX_UPSTREAM_BYTES → 502 RESPONSE_TOO_LARGE,
    and we never call .read() on the body."""
    from pricing import upstream_baokuan
    from pricing.upstream_baokuan import (
        MAX_UPSTREAM_BYTES,
        UpstreamBaokuanError,
        fetch_movie_baokuan,
    )

    monkeypatch.setenv("OPEN_FASTAPI_BASE", "https://openapi.example.com")
    monkeypatch.setenv("OPEN_FASTAPI_APP_KEY", "k")

    fake = _FakeUpstreamResponse(b"x" * 10, content_length_header=str(MAX_UPSTREAM_BYTES + 1))
    monkeypatch.setattr(upstream_baokuan, "urlopen", lambda *a, **kw: fake)

    with pytest.raises(UpstreamBaokuanError) as exc:
        fetch_movie_baokuan({})
    assert exc.value.http_status == 502
    assert exc.value.code == "UPSTREAM_RESPONSE_TOO_LARGE"
    assert exc.value.retryable is False
    assert exc.value.details["content_length"] == MAX_UPSTREAM_BYTES + 1
    assert not fake._read_called  # short-circuited before reading body


def test_fetch_movie_baokuan_rejects_oversized_actual_body(monkeypatch):
    """No Content-Length header (chunked / weird upstream) — read MAX+1 bytes
    and reject if we got more than MAX. Guards against lying / missing
    headers."""
    from pricing import upstream_baokuan
    from pricing.upstream_baokuan import (
        MAX_UPSTREAM_BYTES,
        UpstreamBaokuanError,
        fetch_movie_baokuan,
    )

    monkeypatch.setenv("OPEN_FASTAPI_BASE", "https://openapi.example.com")
    monkeypatch.setenv("OPEN_FASTAPI_APP_KEY", "k")

    # Body 1 byte beyond cap, no Content-Length advertised.
    body = b"x" * (MAX_UPSTREAM_BYTES + 1)
    fake = _FakeUpstreamResponse(body, content_length_header=None)
    monkeypatch.setattr(upstream_baokuan, "urlopen", lambda *a, **kw: fake)

    with pytest.raises(UpstreamBaokuanError) as exc:
        fetch_movie_baokuan({})
    assert exc.value.http_status == 502
    assert exc.value.code == "UPSTREAM_RESPONSE_TOO_LARGE"


def test_fetch_movie_baokuan_accepts_response_under_cap(monkeypatch):
    """Sanity: a normal-sized JSON response goes through and is parsed."""
    from pricing import upstream_baokuan
    from pricing.upstream_baokuan import fetch_movie_baokuan

    monkeypatch.setenv("OPEN_FASTAPI_BASE", "https://openapi.example.com")
    monkeypatch.setenv("OPEN_FASTAPI_APP_KEY", "k")

    body = b'{"code": 10000, "message": "success", "data": {"total": 0, "items": []}}'
    fake = _FakeUpstreamResponse(body, content_length_header=str(len(body)))
    monkeypatch.setattr(upstream_baokuan, "urlopen", lambda *a, **kw: fake)

    payload = fetch_movie_baokuan({})
    assert payload["code"] == 10000
    assert payload["data"]["items"] == []


# ---------- baokuan_query unit tests ----------


def test_template_id_from_baokuan_code_normalizes_xy_code():
    from pricing.baokuan_query import template_id_from_baokuan_code

    assert template_id_from_baokuan_code("xy0001") == "1"
    assert template_id_from_baokuan_code("XY0046") == "46"
    assert template_id_from_baokuan_code(None) is None
    assert template_id_from_baokuan_code("cf_some_template") is None


def test_group_v2_catalog_tiers_by_template_id_requires_complete_five_tiers():
    from pricing.baokuan_query import group_v2_catalog_tiers_by_template_id

    rows = [
        {
            "template_id": "1",
            "tier_code": tier_code,
            "product_line": _tier_shape(tier_code)["product_line"],
            "mode": _tier_shape(tier_code)["mode"],
            "quality": _tier_shape(tier_code)["quality"],
            "flash_pro_axis": _tier_shape(tier_code)["flash_pro_axis"],
            "manual_price": idx + 1,
            "pro_surcharge_display": None,
            "system_reference_price": idx + 11,
            "currency_unit": "web_point",
            "raw_rate": Decimal("1.00000"),
            "final_rate": Decimal("1.00"),
            "rounding_rule_version": "v2.0-round-half-up",
            "manual_override_warning": False,
            "effective_version": 1,
        }
        for idx, tier_code in enumerate(TIER_CODES)
    ]
    rows.append({**rows[0], "template_id": "2"})

    grouped = group_v2_catalog_tiers_by_template_id(rows)
    assert set(grouped) == {"1"}
    assert set(grouped["1"]["tiers"]) == set(TIER_CODES)
    assert grouped["1"]["tiers"]["derivative"]["system_reference_price"] == 11
    assert grouped["1"]["pricing_rule_version"] == "v2.0-round-half-up"
