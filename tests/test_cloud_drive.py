"""Integration tests for /cloud-drive/* backend routes ."""

from __future__ import annotations

import os
import queue
import sqlite3
import threading
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text


sqlite3.register_adapter(Decimal, str)


KEY_A = "grid_AbCdEfGhIjKlMnOpQrStUv"
KEY_B = "grid_ZyXwVuTsRqPoNmLkJiHgFe"
AUTH_TOKEN = "test-bff-token"
AUTH_HEADERS_A = {
    "Authorization": f"Bearer {AUTH_TOKEN}",
    "X-Web-App-Key": KEY_A,
    "Content-Type": "application/json",
}
AUTH_HEADERS_B = {
    "Authorization": f"Bearer {AUTH_TOKEN}",
    "X-Web-App-Key": KEY_B,
    "Content-Type": "application/json",
}


SQLITE_SCHEMA = """
CREATE TABLE users (
    app_key TEXT PRIMARY KEY,
    id INTEGER NOT NULL UNIQUE,
    balance_points NUMERIC NOT NULL DEFAULT 1000 CHECK (balance_points >= 0),
    nickname TEXT,
    mobile TEXT,
    email TEXT,
    company_name TEXT,
    cloud_drive_quota_bytes INTEGER NOT NULL DEFAULT 3221225472,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE user_cloud_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    app_key TEXT NOT NULL,
    file_id TEXT UNIQUE,
    object_key TEXT,
    file_name TEXT NOT NULL,
    suffix TEXT NOT NULL DEFAULT '',
    category INTEGER NOT NULL DEFAULT 0,
    file_size INTEGER NOT NULL DEFAULT 0 CHECK (file_size >= 0),
    content_type TEXT,
    source TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'reserved', 'completed', 'failed', 'delete_pending', 'deleted',
            'transfer_pending', 'transfer_running', 'transfer_completed'
        )
    ),
    upstream_status INTEGER,
    progress INTEGER NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 100),
    srt_file_hash TEXT,
    upstream_payload TEXT NOT NULL DEFAULT '{}',
    upload_id TEXT,
    parent_reservation_id TEXT,
    settled_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    deleted_at TEXT
);
"""


POSTGRES_SCHEMA = """
CREATE TABLE users (
    app_key TEXT PRIMARY KEY,
    id INTEGER NOT NULL UNIQUE,
    balance_points NUMERIC(18, 2) NOT NULL DEFAULT 1000 CHECK (balance_points >= 0),
    nickname TEXT,
    mobile TEXT,
    email TEXT,
    company_name TEXT,
    cloud_drive_quota_bytes BIGINT NOT NULL DEFAULT 3221225472,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE user_cloud_files (
    id BIGSERIAL PRIMARY KEY,
    reservation_id TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    app_key TEXT NOT NULL,
    file_id TEXT UNIQUE,
    object_key TEXT,
    file_name TEXT NOT NULL,
    suffix TEXT NOT NULL DEFAULT '',
    category INTEGER NOT NULL DEFAULT 0,
    file_size BIGINT NOT NULL DEFAULT 0 CHECK (file_size >= 0),
    content_type TEXT,
    source TEXT NOT NULL CHECK (source IN ('local_upload', 'transfer')),
    status TEXT NOT NULL CHECK (
        status IN (
            'reserved', 'completed', 'failed', 'delete_pending', 'deleted',
            'transfer_pending', 'transfer_running', 'transfer_completed'
        )
    ),
    upstream_status INTEGER,
    progress INTEGER NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 100),
    srt_file_hash TEXT,
    upstream_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    upload_id TEXT,
    parent_reservation_id TEXT,
    settled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ
);
"""


@pytest.fixture()
def sqlite_engine():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    with engine.begin() as conn:
        for statement in SQLITE_SCHEMA.split(";"):
            if statement.strip():
                conn.execute(text(statement))
        conn.execute(
            text(
                "INSERT INTO users (app_key, id, cloud_drive_quota_bytes) "
                "VALUES (:k, 1, 100)"
            ),
            {"k": KEY_A},
        )
        conn.execute(
            text(
                "INSERT INTO users (app_key, id, cloud_drive_quota_bytes) "
                "VALUES (:k, 2, 100)"
            ),
            {"k": KEY_B},
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


def test_upload_url_reserves_quota_and_persists_file_id(client, monkeypatch):
    captured = {}

    def fake_upstream(method, path, *, query=None, body=None, config=None):
        captured.update({"method": method, "path": path, "body": body})
        return {
            "code": 10000,
            "data": {
                "file_id": "file_a",
                "object_key": "obj_a",
                "upload_url": "https://upload.example/file_a",
                "expires_in": 3600,
                "upload_directory": "uploads",
            },
        }

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)

    res = client.post(
        "/cloud-drive/upload-url",
        headers=AUTH_HEADERS_A,
        json={
            "file_name": "clip.mp4",
            "file_size": 50,
            "content_type": "video/mp4",
        },
    )

    assert res.status_code == 200, res.get_json()
    assert res.get_json()["data"]["file_id"] == "file_a"
    assert captured["method"] == "POST"
    assert captured["path"] == "/v2/files/upload/presigned-url"

    storage = client.get("/cloud-drive/storage-usage", headers=AUTH_HEADERS_A)
    assert storage.status_code == 200
    assert storage.get_json()["data"]["used_size"] == 50
    assert storage.get_json()["data"]["max_size"] == 100
    assert storage.get_json()["data"]["file_count"] == 1


def test_upload_url_rejects_over_quota_before_upstream(client, monkeypatch):
    called = False

    def fake_upstream(*args, **kwargs):
        nonlocal called
        called = True
        return {}

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)

    res = client.post(
        "/cloud-drive/upload-url",
        headers=AUTH_HEADERS_A,
        json={"file_name": "too-large.mp4", "file_size": 101},
    )

    assert res.status_code == 409
    assert res.get_json()["error"]["code"] == "CLOUD_DRIVE_QUOTA_EXCEEDED"
    assert "部署管理员" in res.get_json()["error"]["message"]
    assert called is False


def test_upload_url_rejects_malformed_file_size_before_upstream(client, monkeypatch):
    called = False

    def fake_upstream(*args, **kwargs):
        nonlocal called
        called = True
        return {}

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    res = client.post(
        "/cloud-drive/upload-url",
        headers=AUTH_HEADERS_A,
        json={"file_name": "bad.mp4", "file_size": "abc"},
    )

    assert res.status_code == 400
    assert res.get_json()["error"]["code"] == "BAD_REQUEST"
    assert res.get_json()["error"]["retryable"] is False
    assert called is False


def test_upload_callback_completes_srt_with_sha256(client, monkeypatch):
    calls = []

    def fake_upstream(method, path, *, query=None, body=None, config=None):
        calls.append((method, path, body))
        if path.endswith("presigned-url"):
            return {
                "code": 10000,
                "data": {
                    "file_id": "srt_1",
                    "object_key": "obj_srt_1",
                    "upload_url": "https://upload.example/srt_1",
                },
            }
        return {"code": 10000, "data": {"ok": True}}

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)

    client.post(
        "/cloud-drive/upload-url",
        headers=AUTH_HEADERS_A,
        json={"file_name": "demo.srt", "file_size": 25},
    )
    sha = "a" * 64
    res = client.post(
        "/cloud-drive/upload-callback",
        headers=AUTH_HEADERS_A,
        json={
            "upload_status": "success",
            "file_size": 25,
            "file_name": "demo.srt",
            "file_id": "srt_1",
            "object_key": "obj_srt_1",
            "sha256": sha,
        },
    )

    assert res.status_code == 200, res.get_json()
    files = client.get("/cloud-drive/files", headers=AUTH_HEADERS_A)
    item = files.get_json()["data"]["items"][0]
    assert item["file_id"] == "srt_1"
    assert item["srt_file_hash"] == sha
    assert calls[-1][1] == "/v2/files/upload/callback"


def test_upload_callback_requires_hash_for_local_srt(client, monkeypatch):
    def fake_upstream(method, path, *, query=None, body=None, config=None):
        if path.endswith("presigned-url"):
            return {
                "code": 10000,
                "data": {
                    "file_id": "srt_no_hash",
                    "object_key": "obj_srt",
                    "upload_url": "https://upload.example/srt",
                },
            }
        raise AssertionError("callback upstream must not be called")

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    client.post(
        "/cloud-drive/upload-url",
        headers=AUTH_HEADERS_A,
        json={"file_name": "demo.srt", "file_size": 25},
    )
    res = client.post(
        "/cloud-drive/upload-callback",
        headers=AUTH_HEADERS_A,
        json={
            "upload_status": "success",
            "file_size": 25,
            "file_name": "demo.srt",
            "file_id": "srt_no_hash",
            "object_key": "obj_srt",
        },
    )

    assert res.status_code == 400
    assert res.get_json()["error"]["code"] == "BAD_REQUEST"


def test_upload_callback_rejects_cross_tenant_before_upstream(client, monkeypatch):
    def fake_upstream(method, path, *, query=None, body=None, config=None):
        if path.endswith("presigned-url"):
            return {
                "code": 10000,
                "data": {
                    "file_id": "tenant_a_file",
                    "object_key": "obj_tenant_a",
                    "upload_url": "https://upload.example/tenant-a",
                },
            }
        raise AssertionError("cross-tenant callback must not hit upstream")

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    client.post(
        "/cloud-drive/upload-url",
        headers=AUTH_HEADERS_A,
        json={"file_name": "clip.mp4", "file_size": 25},
    )
    res = client.post(
        "/cloud-drive/upload-callback",
        headers=AUTH_HEADERS_B,
        json={
            "upload_status": "success",
            "file_id": "tenant_a_file",
            "object_key": "obj_tenant_a",
        },
    )

    assert res.status_code == 404
    assert res.get_json()["error"]["code"] == "NOT_FOUND"


def test_upload_callback_uses_reservation_suffix_for_srt_hash(client, monkeypatch):
    def fake_upstream(method, path, *, query=None, body=None, config=None):
        if path.endswith("presigned-url"):
            return {
                "code": 10000,
                "data": {
                    "file_id": "srt_renamed",
                    "object_key": "obj_srt_renamed",
                    "upload_url": "https://upload.example/srt-renamed",
                },
            }
        raise AssertionError("SRT hash failure must not hit upstream")

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    client.post(
        "/cloud-drive/upload-url",
        headers=AUTH_HEADERS_A,
        json={"file_name": "original.srt", "file_size": 25},
    )
    res = client.post(
        "/cloud-drive/upload-callback",
        headers=AUTH_HEADERS_A,
        json={
            "upload_status": "success",
            "file_name": "renamed.mp4",
            "file_id": "srt_renamed",
            "object_key": "obj_srt_renamed",
        },
    )

    assert res.status_code == 400
    assert res.get_json()["error"]["code"] == "BAD_REQUEST"


def test_upload_callback_keeps_reserved_size_when_client_sends_smaller(
    client, monkeypatch
):
    callback_body = {}

    def fake_upstream(method, path, *, query=None, body=None, config=None):
        if path.endswith("presigned-url"):
            return {
                "code": 10000,
                "data": {
                    "file_id": "size_floor",
                    "object_key": "obj_size_floor",
                    "upload_url": "https://upload.example/size-floor",
                },
            }
        callback_body.update(body)
        return {"code": 10000, "data": {"ok": True}}

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    client.post(
        "/cloud-drive/upload-url",
        headers=AUTH_HEADERS_A,
        json={"file_name": "clip.mp4", "file_size": 80},
    )
    res = client.post(
        "/cloud-drive/upload-callback",
        headers=AUTH_HEADERS_A,
        json={
            "upload_status": "success",
            "file_size": 1,
            "file_name": "wrong.mp4",
            "file_id": "size_floor",
            "object_key": "obj_size_floor",
        },
    )

    assert res.status_code == 200, res.get_json()
    assert callback_body["file_name"] == "clip.mp4"
    assert callback_body["file_size"] == 80
    storage = client.get("/cloud-drive/storage-usage", headers=AUTH_HEADERS_A)
    assert storage.get_json()["data"]["used_size"] == 80


def test_upload_callback_rejects_size_growth_over_quota_before_upstream(
    client, monkeypatch
):
    def fake_upstream(method, path, *, query=None, body=None, config=None):
        if path.endswith("presigned-url"):
            return {
                "code": 10000,
                "data": {
                    "file_id": "size_growth",
                    "object_key": "obj_size_growth",
                    "upload_url": "https://upload.example/size-growth",
                },
            }
        raise AssertionError("over-quota callback must not hit upstream")

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    client.post(
        "/cloud-drive/upload-url",
        headers=AUTH_HEADERS_A,
        json={"file_name": "clip.mp4", "file_size": 80},
    )
    res = client.post(
        "/cloud-drive/upload-callback",
        headers=AUTH_HEADERS_A,
        json={
            "upload_status": "success",
            "file_size": 101,
            "file_id": "size_growth",
            "object_key": "obj_size_growth",
        },
    )

    assert res.status_code == 409
    assert res.get_json()["error"]["code"] == "CLOUD_DRIVE_QUOTA_EXCEEDED"


def test_failed_upload_callback_releases_quota(client, monkeypatch):
    def fake_upstream(method, path, *, query=None, body=None, config=None):
        return {
            "code": 10000,
            "data": {
                "file_id": "failed_upload",
                "object_key": "obj_failed",
                "upload_url": "https://upload.example/failed",
            },
        }

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    client.post(
        "/cloud-drive/upload-url",
        headers=AUTH_HEADERS_A,
        json={"file_name": "bad.mp4", "file_size": 80},
    )
    res = client.post(
        "/cloud-drive/upload-callback",
        headers=AUTH_HEADERS_A,
        json={
            "upload_status": "failed",
            "file_size": 101,
            "file_name": "bad.mp4",
            "file_id": "failed_upload",
            "object_key": "obj_failed",
        },
    )
    assert res.status_code == 200

    storage = client.get("/cloud-drive/storage-usage", headers=AUTH_HEADERS_A)
    assert storage.get_json()["data"]["used_size"] == 0
    assert storage.get_json()["data"]["file_count"] == 0


def test_success_upload_callback_keeps_reservation_on_retryable_upstream_error(
    client, monkeypatch
):
    def fake_upstream(method, path, *, query=None, body=None, config=None):
        if path.endswith("presigned-url"):
            return {
                "code": 10000,
                "data": {
                    "file_id": "callback_retry",
                    "object_key": "obj_callback_retry",
                    "upload_url": "https://upload.example/retry",
                },
            }
        raise server.UpstreamCloudDriveError(
            504,
            "UPSTREAM_TIMEOUT",
            "Internal cloud-drive did not respond in time.",
            retryable=True,
        )

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    client.post(
        "/cloud-drive/upload-url",
        headers=AUTH_HEADERS_A,
        json={"file_name": "retry.mp4", "file_size": 40},
    )
    res = client.post(
        "/cloud-drive/upload-callback",
        headers=AUTH_HEADERS_A,
        json={
            "upload_status": "success",
            "file_size": 40,
            "file_name": "retry.mp4",
            "file_id": "callback_retry",
            "object_key": "obj_callback_retry",
        },
    )

    assert res.status_code == 504
    assert res.get_json()["error"]["retryable"] is True
    storage = client.get("/cloud-drive/storage-usage", headers=AUTH_HEADERS_A)
    assert storage.get_json()["data"]["used_size"] == 40
    assert storage.get_json()["data"]["file_count"] == 1


def test_transfer_rejects_over_quota_before_upstream(client, monkeypatch):
    called = False

    def fake_upstream(*args, **kwargs):
        nonlocal called
        called = True
        return {}

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    res = client.post(
        "/cloud-drive/transfer",
        headers=AUTH_HEADERS_A,
        json={"link": "https://example.com/video.mp4", "file_size": 101},
    )

    assert res.status_code == 409
    assert res.get_json()["error"]["code"] == "CLOUD_DRIVE_QUOTA_EXCEEDED"
    assert called is False


def test_transfer_create_reserves_quota_and_lists_owned_record(client, monkeypatch):
    # regression coverage: upstream POST now returns `upload_id` (not `file_id`); the
    # parent reservation stores it under `upload_id`, leaving `file_id`
    # NULL until the refresh fan-out materializes per-file child rows
    # from `/v2/files/user/filelist?upload_id=...`.
    calls = []

    def fake_upstream(method, path, *, query=None, body=None, config=None):
        calls.append((method, path, query, body))
        if method == "POST":
            return {
                "code": 10000,
                "data": {
                    "upload_id": "baidu-transfer_a",
                    "file_name": "transfer.mp4",
                    "file_size": 40,
                    "link_type": "baidu",
                },
            }
        assert path == "/v2/files/user/filelist"
        assert query["upload_id"] == "baidu-transfer_a"
        return {"code": 10000, "data": {"data": []}}

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    res = client.post(
        "/cloud-drive/transfer",
        headers=AUTH_HEADERS_A,
        json={"link": "https://example.com/video.mp4", "file_size": 40},
    )

    assert res.status_code == 200, res.get_json()
    assert calls[0][:2] == ("POST", "/v2/files/upload")
    assert calls[0][3] == {"link": "https://example.com/video.mp4"}
    storage = client.get("/cloud-drive/storage-usage", headers=AUTH_HEADERS_A)
    assert storage.get_json()["data"]["used_size"] == 40
    assert storage.get_json()["data"]["file_count"] == 1

    transfers = client.get("/cloud-drive/transfer", headers=AUTH_HEADERS_A)
    item = transfers.get_json()["data"]["data"][0]
    # Pre-fan-out parent: identifier surfaces as reservation_id, upstream
    # file_id is still NULL (lives on the future children).
    assert item["file_id"].startswith("cdr_")
    assert item["upstream_file_id"] is None
    assert item["file_name"] == "transfer.mp4"
    assert item["file_size"] == "40"


def test_transfer_create_accepts_link_only_and_accounts_upstream_size(
    client, monkeypatch
):
    calls = []

    def fake_upstream(method, path, *, query=None, body=None, config=None):
        calls.append((method, path, query, body))
        if method == "POST":
            return {
                "code": 10000,
                "data": {
                    "upload_id": "baidu-link_only",
                    "file_name": "link-only.mp4",
                    "file_size": 40,
                    "link_type": "baidu",
                },
            }
        # Refresh GET — no children yet (still in flight); skip fan-out.
        return {"code": 10000, "data": {"data": []}}

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    monkeypatch.setattr(server, "_cloud_drive_transfer_upstream_config", lambda: None)

    def run_submission(**kwargs):
        slot = kwargs.pop("slot", None)
        try:
            server._run_cloud_drive_transfer_submission(**kwargs)
        finally:
            if slot is not None:
                slot.release()

    monkeypatch.setattr(
        server,
        "_enqueue_cloud_drive_transfer_submission",
        run_submission,
    )
    res = client.post(
        "/cloud-drive/transfer",
        headers=AUTH_HEADERS_A,
        json={"link": "https://example.com/link-only.mp4"},
    )

    assert res.status_code == 200, res.get_json()
    assert calls[0][:2] == ("POST", "/v2/files/upload")
    assert calls[0][3] == {"link": "https://example.com/link-only.mp4"}
    storage = client.get("/cloud-drive/storage-usage", headers=AUTH_HEADERS_A)
    assert storage.get_json()["data"]["used_size"] == 40
    transfers = client.get("/cloud-drive/transfer", headers=AUTH_HEADERS_A)
    item = transfers.get_json()["data"]["data"][0]
    # Parent reservation reflects upstream-reported aggregate size; no
    # `file_id` until fan-out lands child rows on a later refresh.
    assert item["upstream_file_id"] is None
    assert item["file_size"] == "40"


def test_transfer_link_only_returns_pending_before_upstream_finishes(
    client, monkeypatch
):
    # regression coverage: validates the full async link-transfer lifecycle —
    #   1. POST returns immediately with placeholder reservation_id
    #   2. Worker stores upload_id; parent still file_id-less
    #   3. GET drives the refresh fan-out: filelist?upload_id=... now
    #      returns proper child file_id rows once upstream finishes,
    #      and we materialize them locally.
    submissions = []
    calls = []
    upstream_phase = {"value": "in_flight"}

    def fake_upstream(method, path, *, query=None, body=None, config=None):
        calls.append((method, path, query, body))
        if method == "POST":
            return {
                "code": 10000,
                "data": {
                    "upload_id": "baidu-transfer_async",
                    "file_name": "async.mp4",
                    "file_size": 40,
                    "link_type": "baidu",
                },
            }
        assert path == "/v2/files/user/filelist"
        if upstream_phase["value"] == "in_flight":
            return {"code": 10000, "data": {"data": []}}
        return {
            "code": 10000,
            "data": {
                "data": [
                    {
                        "file_id": "async_child_xy0001",
                        "file_name": "async.mp4",
                        "file_size": 40,
                        "status": 2,
                        "progress": 100,
                    }
                ]
            },
        }

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    monkeypatch.setattr(server, "_cloud_drive_transfer_upstream_config", lambda: None)

    def capture_submission(**kwargs):
        slot = kwargs.pop("slot", None)
        if slot is not None:
            slot.release()
        submissions.append(kwargs)

    monkeypatch.setattr(
        server,
        "_enqueue_cloud_drive_transfer_submission",
        capture_submission,
    )

    res = client.post(
        "/cloud-drive/transfer",
        headers=AUTH_HEADERS_A,
        json={"link": "https://example.com/async.mp4"},
    )

    assert res.status_code == 200, res.get_json()
    data = res.get_json()["data"]
    assert data["file_id"] == data["reservation_id"]
    assert calls == []
    assert submissions == [
        {
            "app_key": KEY_A,
            "reservation_id": data["reservation_id"],
            "link": "https://example.com/async.mp4",
        }
    ]

    pending = client.get("/cloud-drive/transfer", headers=AUTH_HEADERS_A)
    item = pending.get_json()["data"]["data"][0]
    assert item["file_id"] == data["reservation_id"]
    assert item["status"] == 0
    assert item["file_size"] == "0"

    server._run_cloud_drive_transfer_submission(**submissions[0])

    # Worker ran; parent now carries upload_id, but upstream filelist
    # still empty → no children, parent still visible as placeholder.
    transfers = client.get("/cloud-drive/transfer", headers=AUTH_HEADERS_A)
    item = transfers.get_json()["data"]["data"][0]
    assert item["upstream_file_id"] is None
    assert item["file_size"] == "40"

    # Upstream finishes the underlying upload: filelist?upload_id=…
    # returns a child file. Next GET fans it out + settles the parent.
    upstream_phase["value"] = "done"
    transfers = client.get("/cloud-drive/transfer", headers=AUTH_HEADERS_A)
    items = transfers.get_json()["data"]["data"]
    assert len(items) == 1
    child = items[0]
    assert child["file_id"] == "async_child_xy0001"
    assert child["status"] == 2
    assert child["file_size"] == "40"

    files = client.get("/cloud-drive/files", headers=AUTH_HEADERS_A)
    assert files.get_json()["data"]["items"][0]["file_id"] == "async_child_xy0001"


def test_transfer_link_only_rejects_when_worker_queue_full(client, monkeypatch):
    class FullSlots:
        def acquire(self, blocking=True):
            assert blocking is False
            return False

    class UnusedExecutor:
        def submit(self, _callback):
            raise AssertionError("worker should not be submitted")

    def fail_upstream(*args, **kwargs):
        raise AssertionError("upstream should not be called")

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fail_upstream)
    monkeypatch.setattr(
        server,
        "_cloud_drive_transfer_executor_state",
        lambda: (UnusedExecutor(), FullSlots()),
    )

    res = client.post(
        "/cloud-drive/transfer",
        headers=AUTH_HEADERS_A,
        json={"link": "https://example.com/queue-full.mp4"},
    )

    assert res.status_code == 429, res.get_json()
    error = res.get_json()["error"]
    assert error["code"] == "TRANSFER_QUEUE_FULL"
    assert error["retryable"] is True

    transfers = client.get("/cloud-drive/transfer", headers=AUTH_HEADERS_A)
    assert transfers.get_json()["data"]["total"] == 0


def test_transfer_link_only_over_quota_after_upstream_cleans_upstream(
    client, monkeypatch
):
    # regression coverage: when upstream reports a size that pushes the user over quota,
    # the worker marks the reservation failed. No upstream DELETE is
    # attempted here — we only hold an upload_id (not a file_id), and
    # upstream's delete endpoint is per-file. Orphan handling at the
    # upstream side is out of scope for this PR.
    calls = []

    def fake_upstream(method, path, *, query=None, body=None, config=None):
        calls.append((method, path, body))
        if method == "POST":
            return {
                "code": 10000,
                "data": {
                    "upload_id": "baidu-too_large_after_meta",
                    "file_name": "too-large.mp4",
                    "file_size": 101,
                    "link_type": "baidu",
                },
            }
        # Refresh GET should be skipped — failed reservation is no longer
        # an unsettled link parent.
        return {"code": 10000, "data": {"data": []}}

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    monkeypatch.setattr(server, "_cloud_drive_transfer_upstream_config", lambda: None)

    def run_submission(**kwargs):
        slot = kwargs.pop("slot", None)
        try:
            server._run_cloud_drive_transfer_submission(**kwargs)
        finally:
            if slot is not None:
                slot.release()

    monkeypatch.setattr(
        server,
        "_enqueue_cloud_drive_transfer_submission",
        run_submission,
    )
    res = client.post(
        "/cloud-drive/transfer",
        headers=AUTH_HEADERS_A,
        json={"link": "https://example.com/too-large.mp4"},
    )

    assert res.status_code == 200, res.get_json()
    # No DELETE — we don't have a file_id to call /v2/files/user/files/<id> on.
    assert not any(call[0] == "DELETE" for call in calls)
    storage = client.get("/cloud-drive/storage-usage", headers=AUTH_HEADERS_A)
    assert storage.get_json()["data"]["used_size"] == 0
    assert storage.get_json()["data"]["file_count"] == 0
    transfers = client.get("/cloud-drive/transfer", headers=AUTH_HEADERS_A)
    item = transfers.get_json()["data"]["data"][0]
    assert item["status"] == 3
    running_transfers = client.get(
        "/cloud-drive/transfer?status=1", headers=AUTH_HEADERS_A
    )
    assert running_transfers.get_json()["data"]["total"] == 0
    failed_transfers = client.get(
        "/cloud-drive/transfer?status=3", headers=AUTH_HEADERS_A
    )
    assert failed_transfers.get_json()["data"]["total"] == 1


def test_transfer_list_refreshes_upstream_status_and_files_list(client, monkeypatch):
    calls = []

    def fake_upstream(method, path, *, query=None, body=None, config=None):
        calls.append((method, path, query, body))
        if method == "POST":
            return {
                "code": 10000,
                "data": {
                    "file_id": "transfer_refresh",
                    "file_name": "refresh.mp4",
                    "file_size": 40,
                    "status": 1,
                    "progress": 20,
                },
            }
        assert path == "/v2/files/user/filelist"
        return {
            "code": 10000,
            "data": {
                "data": [
                    {
                        "file_id": "transfer_refresh",
                        "file_name": "refresh.mp4",
                        "file_size": 40,
                        "status": 2,
                        "progress": 100,
                    }
                ]
            },
        }

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    res = client.post(
        "/cloud-drive/transfer",
        headers=AUTH_HEADERS_A,
        json={"link": "https://example.com/refresh.mp4", "file_size": 40},
    )
    assert res.status_code == 200, res.get_json()

    transfers = client.get("/cloud-drive/transfer", headers=AUTH_HEADERS_A)
    item = transfers.get_json()["data"]["data"][0]
    assert item["file_id"] == "transfer_refresh"
    assert item["status"] == 2
    assert item["progress"] == 100
    assert item["completed_time"]
    assert any(
        call[0] == "GET" and call[1] == "/v2/files/user/filelist" for call in calls
    )

    files = client.get("/cloud-drive/files", headers=AUTH_HEADERS_A)
    assert files.get_json()["data"]["items"][0]["file_id"] == "transfer_refresh"


def test_transfer_list_returns_stale_records_when_refresh_upstream_fails(
    client, monkeypatch
):
    # regression coverage: when the refresh fan-out can't reach upstream, the unsettled
    # parent stays visible (no children materialized, no settle). The
    # endpoint must still serve a 200 with the local record.
    import server

    def fake_upstream(method, path, *, query=None, body=None, config=None):
        if method == "POST":
            return {
                "code": 10000,
                "data": {
                    "upload_id": "baidu-stale",
                    "file_name": "stale.mp4",
                    "file_size": 40,
                    "link_type": "baidu",
                },
            }
        raise server.UpstreamCloudDriveError(
            504,
            "UPSTREAM_TIMEOUT",
            "Internal cloud-drive did not respond in time.",
            retryable=True,
        )

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    res = client.post(
        "/cloud-drive/transfer",
        headers=AUTH_HEADERS_A,
        json={"link": "https://example.com/stale.mp4", "file_size": 40},
    )
    assert res.status_code == 200, res.get_json()

    transfers = client.get("/cloud-drive/transfer", headers=AUTH_HEADERS_A)
    assert transfers.status_code == 200, transfers.get_json()
    item = transfers.get_json()["data"]["data"][0]
    # Pre-fan-out parent surfaces reservation_id as task_id; upstream
    # file_id remains NULL because no children have settled yet.
    assert item["upstream_file_id"] is None
    assert item["file_size"] == "40"


def test_transfer_create_rejects_missing_upstream_file_id(client, monkeypatch):
    def fake_upstream(method, path, *, query=None, body=None, config=None):
        return {
            "code": 10000,
            "data": {
                "file_name": "missing-id.mp4",
                "file_size": 40,
                "status": 1,
                "progress": 20,
            },
        }

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    res = client.post(
        "/cloud-drive/transfer",
        headers=AUTH_HEADERS_A,
        json={"link": "https://example.com/missing-id.mp4", "file_size": 40},
    )

    assert res.status_code == 502
    assert res.get_json()["error"]["code"] == "UPSTREAM_SCHEMA_ERROR"
    storage = client.get("/cloud-drive/storage-usage", headers=AUTH_HEADERS_A)
    assert storage.get_json()["data"]["used_size"] == 0
    transfers = client.get("/cloud-drive/transfer", headers=AUTH_HEADERS_A)
    assert transfers.get_json()["data"]["total"] == 1
    assert transfers.get_json()["data"]["data"][0]["status"] == 3


def test_transfer_completed_status_persists(client, monkeypatch):
    def fake_upstream(method, path, *, query=None, body=None, config=None):
        return {
            "code": 10000,
            "data": {
                "file_id": "transfer_done",
                "file_name": "done.mp4",
                "file_size": 40,
                "status": 2,
                "progress": 100,
            },
        }

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    res = client.post(
        "/cloud-drive/transfer",
        headers=AUTH_HEADERS_A,
        json={"link": "https://example.com/done.mp4", "file_size": 40},
    )

    assert res.status_code == 200, res.get_json()
    transfers = client.get("/cloud-drive/transfer", headers=AUTH_HEADERS_A)
    item = transfers.get_json()["data"]["data"][0]
    assert item["file_id"] == "transfer_done"
    assert item["status"] == 2
    assert item["progress"] == 100
    assert item["completed_time"]
    files = client.get("/cloud-drive/files", headers=AUTH_HEADERS_A)
    file_item = files.get_json()["data"]["items"][0]
    assert file_item["file_id"] == "transfer_done"
    assert file_item["file_name"] == "done.mp4"


def test_transfer_upstream_completed_status_sets_local_completed(client, monkeypatch):
    def fake_upstream(method, path, *, query=None, body=None, config=None):
        return {
            "code": 10000,
            "data": {
                "file_id": "transfer_status_done",
                "file_name": "status-done.mp4",
                "file_size": 40,
                "status": 2,
                "progress": 0,
            },
        }

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    res = client.post(
        "/cloud-drive/transfer",
        headers=AUTH_HEADERS_A,
        json={"link": "https://example.com/status-done.mp4", "file_size": 40},
    )

    assert res.status_code == 200, res.get_json()
    transfers = client.get("/cloud-drive/transfer", headers=AUTH_HEADERS_A)
    item = transfers.get_json()["data"]["data"][0]
    assert item["status"] == 2
    assert item["progress"] == 100
    assert item["completed_time"]
    files = client.get("/cloud-drive/files", headers=AUTH_HEADERS_A)
    assert files.get_json()["data"]["items"][0]["file_id"] == "transfer_status_done"


def test_transfer_create_generic_failure_releases_quota(client, monkeypatch):
    # regression coverage: when the local DB write fails after upstream upload, the
    # reservation is marked failed (releasing quota). No upstream
    # DELETE is attempted — we only hold upload_id, not file_id.
    calls = []

    def fake_upstream(method, path, *, query=None, body=None, config=None):
        calls.append((method, path, body))
        if method == "POST":
            return {
                "code": 10000,
                "data": {
                    "upload_id": "baidu-orphan",
                    "file_name": "orphan.mp4",
                    "file_size": 40,
                    "link_type": "baidu",
                },
            }
        return {"code": 10000, "data": {"data": []}}

    def broken_attach(*args, **kwargs):
        raise RuntimeError("local update failed")

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    monkeypatch.setattr(server, "attach_link_upload_id", broken_attach)
    res = client.post(
        "/cloud-drive/transfer",
        headers=AUTH_HEADERS_A,
        json={"link": "https://example.com/orphan.mp4", "file_size": 40},
    )

    assert res.status_code == 503
    assert not any(call[0] == "DELETE" for call in calls)
    storage = client.get("/cloud-drive/storage-usage", headers=AUTH_HEADERS_A)
    assert storage.get_json()["data"]["used_size"] == 0
    assert storage.get_json()["data"]["file_count"] == 0


# ──────────────────────────── regression coverage fan-out tests ─────────────────────────────


def test_transfer_link_fanout_materializes_multiple_children(client, monkeypatch):
    # A baidu folder share resolves into N file_ids upstream; once all
    # are terminal (status=2), one refresh settles the parent and
    # inserts one row per child file_id.
    calls = []

    def fake_upstream(method, path, *, query=None, body=None, config=None):
        calls.append((method, path, query))
        if method == "POST":
            return {
                "code": 10000,
                "data": {
                    "upload_id": "baidu-folder_share",
                    "file_name": "ep01.srt, ep02.srt, ep03.srt",
                    "file_size": 30,
                    "link_type": "baidu",
                },
            }
        assert path == "/v2/files/user/filelist"
        return {
            "code": 10000,
            "data": {
                "data": [
                    {
                        "file_id": f"baokuan_child_{i}",
                        "file_name": f"ep0{i}.srt",
                        "file_size": 10,
                        "status": 2,
                        "progress": 100,
                    }
                    for i in range(1, 4)
                ]
            },
        }

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    res = client.post(
        "/cloud-drive/transfer",
        headers=AUTH_HEADERS_A,
        json={"link": "https://pan.baidu.com/s/folder-share", "file_size": 30},
    )
    assert res.status_code == 200

    transfers = client.get("/cloud-drive/transfer", headers=AUTH_HEADERS_A)
    items = transfers.get_json()["data"]["data"]
    # Parent is hidden after settlement; only the 3 children appear.
    assert {item["file_id"] for item in items} == {
        "baokuan_child_1",
        "baokuan_child_2",
        "baokuan_child_3",
    }
    assert all(item["status"] == 2 for item in items)
    assert all(item["file_size"] == "10" for item in items)

    # Each child file is also browsable via /cloud-drive/files.
    files = client.get("/cloud-drive/files", headers=AUTH_HEADERS_A)
    assert {f["file_id"] for f in files.get_json()["data"]["items"]} == {
        "baokuan_child_1",
        "baokuan_child_2",
        "baokuan_child_3",
    }
    # Quota accounting: parent's pre-fan-out 30 is replaced by 3×10 = 30.
    storage = client.get("/cloud-drive/storage-usage", headers=AUTH_HEADERS_A)
    assert storage.get_json()["data"]["used_size"] == 30
    assert storage.get_json()["data"]["file_count"] == 3


def test_transfer_link_fanout_skips_unsettled_parents(client, monkeypatch):
    # Some children still in `uploading` (status=1) → fan-out should
    # leave the parent alone for next refresh.
    def fake_upstream(method, path, *, query=None, body=None, config=None):
        if method == "POST":
            return {
                "code": 10000,
                "data": {
                    "upload_id": "baidu-partial",
                    "file_name": "two.srt, one.srt",
                    "file_size": 20,
                    "link_type": "baidu",
                },
            }
        return {
            "code": 10000,
            "data": {
                "data": [
                    {
                        "file_id": "done_child",
                        "file_name": "two.srt",
                        "file_size": 10,
                        "status": 2,
                        "progress": 100,
                    },
                    {
                        "file_id": "running_child",
                        "file_name": "one.srt",
                        "file_size": 10,
                        "status": 1,
                        "progress": 60,
                    },
                ]
            },
        }

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    client.post(
        "/cloud-drive/transfer",
        headers=AUTH_HEADERS_A,
        json={"link": "https://pan.baidu.com/s/partial", "file_size": 20},
    )

    transfers = client.get("/cloud-drive/transfer", headers=AUTH_HEADERS_A)
    items = transfers.get_json()["data"]["data"]
    # Parent still visible, no children materialized.
    assert len(items) == 1
    assert items[0]["upstream_file_id"] is None
    assert items[0]["file_size"] == "20"

    # No child appears in /cloud-drive/files either.
    files = client.get("/cloud-drive/files", headers=AUTH_HEADERS_A)
    assert files.get_json()["data"]["items"] == []


def test_transfer_link_fanout_mixed_success_and_failure(client, monkeypatch):
    # Settlement still proceeds when some children failed (status=3) or
    # were upstream-deleted (status=4). Success rows become visible
    # files; failed rows surface in /cloud-drive/transfer; deleted ones
    # are skipped entirely.
    def fake_upstream(method, path, *, query=None, body=None, config=None):
        if method == "POST":
            return {
                "code": 10000,
                "data": {
                    "upload_id": "baidu-mixed",
                    "file_name": "ok.srt, bad.srt, gone.srt",
                    "file_size": 30,
                    "link_type": "baidu",
                },
            }
        return {
            "code": 10000,
            "data": {
                "data": [
                    {
                        "file_id": "child_ok",
                        "file_name": "ok.srt",
                        "file_size": 10,
                        "status": 2,
                        "progress": 100,
                    },
                    {
                        "file_id": "child_failed",
                        "file_name": "bad.srt",
                        "file_size": 0,
                        "status": 3,
                        "progress": 0,
                        "error_message": "ingest failed",
                    },
                    {
                        "file_id": "child_deleted",
                        "file_name": "gone.srt",
                        "file_size": 0,
                        "status": 4,
                        "progress": 0,
                    },
                ]
            },
        }

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    client.post(
        "/cloud-drive/transfer",
        headers=AUTH_HEADERS_A,
        json={"link": "https://pan.baidu.com/s/mixed", "file_size": 30},
    )

    transfers = client.get("/cloud-drive/transfer", headers=AUTH_HEADERS_A)
    transfer_items = transfers.get_json()["data"]["data"]
    by_id = {item["file_id"]: item for item in transfer_items}
    # Deleted child is not materialized; success + failed both land.
    assert set(by_id) == {"child_ok", "child_failed"}
    assert by_id["child_ok"]["status"] == 2
    assert by_id["child_failed"]["status"] == 3

    files = client.get("/cloud-drive/files", headers=AUTH_HEADERS_A)
    # Only the successful child is browsable as a usable file.
    assert [f["file_id"] for f in files.get_json()["data"]["items"]] == ["child_ok"]


def test_transfer_link_fanout_is_idempotent_after_settlement(
    client, sqlite_engine, monkeypatch
):
    # After settlement, subsequent GETs must not re-call upstream
    # filelist for the same upload_id — the partial index drops the
    # parent from `list_unsettled_link_parents` and `settled_at` is
    # already set.
    upstream_calls = []

    def fake_upstream(method, path, *, query=None, body=None, config=None):
        upstream_calls.append((method, path, query))
        if method == "POST":
            return {
                "code": 10000,
                "data": {
                    "upload_id": "baidu-once",
                    "file_name": "once.srt",
                    "file_size": 10,
                    "link_type": "baidu",
                },
            }
        return {
            "code": 10000,
            "data": {
                "data": [
                    {
                        "file_id": "once_child",
                        "file_name": "once.srt",
                        "file_size": 10,
                        "status": 2,
                        "progress": 100,
                    }
                ]
            },
        }

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    client.post(
        "/cloud-drive/transfer",
        headers=AUTH_HEADERS_A,
        json={"link": "https://pan.baidu.com/s/once", "file_size": 10},
    )
    # First GET drives fan-out + settlement.
    client.get("/cloud-drive/transfer", headers=AUTH_HEADERS_A)
    first_get_call_count = sum(1 for c in upstream_calls if c[0] == "GET")
    assert first_get_call_count == 1

    # Second + third GETs must not hit upstream — parent is settled.
    client.get("/cloud-drive/transfer", headers=AUTH_HEADERS_A)
    client.get("/cloud-drive/transfer", headers=AUTH_HEADERS_A)
    assert sum(1 for c in upstream_calls if c[0] == "GET") == first_get_call_count


def test_transfer_link_fanout_dedups_names_per_user(
    client, sqlite_engine, monkeypatch
):
    # Upstream dedups in one master namespace. If user A already has
    # episode.mp4, user B may receive episode(1).mp4 from upstream even
    # though user B's private cloud is empty. Fan-out must strip that
    # upstream suffix and recompute against only user B's files.
    with sqlite_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO user_cloud_files (
                    reservation_id, user_id, app_key, file_id, object_key,
                    file_name, suffix, category, file_size, source, status,
                    progress, upstream_payload
                )
                VALUES (
                    'existing_user_a_episode', 1, :app_key, 'existing_episode_a', 'obj',
                    'episode.mp4', 'mp4', 1, 10, 'local_upload', 'completed',
                    100, '{}'
                )
                """
            ),
            {"app_key": KEY_A},
        )

    def fake_upstream(method, path, *, query=None, body=None, config=None):
        if method == "POST":
            return {
                "code": 10000,
                "data": {
                    "upload_id": "baidu-user-b",
                    "file_name": "episode(1).mp4",
                    "file_size": 10,
                    "link_type": "baidu",
                },
            }
        assert query["upload_id"] == "baidu-user-b"
        return {
            "code": 10000,
            "data": {
                "data": [
                    {
                        "file_id": "episode_user_b",
                        "file_name": "episode(1).mp4",
                        "file_size": 10,
                        "status": 2,
                        "progress": 100,
                    }
                ]
            },
        }

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    res = client.post(
        "/cloud-drive/transfer",
        headers=AUTH_HEADERS_B,
        json={"link": "https://pan.baidu.com/s/user-b", "file_size": 10},
    )
    assert res.status_code == 200

    transfers = client.get("/cloud-drive/transfer", headers=AUTH_HEADERS_B)
    transfer_items = transfers.get_json()["data"]["data"]
    assert [item["file_name"] for item in transfer_items] == ["episode.mp4"]

    files = client.get("/cloud-drive/files", headers=AUTH_HEADERS_B)
    file_items = files.get_json()["data"]["items"]
    assert [item["file_name"] for item in file_items] == ["episode.mp4"]

    user_a_files = client.get("/cloud-drive/files", headers=AUTH_HEADERS_A)
    assert [item["file_name"] for item in user_a_files.get_json()["data"]["items"]] == [
        "episode.mp4"
    ]


def test_transfer_link_fanout_dedups_names_within_same_batch(
    client, monkeypatch
):
    # Two upstream children can strip back to the same base name in one
    # fan-out transaction. The second child must see the first assigned
    # name even before that insert is visible to a fresh SELECT.
    def fake_upstream(method, path, *, query=None, body=None, config=None):
        if method == "POST":
            return {
                "code": 10000,
                "data": {
                    "upload_id": "baidu-same-batch",
                    "file_name": "clip(1).mp4, clip(2).mp4",
                    "file_size": 20,
                    "link_type": "baidu",
                },
            }
        assert query["upload_id"] == "baidu-same-batch"
        return {
            "code": 10000,
            "data": {
                "data": [
                    {
                        "file_id": "clip_batch_1",
                        "file_name": "clip(1).mp4",
                        "file_size": 10,
                        "status": 2,
                        "progress": 100,
                    },
                    {
                        "file_id": "clip_batch_2",
                        "file_name": "clip(2).mp4",
                        "file_size": 10,
                        "status": 2,
                        "progress": 100,
                    },
                ]
            },
        }

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    res = client.post(
        "/cloud-drive/transfer",
        headers=AUTH_HEADERS_A,
        json={"link": "https://pan.baidu.com/s/same-batch", "file_size": 20},
    )
    assert res.status_code == 200

    transfers = client.get("/cloud-drive/transfer", headers=AUTH_HEADERS_A)
    names_by_id = {
        item["file_id"]: item["file_name"]
        for item in transfers.get_json()["data"]["data"]
    }
    assert names_by_id == {
        "clip_batch_1": "clip.mp4",
        "clip_batch_2": "clip(1).mp4",
    }


def test_transfer_orphan_legacy_baidu_file_id_is_fanned_out(
    client, sqlite_engine, monkeypatch
):
    # The previous worker stored upload_ids into the `file_id` column.
    # The 20260604_0002 migration moves them into `upload_id` and the
    # next GET picks them up via the new fan-out path. This test seeds
    # such a legacy row directly and verifies the recovery.
    with sqlite_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO user_cloud_files (
                    reservation_id, user_id, app_key, upload_id,
                    file_name, suffix, category, file_size, source, status,
                    progress, upstream_payload
                ) VALUES (
                    'cdr_legacy_orphan', 1, :app_key, 'baidu-legacy_orphan',
                    'legacy.mp4', 'mp4', 1, 0, 'transfer', 'transfer_pending',
                    0, '{}'
                )
                """
            ),
            {"app_key": KEY_A},
        )

    def fake_upstream(method, path, *, query=None, body=None, config=None):
        assert method == "GET" and path == "/v2/files/user/filelist"
        assert query["upload_id"] == "baidu-legacy_orphan"
        return {
            "code": 10000,
            "data": {
                "data": [
                    {
                        "file_id": "legacy_recovered",
                        "file_name": "legacy.mp4",
                        "file_size": 40,
                        "status": 2,
                        "progress": 100,
                    }
                ]
            },
        }

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    transfers = client.get("/cloud-drive/transfer", headers=AUTH_HEADERS_A)
    items = transfers.get_json()["data"]["data"]
    assert [item["file_id"] for item in items] == ["legacy_recovered"]
    assert items[0]["status"] == 2


def test_cross_tenant_delete_returns_404_without_upstream(
    client, sqlite_engine, monkeypatch
):
    with sqlite_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO user_cloud_files (
                    reservation_id, user_id, app_key, file_id, object_key,
                    file_name, suffix, category, file_size, source, status,
                    progress, upstream_payload
                )
                VALUES (
                    'res_a', 1, :app_key, 'owned_by_a', 'obj',
                    'clip.mp4', 'mp4', 1, 50, 'local_upload', 'completed',
                    100, '{}'
                )
                """
            ),
            {"app_key": KEY_A},
        )

    def fake_upstream(*args, **kwargs):
        raise AssertionError("cross-tenant delete must not hit upstream")

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    res = client.delete("/cloud-drive/files/owned_by_a", headers=AUTH_HEADERS_B)

    assert res.status_code == 404
    assert res.get_json()["error"]["code"] == "NOT_FOUND"


def test_cross_tenant_download_returns_404_without_upstream(
    client, sqlite_engine, monkeypatch
):
    with sqlite_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO user_cloud_files (
                    reservation_id, user_id, app_key, file_id, object_key,
                    file_name, suffix, category, file_size, source, status,
                    progress, upstream_payload
                )
                VALUES (
                    'download_owned_by_a', 1, :app_key, 'download_owned_by_a', 'obj',
                    'clip.mp4', 'mp4', 1, 50, 'local_upload', 'completed',
                    100, '{}'
                )
                """
            ),
            {"app_key": KEY_A},
        )

    def fake_upstream(*args, **kwargs):
        raise AssertionError("cross-tenant download must not hit upstream")

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    res = client.post(
        "/cloud-drive/download-url",
        headers=AUTH_HEADERS_B,
        json={"file_id": "download_owned_by_a"},
    )

    assert res.status_code == 404
    assert res.get_json()["error"]["code"] == "NOT_FOUND"


def test_cross_tenant_list_does_not_show_other_user_files(client, sqlite_engine):
    with sqlite_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO user_cloud_files (
                    reservation_id, user_id, app_key, file_id, object_key,
                    file_name, suffix, category, file_size, source, status,
                    progress, upstream_payload
                )
                VALUES (
                    'list_owned_by_a', 1, :app_key, 'list_owned_by_a', 'obj',
                    'clip.mp4', 'mp4', 1, 50, 'local_upload', 'completed',
                    100, '{}'
                )
                """
            ),
            {"app_key": KEY_A},
        )

    res = client.get("/cloud-drive/files", headers=AUTH_HEADERS_B)

    assert res.status_code == 200
    assert res.get_json()["data"]["total"] == 0
    assert res.get_json()["data"]["items"] == []


def test_download_url_rejects_reserved_file_before_upstream(
    client, sqlite_engine, monkeypatch
):
    with sqlite_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO user_cloud_files (
                    reservation_id, user_id, app_key, file_id, object_key,
                    file_name, suffix, category, file_size, source, status,
                    progress, upstream_payload
                )
                VALUES (
                    'reserved_download', 1, :app_key, 'reserved_download_file', 'obj',
                    'clip.mp4', 'mp4', 1, 50, 'local_upload', 'reserved',
                    0, '{}'
                )
                """
            ),
            {"app_key": KEY_A},
        )

    def fake_upstream(*args, **kwargs):
        raise AssertionError("reserved file download must not hit upstream")

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    res = client.post(
        "/cloud-drive/download-url",
        headers=AUTH_HEADERS_A,
        json={"file_id": "reserved_download_file"},
    )

    assert res.status_code == 404
    assert res.get_json()["error"]["code"] == "NOT_FOUND"


def test_single_delete_keeps_quota_until_upstream_confirms(
    client, sqlite_engine, monkeypatch
):
    with sqlite_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO user_cloud_files (
                    reservation_id, user_id, app_key, file_id, object_key,
                    file_name, suffix, category, file_size, source, status,
                    progress, upstream_payload
                )
                VALUES (
                    'delete_order', 1, :app_key, 'delete_order_file', 'obj',
                    'clip.mp4', 'mp4', 1, 50, 'local_upload', 'completed',
                    100, '{}'
                )
                """
            ),
            {"app_key": KEY_A},
        )

    def fake_upstream(method, path, *, query=None, body=None, config=None):
        with sqlite_engine.connect() as conn:
            status = conn.execute(
                text("SELECT status FROM user_cloud_files WHERE file_id = :file_id"),
                {"file_id": "delete_order_file"},
            ).scalar_one()
            used = conn.execute(
                text(
                    "SELECT COALESCE(SUM(file_size), 0) FROM user_cloud_files "
                    "WHERE user_id = 1 AND status IN ("
                    "'reserved', 'completed', 'delete_pending', "
                    "'transfer_pending', 'transfer_running', 'transfer_completed')"
                )
            ).scalar_one()
        assert status == "delete_pending"
        assert int(used) == 50
        return {"code": 10000, "data": {"deleted": True}}

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    res = client.delete("/cloud-drive/files/delete_order_file", headers=AUTH_HEADERS_A)

    assert res.status_code == 200, res.get_json()
    storage = client.get("/cloud-drive/storage-usage", headers=AUTH_HEADERS_A)
    assert storage.get_json()["data"]["used_size"] == 0


def test_single_delete_pending_can_retry_after_finalize_failure(
    client, sqlite_engine, monkeypatch
):
    with sqlite_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO user_cloud_files (
                    reservation_id, user_id, app_key, file_id, object_key,
                    file_name, suffix, category, file_size, source, status,
                    progress, upstream_payload
                )
                VALUES (
                    'delete_retry', 1, :app_key, 'delete_retry_file', 'obj',
                    'clip.mp4', 'mp4', 1, 50, 'local_upload', 'completed',
                    100, '{}'
                )
                """
            ),
            {"app_key": KEY_A},
        )

    import cloud_drive.store as store
    import server

    upstream_calls = []
    finalize_calls = 0

    def fake_upstream(method, path, *, query=None, body=None, config=None):
        upstream_calls.append((method, path))
        return {"code": 10000, "data": {"deleted": True}}

    def flaky_finalize(conn, *, user_id, file_id):
        nonlocal finalize_calls
        finalize_calls += 1
        if finalize_calls == 1:
            raise RuntimeError("finalize failed")
        store.finalize_delete_file(conn, user_id=user_id, file_id=file_id)

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    monkeypatch.setattr(server, "finalize_delete_file", flaky_finalize)

    first = client.delete(
        "/cloud-drive/files/delete_retry_file", headers=AUTH_HEADERS_A
    )
    assert first.status_code == 503
    storage = client.get("/cloud-drive/storage-usage", headers=AUTH_HEADERS_A)
    assert storage.get_json()["data"]["used_size"] == 50
    with sqlite_engine.connect() as conn:
        status = conn.execute(
            text("SELECT status FROM user_cloud_files WHERE file_id = :file_id"),
            {"file_id": "delete_retry_file"},
        ).scalar_one()
    assert status == "delete_pending"

    second = client.delete(
        "/cloud-drive/files/delete_retry_file", headers=AUTH_HEADERS_A
    )
    assert second.status_code == 200, second.get_json()
    storage = client.get("/cloud-drive/storage-usage", headers=AUTH_HEADERS_A)
    assert storage.get_json()["data"]["used_size"] == 0
    assert upstream_calls == [
        ("DELETE", "/v2/files/user/files/delete_retry_file"),
        ("DELETE", "/v2/files/user/files/delete_retry_file"),
    ]


def test_single_delete_restores_local_state_when_upstream_business_error(
    client, sqlite_engine, monkeypatch
):
    with sqlite_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO user_cloud_files (
                    reservation_id, user_id, app_key, file_id, object_key,
                    file_name, suffix, category, file_size, source, status,
                    progress, upstream_payload
                )
                VALUES (
                    'delete_restore', 1, :app_key, 'delete_restore_file', 'obj',
                    'clip.mp4', 'mp4', 1, 50, 'local_upload', 'completed',
                    100, '{}'
                )
                """
            ),
            {"app_key": KEY_A},
        )

    def fake_upstream(method, path, *, query=None, body=None, config=None):
        return {"code": 50001, "message": "delete failed", "data": None}

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    res = client.delete(
        "/cloud-drive/files/delete_restore_file", headers=AUTH_HEADERS_A
    )

    assert res.status_code == 502
    storage = client.get("/cloud-drive/storage-usage", headers=AUTH_HEADERS_A)
    assert storage.get_json()["data"]["used_size"] == 50
    files = client.get("/cloud-drive/files", headers=AUTH_HEADERS_A)
    assert files.get_json()["data"]["items"][0]["file_id"] == "delete_restore_file"


def test_batch_delete_keeps_quota_until_upstream_confirms(
    client, sqlite_engine, monkeypatch
):
    with sqlite_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO user_cloud_files (
                    reservation_id, user_id, app_key, file_id, object_key,
                    file_name, suffix, category, file_size, source, status,
                    progress, upstream_payload
                )
                VALUES (
                    'batch_delete_order', 1, :app_key, 'batch_delete_file', 'obj',
                    'clip.mp4', 'mp4', 1, 50, 'local_upload', 'completed',
                    100, '{}'
                )
                """
            ),
            {"app_key": KEY_A},
        )

    def fake_upstream(method, path, *, query=None, body=None, config=None):
        with sqlite_engine.connect() as conn:
            status = conn.execute(
                text("SELECT status FROM user_cloud_files WHERE file_id = :file_id"),
                {"file_id": "batch_delete_file"},
            ).scalar_one()
            used = conn.execute(
                text(
                    "SELECT COALESCE(SUM(file_size), 0) FROM user_cloud_files "
                    "WHERE user_id = 1 AND status IN ("
                    "'reserved', 'completed', 'delete_pending', "
                    "'transfer_pending', 'transfer_running', 'transfer_completed')"
                )
            ).scalar_one()
        assert status == "delete_pending"
        assert int(used) == 50
        return {"code": 10000, "data": {"deleted": True}}

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    res = client.post(
        "/cloud-drive/transfer/batch-delete",
        headers=AUTH_HEADERS_A,
        json={"file_ids": ["batch_delete_file"]},
    )

    assert res.status_code == 200, res.get_json()
    storage = client.get("/cloud-drive/storage-usage", headers=AUTH_HEADERS_A)
    assert storage.get_json()["data"]["used_size"] == 0


def test_batch_delete_restores_local_state_when_upstream_business_error(
    client, sqlite_engine, monkeypatch
):
    with sqlite_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO user_cloud_files (
                    reservation_id, user_id, app_key, file_id, object_key,
                    file_name, suffix, category, file_size, source, status,
                    progress, upstream_payload
                )
                VALUES (
                    'batch_restore', 1, :app_key, 'batch_restore_file', 'obj',
                    'clip.mp4', 'mp4', 1, 50, 'local_upload', 'completed',
                    100, '{}'
                )
                """
            ),
            {"app_key": KEY_A},
        )

    def fake_upstream(method, path, *, query=None, body=None, config=None):
        return {"code": 50001, "message": "batch delete failed", "data": None}

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    res = client.post(
        "/cloud-drive/transfer/batch-delete",
        headers=AUTH_HEADERS_A,
        json={"file_ids": ["batch_restore_file"]},
    )

    assert res.status_code == 502
    storage = client.get("/cloud-drive/storage-usage", headers=AUTH_HEADERS_A)
    assert storage.get_json()["data"]["used_size"] == 50
    files = client.get("/cloud-drive/files", headers=AUTH_HEADERS_A)
    assert files.get_json()["data"]["items"][0]["file_id"] == "batch_restore_file"


def test_batch_delete_keeps_pending_when_upstream_deleted_and_finalize_fails(
    client, sqlite_engine, monkeypatch
):
    with sqlite_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO user_cloud_files (
                    reservation_id, user_id, app_key, file_id, object_key,
                    file_name, suffix, category, file_size, source, status,
                    progress, upstream_payload
                )
                VALUES (
                    'batch_finalize_restore', 1, :app_key,
                    'batch_finalize_restore_file', 'obj', 'clip.mp4', 'mp4',
                    1, 50, 'local_upload', 'completed', 100, '{}'
                )
                """
            ),
            {"app_key": KEY_A},
        )

    def fake_upstream(method, path, *, query=None, body=None, config=None):
        return {"code": 10000, "data": {"deleted": True}}

    def broken_finalize(*args, **kwargs):
        raise RuntimeError("finalize failed")

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    monkeypatch.setattr(server, "_finalize_cloud_drive_files_delete", broken_finalize)
    res = client.post(
        "/cloud-drive/transfer/batch-delete",
        headers=AUTH_HEADERS_A,
        json={"file_ids": ["batch_finalize_restore_file"]},
    )

    assert res.status_code == 503
    storage = client.get("/cloud-drive/storage-usage", headers=AUTH_HEADERS_A)
    assert storage.get_json()["data"]["used_size"] == 50
    with sqlite_engine.connect() as conn:
        status = conn.execute(
            text("SELECT status FROM user_cloud_files WHERE file_id = :file_id"),
            {"file_id": "batch_finalize_restore_file"},
        ).scalar_one()
    assert status == "delete_pending"


def test_batch_delete_cross_tenant_returns_404_without_upstream(
    client, sqlite_engine, monkeypatch
):
    with sqlite_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO user_cloud_files (
                    reservation_id, user_id, app_key, file_id, object_key,
                    file_name, suffix, category, file_size, source, status,
                    progress, upstream_payload
                )
                VALUES (
                    'batch_owned_by_a', 1, :app_key, 'batch_owned_by_a', 'obj',
                    'clip.mp4', 'mp4', 1, 50, 'local_upload', 'completed',
                    100, '{}'
                )
                """
            ),
            {"app_key": KEY_A},
        )

    def fake_upstream(*args, **kwargs):
        raise AssertionError("cross-tenant batch delete must not hit upstream")

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    res = client.post(
        "/cloud-drive/transfer/batch-delete",
        headers=AUTH_HEADERS_B,
        json={"file_ids": ["batch_owned_by_a"]},
    )

    assert res.status_code == 404
    assert res.get_json()["error"]["code"] == "NOT_FOUND"


def test_files_batch_delete_happy_path_returns_upstream_data(
    client, sqlite_engine, monkeypatch
):
    with sqlite_engine.begin() as conn:
        for idx in (1, 2):
            conn.execute(
                text(
                    """
                    INSERT INTO user_cloud_files (
                        reservation_id, user_id, app_key, file_id, object_key,
                        file_name, suffix, category, file_size, source, status,
                        progress, upstream_payload
                    )
                    VALUES (
                        :rid, 1, :app_key, :fid, 'obj', 'clip.mp4', 'mp4',
                        1, 50, 'local_upload', 'completed', 100, '{}'
                    )
                    """
                ),
                {
                    "rid": f"files_batch_{idx}",
                    "fid": f"files_batch_file_{idx}",
                    "app_key": KEY_A,
                },
            )

    upstream_calls = []

    def fake_upstream(method, path, *, query=None, body=None, config=None):
        upstream_calls.append((method, path, body))
        return {
            "code": 10000,
            "data": {
                "requested_count": 2,
                "deleted_count": 2,
                "failed_count": 0,
                "failed_items": [],
                "storage_usage": {
                    "used_size": 0,
                    "max_size": 3221225472,
                    "file_count": 0,
                    "usage_percentage": 0.0,
                },
            },
        }

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    res = client.post(
        "/cloud-drive/files/batch-delete",
        headers=AUTH_HEADERS_A,
        json={"file_ids": ["files_batch_file_1", "files_batch_file_2"]},
    )

    assert res.status_code == 200, res.get_json()
    data = res.get_json()["data"]
    assert data["requested_count"] == 2
    assert data["deleted_count"] == 2
    assert data["failed_count"] == 0
    assert data["storage_usage"]["used_size"] == 0
    assert len(upstream_calls) == 1
    assert upstream_calls[0][1] == "/v2/files/user/files/batch-delete"


def test_files_batch_delete_rejects_empty_file_ids(client, monkeypatch):
    import server

    def fake_upstream(*args, **kwargs):
        raise AssertionError("validation failure must not hit upstream")

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    res = client.post(
        "/cloud-drive/files/batch-delete",
        headers=AUTH_HEADERS_A,
        json={"file_ids": []},
    )

    assert res.status_code == 400
    assert res.get_json()["error"]["code"] == "BAD_REQUEST"


def test_files_batch_delete_rejects_oversize_batch(client, monkeypatch):
    import server

    def fake_upstream(*args, **kwargs):
        raise AssertionError("oversize batch must not hit upstream")

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    res = client.post(
        "/cloud-drive/files/batch-delete",
        headers=AUTH_HEADERS_A,
        json={"file_ids": [f"oversize_{i}" for i in range(51)]},
    )

    assert res.status_code == 400
    assert res.get_json()["error"]["code"] == "BAD_REQUEST"


def test_files_batch_delete_cross_tenant_returns_404(
    client, sqlite_engine, monkeypatch
):
    with sqlite_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO user_cloud_files (
                    reservation_id, user_id, app_key, file_id, object_key,
                    file_name, suffix, category, file_size, source, status,
                    progress, upstream_payload
                )
                VALUES (
                    'files_batch_owned_by_a', 1, :app_key, 'files_batch_owned_by_a',
                    'obj', 'clip.mp4', 'mp4', 1, 50, 'local_upload', 'completed',
                    100, '{}'
                )
                """
            ),
            {"app_key": KEY_A},
        )

    def fake_upstream(*args, **kwargs):
        raise AssertionError("cross-tenant batch delete must not hit upstream")

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    res = client.post(
        "/cloud-drive/files/batch-delete",
        headers=AUTH_HEADERS_B,
        json={"file_ids": ["files_batch_owned_by_a"]},
    )

    assert res.status_code == 404
    assert res.get_json()["error"]["code"] == "NOT_FOUND"


def test_files_batch_delete_restores_local_state_on_upstream_error(
    client, sqlite_engine, monkeypatch
):
    with sqlite_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO user_cloud_files (
                    reservation_id, user_id, app_key, file_id, object_key,
                    file_name, suffix, category, file_size, source, status,
                    progress, upstream_payload
                )
                VALUES (
                    'files_batch_restore', 1, :app_key,
                    'files_batch_restore_file', 'obj', 'clip.mp4', 'mp4',
                    1, 50, 'local_upload', 'completed', 100, '{}'
                )
                """
            ),
            {"app_key": KEY_A},
        )

    def fake_upstream(method, path, *, query=None, body=None, config=None):
        return {"code": 50001, "message": "batch delete failed", "data": None}

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    res = client.post(
        "/cloud-drive/files/batch-delete",
        headers=AUTH_HEADERS_A,
        json={"file_ids": ["files_batch_restore_file"]},
    )

    assert res.status_code == 502
    storage = client.get("/cloud-drive/storage-usage", headers=AUTH_HEADERS_A)
    assert storage.get_json()["data"]["used_size"] == 50


def test_files_batch_delete_partial_upstream_failure_keeps_failed_file(
    client, sqlite_engine, monkeypatch
):
    with sqlite_engine.begin() as conn:
        for idx in (1, 2):
            conn.execute(
                text(
                    """
                    INSERT INTO user_cloud_files (
                        reservation_id, user_id, app_key, file_id, object_key,
                        file_name, suffix, category, file_size, source, status,
                        progress, upstream_payload
                    )
                    VALUES (
                        :rid, 1, :app_key, :fid, 'obj', 'clip.mp4', 'mp4',
                        1, 50, 'local_upload', 'completed', 100, '{}'
                    )
                    """
                ),
                {
                    "rid": f"files_batch_partial_{idx}",
                    "fid": f"files_batch_partial_file_{idx}",
                    "app_key": KEY_A,
                },
            )

    def fake_upstream(method, path, *, query=None, body=None, config=None):
        return {
            "code": 10000,
            "data": {
                "requested_count": 2,
                "deleted_count": 1,
                "failed_count": 1,
                "failed_items": [
                    {
                        "file_id": "files_batch_partial_file_2",
                        "reason": "not_found",
                    }
                ],
                "storage_usage": {
                    "used_size": 50,
                    "max_size": 3221225472,
                    "file_count": 1,
                    "usage_percentage": 0.0,
                },
            },
        }

    import server

    monkeypatch.setattr(server, "call_cloud_drive_upstream", fake_upstream)
    res = client.post(
        "/cloud-drive/files/batch-delete",
        headers=AUTH_HEADERS_A,
        json={
            "file_ids": [
                "files_batch_partial_file_1",
                "files_batch_partial_file_2",
            ]
        },
    )

    assert res.status_code == 200, res.get_json()
    data = res.get_json()["data"]
    assert data["deleted_count"] == 1
    assert data["failed_count"] == 1
    assert data["failed_items"][0]["file_id"] == "files_batch_partial_file_2"

    # The succeeded file is finalized; the upstream-rejected file is restored
    # to its original `completed` status so the next listing still shows it.
    with sqlite_engine.connect() as conn:
        rows = {
            row.file_id: row.status
            for row in conn.execute(
                text(
                    "SELECT file_id, status FROM user_cloud_files "
                    "WHERE app_key = :app_key"
                ),
                {"app_key": KEY_A},
            )
        }
    assert rows["files_batch_partial_file_1"] == "deleted"
    assert rows["files_batch_partial_file_2"] == "completed"


def test_cloud_drive_routes_require_bearer(client):
    res = client.get("/cloud-drive/storage-usage", headers={"X-Web-App-Key": KEY_A})

    assert res.status_code == 401
    assert res.get_json()["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.skipif(
    not os.environ.get("CLOUD_DRIVE_POSTGRES_TEST_URL"),
    reason="requires CLOUD_DRIVE_POSTGRES_TEST_URL for row-lock concurrency coverage",
)
def test_postgres_quota_race_allows_only_one_reservation():
    from cloud_drive.store import CloudDriveStoreError, reserve_local_upload

    postgres_url = os.environ["CLOUD_DRIVE_POSTGRES_TEST_URL"]
    admin_engine = create_engine(postgres_url)
    schema = f"cloud_drive_quota_race_{uuid.uuid4().hex}"
    with admin_engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))

    test_engine = create_engine(
        postgres_url,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    outcomes = queue.Queue()
    first_reserved = threading.Event()
    second_attempting_reservation = threading.Event()
    release_first = threading.Event()

    try:
        with test_engine.begin() as conn:
            for statement in POSTGRES_SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(text(statement))
            conn.execute(
                text(
                    "INSERT INTO users (app_key, id, cloud_drive_quota_bytes) "
                    "VALUES (:k, 1, 100)"
                ),
                {"k": KEY_A},
            )

        def first_worker():
            conn = test_engine.connect()
            trans = conn.begin()
            try:
                reserve_local_upload(
                    conn,
                    user_id=1,
                    app_key=KEY_A,
                    file_name="first.mp4",
                    file_size=60,
                    content_type="video/mp4",
                )
                first_reserved.set()
                if not release_first.wait(timeout=5):
                    raise AssertionError("second reservation did not start")
                trans.commit()
                outcomes.put("ok")
            except Exception as error:
                trans.rollback()
                outcomes.put(error)
            finally:
                conn.close()

        def second_worker():
            if not first_reserved.wait(timeout=5):
                outcomes.put(AssertionError("first reservation did not acquire lock"))
                return
            conn = test_engine.connect()
            trans = conn.begin()
            try:
                second_attempting_reservation.set()
                reserve_local_upload(
                    conn,
                    user_id=1,
                    app_key=KEY_A,
                    file_name="second.mp4",
                    file_size=60,
                    content_type="video/mp4",
                )
                trans.commit()
                outcomes.put("ok")
            except CloudDriveStoreError as error:
                trans.rollback()
                outcomes.put(error.code)
            except Exception as error:
                trans.rollback()
                outcomes.put(error)
            finally:
                conn.close()

        first = threading.Thread(target=first_worker)
        second = threading.Thread(target=second_worker)
        first.start()
        second.start()
        assert second_attempting_reservation.wait(timeout=5)
        with pytest.raises(queue.Empty):
            outcomes.get(timeout=0.25)
        release_first.set()
        first.join(timeout=5)
        second.join(timeout=5)

        result = [outcomes.get_nowait() for _ in range(outcomes.qsize())]
        unexpected = [
            item for item in result if item not in {"ok", "CLOUD_DRIVE_QUOTA_EXCEEDED"}
        ]
        assert unexpected == []
        assert result.count("ok") == 1
        assert result.count("CLOUD_DRIVE_QUOTA_EXCEEDED") == 1
    finally:
        test_engine.dispose()
        with admin_engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()
