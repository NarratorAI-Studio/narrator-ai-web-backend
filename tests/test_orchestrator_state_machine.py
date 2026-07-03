"""Tests for orchestrator.state_machine.

Locks the resolve_next_step branching table and extract_step_result
field mapping so any divergence from sync/route.ts shows up here. New
combinations covered by sync.ts should also land here.
"""

from __future__ import annotations

import json

import pytest

from orchestrator.state_machine import (
    extract_step_result,
    resolve_next_step,
    resolve_post_subtitle_step,
)


# ─── resolve_next_step ──────────────────────────────────────────────────────


class TestResolveNextStep:
    def test_subtitle_extract_to_removal(self):
        assert resolve_next_step("subtitle_extract", {}) == "subtitle_removal"

    @pytest.mark.parametrize(
        "task,expected",
        [
            ({"enable_subsync": True}, "subsync"),
            ({"writing_type": 1}, "fast_generate_writing"),
            ({"writing_type": 2}, "fast_generate_writing"),
            ({"use_existing_model": True}, "generate_writing"),
            ({}, "popular_learning"),
        ],
    )
    def test_subtitle_removal_branches(self, task, expected):
        assert resolve_next_step("subtitle_removal", task) == expected

    def test_subtitle_removal_subsync_beats_original(self):
        """enable_subsync wins even when writing_type=1 (original)."""
        assert (
            resolve_next_step(
                "subtitle_removal", {"enable_subsync": True, "writing_type": 1}
            )
            == "subsync"
        )

    @pytest.mark.parametrize(
        "task,expected",
        [
            ({"writing_type": 1}, "fast_generate_writing"),
            ({"writing_type": 2}, "fast_generate_writing"),
            ({"use_existing_model": True}, "generate_writing"),
            ({}, "popular_learning"),
        ],
    )
    def test_subsync_branches(self, task, expected):
        assert resolve_next_step("subsync", task) == expected

    def test_popular_learning_to_generate_writing(self):
        assert resolve_next_step("popular_learning", {}) == "generate_writing"

    def test_generate_writing_to_clip_data(self):
        assert resolve_next_step("generate_writing", {}) == "clip_data"

    def test_fast_generate_writing_to_fast_clip(self):
        assert (
            resolve_next_step("fast_generate_writing", {})
            == "generate_fast_writing_clip_data"
        )

    def test_clip_data_to_video_composing(self):
        assert resolve_next_step("clip_data", {}) == "video_composing"

    def test_fast_clip_to_video_composing(self):
        assert (
            resolve_next_step("generate_fast_writing_clip_data", {})
            == "video_composing"
        )

    def test_video_composing_terminates(self):
        assert resolve_next_step("video_composing", {}) is None

    def test_unknown_step_terminates(self):
        assert resolve_next_step("does_not_exist", {}) is None


class TestResolvePostSubtitle:
    """Standalone coverage of the helper resolveNextStep delegates to."""

    @pytest.mark.parametrize(
        "task,expected",
        [
            ({"enable_subsync": True, "writing_type": 0}, "subsync"),
            ({"writing_type": 1}, "fast_generate_writing"),
            ({"writing_type": 2}, "fast_generate_writing"),
            ({"writing_type": 0, "use_existing_model": True}, "generate_writing"),
            ({"writing_type": 0}, "popular_learning"),
        ],
    )
    def test_branches(self, task, expected):
        assert resolve_post_subtitle_step(task) == expected


# ─── extract_step_result ────────────────────────────────────────────────────


class TestExtractStepResult:
    def test_subtitle_extract_reads_first_task_result(self):
        remote = {
            "results": {
                "tasks": [{"task_result": "srt-file-abc"}],
                "file_id": "ignored-fallback",
            }
        }
        out = extract_step_result("subtitle_extract", remote)
        assert out["srt_file_id"] == "srt-file-abc"
        assert out["task_result"] == "srt-file-abc"
        assert out["raw_results"] == remote["results"]

    def test_subtitle_extract_falls_back_to_results_file_id(self):
        remote = {"results": {"tasks": [{}], "file_id": "fallback-srt"}}
        out = extract_step_result("subtitle_extract", remote)
        assert out["srt_file_id"] == "fallback-srt"

    def test_subtitle_removal_reads_first_task_result(self):
        remote = {
            "results": {
                "tasks": [{"task_result": "video-clean-xyz"}],
            }
        }
        out = extract_step_result("subtitle_removal", remote)
        assert out["video_file_id"] == "video-clean-xyz"

    def test_popular_learning_prefers_order_info_field(self):
        remote = {
            "results": {
                "order_info": {"learning_model_id": "model-from-order", "order_num": "ord-1"},
                "tasks": [{"task_result": json.dumps({"agent_unique_code": "ignored"})}],
            },
            "completed_at": "2026-06-11T01:23:45Z",
        }
        out = extract_step_result("popular_learning", remote)
        assert out["learning_model_id"] == "model-from-order"
        assert out["order_num"] == "ord-1"
        assert out["finished_at"] == "2026-06-11T01:23:45Z"

    def test_popular_learning_falls_back_to_task_result_agent_unique_code(self):
        remote = {
            "results": {
                "order_info": {"order_num": "ord-1"},
                "tasks": [{"task_result": json.dumps({"agent_unique_code": "uniq-code"})}],
            }
        }
        out = extract_step_result("popular_learning", remote)
        assert out["learning_model_id"] == "uniq-code"

    def test_popular_learning_malformed_task_result_gives_none(self):
        remote = {
            "results": {
                "order_info": {"order_num": "ord-1"},
                "tasks": [{"task_result": "not-json"}],
            }
        }
        out = extract_step_result("popular_learning", remote)
        assert out["learning_model_id"] is None

    def test_generate_writing_captures_task_int_id_and_file_id(self):
        remote = {
            "task_id": "writing-task-top",
            "results": {
                "order_info": {"task_id": "writing-task-order", "order_num": "ord-w"},
                "tasks": [{"id": 9876}],
                "file_ids": ["file-001", "file-002"],
            },
        }
        out = extract_step_result("generate_writing", remote)
        assert out["writing_task_id"] == "writing-task-order"
        assert out["task_int_id"] == 9876
        assert out["file_id"] == "file-001"
        assert out["writing_order_num"] == "ord-w"

    def test_fast_generate_writing_uses_same_extractor_as_generate_writing(self):
        remote = {
            "task_id": "fast-task",
            "results": {
                "order_info": {"task_id": "fast-from-order"},
                "tasks": [{"id": 42}],
                "file_ids": ["fast-file"],
            },
        }
        out = extract_step_result("fast_generate_writing", remote)
        assert out["writing_task_id"] == "fast-from-order"
        assert out["file_id"] == "fast-file"

    def test_clip_data_captures_task_order_num(self):
        remote = {
            "task_order_num": "order-clip",
            "task_id": "clip-top",
            "results": {"order_info": {"task_id": "clip-from-order"}},
        }
        out = extract_step_result("clip_data", remote)
        assert out["clip_data_task_id"] == "clip-from-order"
        assert out["task_order_num"] == "order-clip"

    def test_fast_clip_data_captures_task_order_num(self):
        remote = {
            "task_order_num": "order-fast-clip",
            "results": {"order_info": {"task_id": "fc-from-order"}},
        }
        out = extract_step_result("generate_fast_writing_clip_data", remote)
        assert out["fast_clip_task_id"] == "fc-from-order"
        assert out["task_order_num"] == "order-fast-clip"

    def test_video_composing_pulls_video_url_and_zip(self):
        remote = {
            "results": {
                "order_info": {},
                "tasks": [
                    {
                        "video_url": "https://cdn/video.mp4",
                        "project_zip": "https://cdn/project.zip",
                    }
                ],
            }
        }
        out = extract_step_result("video_composing", remote)
        assert out["video_url"] == "https://cdn/video.mp4"
        assert out["project_zip"] == "https://cdn/project.zip"

    def test_video_composing_falls_back_to_order_info_fields(self):
        remote = {
            "results": {
                "order_info": {
                    "video_url": "https://order/video.mp4",
                    "project_zip": "https://order/project.zip",
                },
                "tasks": [{}],
            }
        }
        out = extract_step_result("video_composing", remote)
        assert out["video_url"] == "https://order/video.mp4"
        assert out["project_zip"] == "https://order/project.zip"

    def test_empty_remote_returns_safe_base(self):
        out = extract_step_result("generate_writing", {})
        # Base fields present, optional ones None — no KeyError.
        assert out["raw_results"] == {}
        assert out["writing_task_id"] is None
        assert out["task_int_id"] is None
