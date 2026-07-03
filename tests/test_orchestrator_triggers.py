"""Tests for orchestrator.triggers.

Strategy: monkeypatch `orchestrator.triggers.proxy_narrator_upstream` to
capture the upstream path + body that would be sent, plus return a fake
`{code, message, data: {task_id: ...}}` shape mirroring real upstream.

Coverage:
  - All 8 trigger steps build the same body the web sync/route.ts
    triggerNextStep produces (field-for-field comparison)
  - Missing required field returns TriggerResult(success=False, error=...)
  - Upstream error (UpstreamNarratorError) is flattened into error string
  - Upstream not returning task_id is reported as failure
  - Unknown step returns failure
  - subsync is rejected with explicit message (sync/route.ts never wires it)
"""

from __future__ import annotations

import pytest

from narrator_metadata.upstream import UpstreamNarratorError
import orchestrator.triggers as triggers_module
from orchestrator.triggers import TriggerResult, trigger_next_step


# ─── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture()
def capture_upstream(monkeypatch):
    """Capture the upstream call args. Returns a list — the test reads
    the first (and usually only) entry. Default upstream response is a
    well-formed wrapped task_id; tests may override `response`."""
    captured: list[dict] = []
    response = {"code": 0, "message": "ok", "data": {"task_id": "upstream-task-1"}}

    def fake_proxy(upstream_path, *, method="GET", body=None, timeout_seconds=10.0, query_params=None):
        captured.append(
            {
                "upstream_path": upstream_path,
                "method": method,
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        return response

    monkeypatch.setattr(triggers_module, "proxy_narrator_upstream", fake_proxy)
    return captured


# ─── happy-path body construction (one assertion per step) ──────────────────


class TestTriggerBodies:
    def test_subtitle_extract_body(self, capture_upstream):
        task = {"raw_video_id": "video-raw-1"}
        result = trigger_next_step("subtitle_extract", task)
        assert result.success and result.task_id == "upstream-task-1"
        sent = capture_upstream[0]
        assert sent["upstream_path"] == "/v2/task/ocr_extraction/create"
        assert sent["method"] == "POST"
        assert sent["body"] == {
            "file_id": ["video-raw-1"],
            "mode": 2,
            "language": "Auto-Detect",
            "subtitle_position": "auto",
        }

    def test_subtitle_removal_body_default_mode(self, capture_upstream):
        result = trigger_next_step("subtitle_removal", {"raw_video_id": "v1"})
        assert result.success
        assert capture_upstream[0]["body"] == {
            "file_ids": ["v1"],
            "mode": "standard",
        }

    def test_subtitle_removal_body_custom_mode(self, capture_upstream):
        result = trigger_next_step(
            "subtitle_removal", {"raw_video_id": "v1", "removal_mode": "aggressive"}
        )
        assert result.success
        assert capture_upstream[0]["body"]["mode"] == "aggressive"

    def test_popular_learning_prefers_learning_srt_id(self, capture_upstream):
        task = {
            "learning_srt_id": "srt-learn",
            "native_srt_id": "srt-native",
            "narrator_type": "third_person",
            "model_version": "v2",
        }
        result = trigger_next_step("popular_learning", task)
        assert result.success
        body = capture_upstream[0]["body"]
        assert body == {
            "video_srt_path": "srt-learn",
            "narrator_type": "third_person",
            "model_version": "v2",
        }

    def test_popular_learning_falls_back_to_native_srt(self, capture_upstream):
        task = {"native_srt_id": "srt-native", "narrator_type": "x", "model_version": "v"}
        result = trigger_next_step("popular_learning", task)
        assert result.success
        assert capture_upstream[0]["body"]["video_srt_path"] == "srt-native"

    def test_generate_writing_uses_learning_model_from_previous_step(
        self, capture_upstream
    ):
        task = {
            "steps": {
                "popular_learning": {"result": {"learning_model_id": "model-from-pl"}}
            },
            "native_video_id": "nv-1",
            "native_srt_id": "ns-1",
            "playlet_name": "Foo",
            "target_platform": "douyin",
            "task_count": 3,
            "refine_gaps": True,
        }
        result = trigger_next_step("generate_writing", task)
        assert result.success
        body = capture_upstream[0]["body"]
        assert body["learning_model_id"] == "model-from-pl"
        assert body["playlet_name"] == "Foo"
        assert body["target_platform"] == "douyin"
        assert body["task_count"] == 3
        assert body["refine_srt_gaps"] == "1"
        assert body["story_info"] == ""
        assert body["target_character_name"] == ""
        # default vendor_requirements baked in when caller doesn't set
        assert "短视频平台" in body["vendor_requirements"]
        assert body["episodes_data"] == [
            {
                "video_oss_key": "nv-1",
                "srt_oss_key": "ns-1",
                "negative_oss_key": "nv-1",
                "num": 1,
            }
        ]

    def test_generate_writing_uses_existing_model_id_when_no_step_result(
        self, capture_upstream
    ):
        task = {
            "existing_model_id": "existing-model-9",
            "native_video_id": "nv",
            "native_srt_id": "ns",
        }
        result = trigger_next_step("generate_writing", task)
        assert result.success
        assert (
            capture_upstream[0]["body"]["learning_model_id"] == "existing-model-9"
        )

    def test_generate_writing_missing_learning_model_id(self, capture_upstream):
        result = trigger_next_step("generate_writing", {"native_video_id": "nv"})
        assert not result.success
        assert "learning_model_id" in result.error
        assert capture_upstream == []

    def test_fast_generate_writing_first_person_mode(self, capture_upstream):
        task = {
            "narrator_type_label": "第一人称视角",
            "writing_type": 2,
            "playlet_name": "x",
            "native_video_id": "nv",
            "native_srt_id": "ns",
            "writing_model": "pro",
            "writing_language": "English",
            "use_existing_model": False,
            "learning_srt_id": "ls-1",
        }
        result = trigger_next_step("fast_generate_writing", task)
        assert result.success
        body = capture_upstream[0]["body"]
        assert body["target_mode"] == 2
        assert body["perspective"] == "first_person"
        assert body["model"] == "pro"
        assert body["language"] == "English"
        assert body["learning_srt"] == "ls-1"
        assert "learning_model_id" not in body

    def test_fast_generate_writing_short_drama_mode(self, capture_upstream):
        task = {
            "narrator_type_label": "短剧",
            "writing_type": 1,
            "confirmed_movie_json": "should-be-ignored",
            "native_video_id": "nv",
            "native_srt_id": "ns",
            "use_existing_model": True,
            "existing_model_id": "em-1",
        }
        result = trigger_next_step("fast_generate_writing", task)
        assert result.success
        body = capture_upstream[0]["body"]
        # 短剧 forces target_mode=3 regardless of writing_type, and clears
        # confirmed_movie_json.
        assert body["target_mode"] == 3
        assert body["confirmed_movie_json"] == ""
        assert body["learning_model_id"] == "em-1"

    def test_clip_data_uses_generate_writing_task_int_id(self, capture_upstream):
        task = {
            "steps": {
                "generate_writing": {
                    "result": {"task_int_id": 1234, "order_num": "ord-c"}
                }
            },
            "dubing_id": "dub-1",
            "bgm_id": "bgm-5",
        }
        result = trigger_next_step("clip_data", task)
        assert result.success
        body = capture_upstream[0]["body"]
        assert body == {
            "generate_task_id": "1234",
            "order_num": "ord-c",
            "bgm": "bgm-5",
            "dubbing": "dub-1",
            "dubbing_type": "default",
        }

    def test_clip_data_falls_back_to_raw_tasks_first_id(self, capture_upstream):
        task = {
            "steps": {
                "generate_writing": {
                    "result": {
                        "raw_results": {"tasks": [{"id": 555}]},
                        "writing_order_num": "wno-1",
                    }
                }
            },
            "dubing_id": "dub-1",
        }
        result = trigger_next_step("clip_data", task)
        assert result.success
        body = capture_upstream[0]["body"]
        assert body["generate_task_id"] == "555"
        assert body["order_num"] == "wno-1"

    def test_clip_data_missing_dubing_id(self, capture_upstream):
        task = {
            "steps": {
                "generate_writing": {"result": {"task_int_id": 1, "order_num": "o"}}
            }
        }
        result = trigger_next_step("clip_data", task)
        assert not result.success
        assert "dubing_id" in result.error

    def test_clip_data_missing_both_task_id_and_order_num(self, capture_upstream):
        task = {"steps": {"generate_writing": {"result": {}}}, "dubing_id": "d"}
        result = trigger_next_step("clip_data", task)
        assert not result.success
        assert "generate_writing" in result.error

    def test_fast_clip_uses_fast_step_writing_task_and_file(self, capture_upstream):
        task = {
            "steps": {
                "fast_generate_writing": {
                    "result": {
                        "writing_task_id": "wt-7",
                        "file_id": "f-9",
                    }
                }
            },
            "dubing_id": "dub-1",
            "playlet_name": "show",
            "native_video_id": "nv",
            "native_srt_id": "ns",
        }
        result = trigger_next_step("generate_fast_writing_clip_data", task)
        assert result.success
        body = capture_upstream[0]["body"]
        assert body["task_id"] == "wt-7"
        assert body["file_id"] == "f-9"
        assert body["dubbing"] == "dub-1"
        assert body["dubbing_type"] == "普通话"
        assert body["episodes_data"][0]["video_oss_key"] == "nv"

    def test_fast_clip_falls_back_to_order_info_and_file_ids(self, capture_upstream):
        task = {
            "steps": {
                "fast_generate_writing": {
                    "result": {
                        "raw_results": {
                            "order_info": {"task_id": "from-order"},
                            "file_ids": ["from-file-ids"],
                        }
                    }
                }
            },
            "dubing_id": "d",
        }
        result = trigger_next_step("generate_fast_writing_clip_data", task)
        assert result.success
        body = capture_upstream[0]["body"]
        assert body["task_id"] == "from-order"
        assert body["file_id"] == "from-file-ids"

    def test_fast_clip_extracts_file_id_from_nested_task_result_json(
        self, capture_upstream
    ):
        task = {
            "steps": {
                "fast_generate_writing": {
                    "result": {
                        "raw_results": {
                            "order_info": {"task_id": "t"},
                            "tasks": [
                                {
                                    "task_result": '{"data": {"file_id": "nested-file"}}'
                                }
                            ],
                        }
                    }
                }
            },
            "dubing_id": "d",
        }
        result = trigger_next_step("generate_fast_writing_clip_data", task)
        assert result.success
        assert capture_upstream[0]["body"]["file_id"] == "nested-file"

    def test_fast_clip_missing_task_id(self, capture_upstream):
        task = {
            "steps": {
                "fast_generate_writing": {"result": {"file_id": "f"}},
            },
            "dubing_id": "d",
        }
        result = trigger_next_step("generate_fast_writing_clip_data", task)
        assert not result.success
        assert "task_id" in result.error

    def test_fast_clip_missing_file_id(self, capture_upstream):
        task = {
            "steps": {
                "fast_generate_writing": {"result": {"writing_task_id": "t"}},
            },
            "dubing_id": "d",
        }
        result = trigger_next_step("generate_fast_writing_clip_data", task)
        assert not result.success
        assert "file_id" in result.error

    def test_video_composing_uses_clip_data_order_num(self, capture_upstream):
        task = {
            "steps": {
                "clip_data": {"result": {"task_order_num": "TON-1"}},
            }
        }
        result = trigger_next_step("video_composing", task)
        assert result.success
        assert capture_upstream[0]["body"] == {"order_num": "TON-1"}

    def test_video_composing_falls_back_to_fast_clip(self, capture_upstream):
        task = {
            "steps": {
                "generate_fast_writing_clip_data": {
                    "result": {"task_order_num": "FAST-TON"}
                }
            }
        }
        result = trigger_next_step("video_composing", task)
        assert result.success
        assert capture_upstream[0]["body"] == {"order_num": "FAST-TON"}

    def test_video_composing_missing_order_num(self, capture_upstream):
        task = {"steps": {"clip_data": {"result": {}}}}
        result = trigger_next_step("video_composing", task)
        assert not result.success
        assert "task_order_num" in result.error


# ─── upstream failure paths ─────────────────────────────────────────────────


class TestUpstreamFailures:
    def test_upstream_error_is_flattened(self, monkeypatch):
        def boom(*a, **k):
            raise UpstreamNarratorError(
                502, "UPSTREAM_TIMEOUT", "upstream timed out"
            )

        monkeypatch.setattr(triggers_module, "proxy_narrator_upstream", boom)
        result = trigger_next_step("subtitle_extract", {"raw_video_id": "v"})
        assert not result.success
        assert "UPSTREAM_TIMEOUT" in result.error
        assert "timed out" in result.error

    def test_upstream_response_without_task_id_is_failure(self, monkeypatch):
        monkeypatch.setattr(
            triggers_module,
            "proxy_narrator_upstream",
            lambda *a, **k: {"code": 0, "data": {}},
        )
        result = trigger_next_step("subtitle_extract", {"raw_video_id": "v"})
        assert not result.success
        assert "task_id" in result.error


# ─── policy ─────────────────────────────────────────────────────────────────


class TestPolicy:
    def test_unknown_step(self, capture_upstream):
        result = trigger_next_step("does_not_exist", {})
        assert not result.success
        assert "未知步骤" in result.error
        assert capture_upstream == []

    def test_subsync_rejected_explicitly(self, capture_upstream):
        # subsync upstream path is configured but no body builder, so the
        # build path returns a TriggerResult failure before any upstream call.
        result = trigger_next_step("subsync", {})
        assert not result.success
        assert capture_upstream == []


# ─── regression coverage playlet multi-episode (uses _build_episodes_data) ────────────────


class TestPlayletMultiEpisode:
    """When a playlet master task carries `episodes_data: [...]`, every
    upstream step that builds `episodes_data` must expand to N entries
    instead of the legacy single-episode shape .
    """

    @pytest.fixture()
    def task_3eps(self):
        return {
            "existing_model_id": "model-1",
            "native_video_id": "ep1-video",
            "native_srt_id": "ep1-srt",
            "playlet_name": "下山应劫",
            "narrator_type_label": "短剧",
            "writing_model": "flash",
            "dubing_id": "voice-1",
            "episodes_data": [
                {"video_id": "v1", "srt_id": "s1"},
                {"video_id": "v2", "srt_id": "s2"},
                {"video_id": "v3", "srt_id": "s3"},
            ],
        }

    def test_generate_writing_expands_all_episodes(self, capture_upstream, task_3eps):
        result = trigger_next_step("generate_writing", task_3eps)
        assert result.success
        assert capture_upstream[0]["body"]["episodes_data"] == [
            {"video_oss_key": "v1", "srt_oss_key": "s1", "negative_oss_key": "v1", "num": 1},
            {"video_oss_key": "v2", "srt_oss_key": "s2", "negative_oss_key": "v2", "num": 2},
            {"video_oss_key": "v3", "srt_oss_key": "s3", "negative_oss_key": "v3", "num": 3},
        ]

    def test_fast_generate_writing_expands_all_episodes(
        self, capture_upstream, task_3eps
    ):
        task_3eps["use_existing_model"] = True
        result = trigger_next_step("fast_generate_writing", task_3eps)
        assert result.success
        assert len(capture_upstream[0]["body"]["episodes_data"]) == 3

    def test_fast_clip_expands_all_episodes(self, capture_upstream, task_3eps):
        task_3eps["steps"] = {
            "fast_generate_writing": {
                "result": {"writing_task_id": "wt-1", "file_id": "f-1"}
            }
        }
        result = trigger_next_step("generate_fast_writing_clip_data", task_3eps)
        assert result.success
        assert len(capture_upstream[0]["body"]["episodes_data"]) == 3

    def test_legacy_task_without_episodes_data_falls_back_to_native(
        self, capture_upstream
    ):
        # Previous task: no episodes_data field, only native_*. Must keep
        # working — fall back to the single-episode shape.
        task = {
            "existing_model_id": "model-1",
            "native_video_id": "nv",
            "native_srt_id": "ns",
            "playlet_name": "Foo",
        }
        result = trigger_next_step("generate_writing", task)
        assert result.success
        assert capture_upstream[0]["body"]["episodes_data"] == [
            {"video_oss_key": "nv", "srt_oss_key": "ns", "negative_oss_key": "nv", "num": 1}
        ]

    def test_empty_episodes_data_also_falls_back(self, capture_upstream):
        # Defensive: an empty list shouldn't produce an empty upstream
        # episodes_data (upstream would reject); fall back to native.
        task = {
            "existing_model_id": "model-1",
            "native_video_id": "nv",
            "native_srt_id": "ns",
            "episodes_data": [],
        }
        result = trigger_next_step("generate_writing", task)
        assert result.success
        assert capture_upstream[0]["body"]["episodes_data"] == [
            {"video_oss_key": "nv", "srt_oss_key": "ns", "negative_oss_key": "nv", "num": 1}
        ]
