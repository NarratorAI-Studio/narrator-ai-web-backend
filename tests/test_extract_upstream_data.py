"""Unit tests for `_extract_upstream_data` upstream-message surfacing ."""

from __future__ import annotations

import pytest

from cloud_drive.store import CloudDriveStoreError
from server import _coerce_upstream_user_message, _extract_upstream_data


def test_surfaces_upstream_message_field():
    payload = {"code": 40001, "message": "链接已过期，请重新生成", "data": None}
    with pytest.raises(CloudDriveStoreError) as exc_info:
        _extract_upstream_data(payload)
    assert exc_info.value.message == "链接已过期，请重新生成"
    assert exc_info.value.code == "UPSTREAM_BUSINESS_ERROR"
    assert exc_info.value.http_status == 502
    assert exc_info.value.details == {"upstream_payload": payload}


def test_surfaces_upstream_msg_field_when_message_absent():
    payload = {"code": 40002, "msg": "文件格式不支持", "data": None}
    with pytest.raises(CloudDriveStoreError) as exc_info:
        _extract_upstream_data(payload)
    assert exc_info.value.message == "文件格式不支持"


def test_message_wins_over_msg_when_both_present():
    payload = {"code": 40003, "message": "primary", "msg": "secondary"}
    with pytest.raises(CloudDriveStoreError) as exc_info:
        _extract_upstream_data(payload)
    assert exc_info.value.message == "primary"


def test_fallback_when_message_missing():
    payload = {"code": 40004, "data": None}
    with pytest.raises(CloudDriveStoreError) as exc_info:
        _extract_upstream_data(payload)
    assert exc_info.value.message == "Internal cloud-drive returned a business error."


def test_fallback_when_message_empty_string():
    payload = {"code": 40005, "message": "", "data": None}
    with pytest.raises(CloudDriveStoreError) as exc_info:
        _extract_upstream_data(payload)
    assert exc_info.value.message == "Internal cloud-drive returned a business error."


def test_fallback_when_message_is_whitespace():
    payload = {"code": 40006, "message": "   ", "data": None}
    with pytest.raises(CloudDriveStoreError) as exc_info:
        _extract_upstream_data(payload)
    assert exc_info.value.message == "Internal cloud-drive returned a business error."


def test_fallback_when_message_is_non_string():
    payload = {"code": 40007, "message": 12345, "data": None}
    with pytest.raises(CloudDriveStoreError) as exc_info:
        _extract_upstream_data(payload)
    assert exc_info.value.message == "Internal cloud-drive returned a business error."


def test_strips_whitespace_around_message():
    payload = {"code": 40008, "message": "  请稍后重试  "}
    with pytest.raises(CloudDriveStoreError) as exc_info:
        _extract_upstream_data(payload)
    assert exc_info.value.message == "请稍后重试"


def test_success_code_passes_through_data_field():
    payload = {"code": 10000, "data": {"file_id": "abc"}}
    assert _extract_upstream_data(payload) == {"file_id": "abc"}


def test_payload_without_code_passes_through_unchanged():
    payload = {"data": {"x": 1}}
    assert _extract_upstream_data(payload) == {"x": 1}


def test_non_dict_payload_returned_as_is():
    assert _extract_upstream_data([1, 2, 3]) == [1, 2, 3]


def test_coerce_function_returns_fallback_for_dict_without_text():
    assert (
        _coerce_upstream_user_message({"code": 1})
        == "Internal cloud-drive returned a business error."
    )
