"""Unit tests for transfer error_message/error_code surfacing (regression coverage / Web API contract)."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, text

from cloud_drive.store import (
    mark_reservation_failed,
    row_to_transfer_task,
)


SQLITE_USER_CLOUD_FILES_SCHEMA = """
CREATE TABLE user_cloud_files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id  TEXT NOT NULL UNIQUE,
    user_id         INTEGER NOT NULL,
    app_key         TEXT NOT NULL,
    file_id         TEXT UNIQUE,
    object_key      TEXT,
    file_name       TEXT NOT NULL,
    suffix          TEXT NOT NULL DEFAULT '',
    category        INTEGER NOT NULL DEFAULT 0,
    file_size       INTEGER NOT NULL DEFAULT 0,
    content_type    TEXT,
    source          TEXT NOT NULL,
    status          TEXT NOT NULL,
    upstream_status INTEGER,
    progress        INTEGER NOT NULL DEFAULT 0,
    srt_file_hash   TEXT,
    upstream_payload TEXT NOT NULL DEFAULT '{}',
    upload_id       TEXT,
    parent_reservation_id TEXT,
    settled_at      TEXT,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at    TEXT,
    deleted_at      TEXT
);
"""


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    with eng.begin() as conn:
        conn.execute(text(SQLITE_USER_CLOUD_FILES_SCHEMA))
        conn.execute(
            text(
                "INSERT INTO user_cloud_files "
                "(reservation_id, user_id, app_key, file_name, source, status, upstream_payload) "
                "VALUES (:rid, 1, 'k', 'transfer', 'transfer', 'transfer_pending', '{}')"
            ),
            {"rid": "cdr_pending"},
        )
    yield eng
    eng.dispose()


def _read_payload(engine, reservation_id):
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT upstream_payload, status FROM user_cloud_files "
                "WHERE reservation_id = :rid"
            ),
            {"rid": reservation_id},
        ).first()
    payload = row[0]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return payload, row[1]


# ─── mark_reservation_failed ────────────────────────────────────────────────


def test_persists_error_message_and_code_to_payload(engine):
    with engine.begin() as conn:
        mark_reservation_failed(
            conn,
            reservation_id="cdr_pending",
            error_message="链接已过期，请重新生成",
            error_code="UPSTREAM_BUSINESS_ERROR",
        )

    payload, status = _read_payload(engine, "cdr_pending")
    assert status == "failed"
    assert payload["error_message"] == "链接已过期，请重新生成"
    assert payload["error_code"] == "UPSTREAM_BUSINESS_ERROR"


def test_merges_error_into_existing_upstream_payload(engine):
    with engine.begin() as conn:
        mark_reservation_failed(
            conn,
            reservation_id="cdr_pending",
            upstream_payload={"code": 40001, "trace_id": "abc"},
            error_message="link expired",
            error_code="UPSTREAM_BUSINESS_ERROR",
        )

    payload, _ = _read_payload(engine, "cdr_pending")
    assert payload["code"] == 40001
    assert payload["trace_id"] == "abc"
    assert payload["error_message"] == "link expired"
    assert payload["error_code"] == "UPSTREAM_BUSINESS_ERROR"


def test_no_error_args_keeps_payload_empty(engine):
    with engine.begin() as conn:
        mark_reservation_failed(conn, reservation_id="cdr_pending")

    payload, status = _read_payload(engine, "cdr_pending")
    assert status == "failed"
    assert payload == {}


def test_only_error_message_persists_message_only(engine):
    with engine.begin() as conn:
        mark_reservation_failed(
            conn,
            reservation_id="cdr_pending",
            error_message="something failed",
        )

    payload, _ = _read_payload(engine, "cdr_pending")
    assert payload == {"error_message": "something failed"}


def test_only_error_code_persists_code_only(engine):
    with engine.begin() as conn:
        mark_reservation_failed(
            conn,
            reservation_id="cdr_pending",
            error_code="INTERNAL_ERROR",
        )

    payload, _ = _read_payload(engine, "cdr_pending")
    assert payload == {"error_code": "INTERNAL_ERROR"}


# ─── row_to_transfer_task ────────────────────────────────────────────────


def _base_row(**overrides):
    base = {
        "reservation_id": "cdr_x",
        "file_id": None,
        "file_name": "transfer",
        "file_size": 0,
        "category": 0,
        "progress": 0,
        "status": "failed",
        "upstream_status": None,
        "created_at": None,
        "completed_at": None,
        "upstream_payload": {},
    }
    base.update(overrides)
    return base


def test_row_surfaces_error_fields_when_payload_has_them():
    result = row_to_transfer_task(
        _base_row(
            upstream_payload={
                "error_message": "链接已过期",
                "error_code": "UPSTREAM_BUSINESS_ERROR",
            }
        )
    )
    assert result["error_message"] == "链接已过期"
    assert result["error_code"] == "UPSTREAM_BUSINESS_ERROR"


def test_row_omits_error_fields_when_payload_empty():
    result = row_to_transfer_task(_base_row(upstream_payload={}))
    assert "error_message" not in result
    assert "error_code" not in result


def test_row_omits_error_fields_when_payload_is_not_dict():
    result = row_to_transfer_task(_base_row(upstream_payload=None))
    assert "error_message" not in result
    assert "error_code" not in result

    result = row_to_transfer_task(_base_row(upstream_payload=[]))
    assert "error_message" not in result
    assert "error_code" not in result


def test_row_omits_error_fields_when_values_are_empty_strings():
    result = row_to_transfer_task(
        _base_row(upstream_payload={"error_message": "", "error_code": ""})
    )
    assert "error_message" not in result
    assert "error_code" not in result


def test_row_omits_error_fields_when_values_are_non_strings():
    result = row_to_transfer_task(
        _base_row(upstream_payload={"error_message": 123, "error_code": None})
    )
    assert "error_message" not in result
    assert "error_code" not in result


def test_row_existing_fields_unchanged_when_payload_has_error():
    result = row_to_transfer_task(
        _base_row(
            file_id="upstream_xyz",
            file_name="my_video.mp4",
            file_size=12345,
            status="failed",
            upstream_payload={
                "error_message": "失败原因",
                "error_code": "X",
            },
        )
    )
    assert result["file_id"] == "upstream_xyz"
    assert result["upstream_file_id"] == "upstream_xyz"
    assert result["file_name"] == "my_video.mp4"
    assert result["file_size"] == "12345"
    assert result["status"] == 3
    assert result["error_message"] == "失败原因"
    assert result["error_code"] == "X"
