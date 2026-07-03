"""State machine + result extraction for one-stop delivery pipeline.

1:1 port of narrator-ai-web `src/app/api/narrator/master-tasks/[id]/sync/
route.ts:46-128` (resolvePostSubtitleStep / resolveNextStep /
extractStepResult). Pure functions — no IO, no Flask context, no DB.
Keep behaviour byte-equivalent with the web side; any divergence is a
bug worth fixing on both sides at once, not a deliberate improvement
opportunity.
"""

from __future__ import annotations

import json
from typing import Any

# Step names accepted by resolve_next_step / extract_step_result /
# trigger_next_step. Mirrors the web `StepName` union.
STEP_NAMES = (
    "subtitle_extract",
    "subtitle_removal",
    "subsync",
    "popular_learning",
    "generate_writing",
    "fast_generate_writing",
    "generate_fast_writing_clip_data",
    "clip_data",
    "video_composing",
)


def _is_original_writing(task: dict) -> bool:
    """`writing_type == 1 | 2` means original (non-popular-learning) writing —
    the pipeline skips popular_learning and goes straight to fast writing.
    Mirrors sync/route.ts:47 `isOriginal`."""
    return task.get("writing_type") in (1, 2)


def resolve_post_subtitle_step(task: dict) -> str:
    """After subtitle_removal finishes, decide the next step based on
    enable_subsync / writing_type / use_existing_model. Mirrors
    sync/route.ts:46-51 `resolvePostSubtitleStep`."""
    if task.get("enable_subsync"):
        return "subsync"
    if _is_original_writing(task):
        return "fast_generate_writing"
    return "generate_writing" if task.get("use_existing_model") else "popular_learning"


def resolve_next_step(current_step: str, task: dict) -> str | None:
    """Pure step-graph successor. Returns None when the pipeline is done
    (video_composing → None). 1:1 port of sync/route.ts:53-69
    `resolveNextStep`."""
    if current_step == "subtitle_extract":
        return "subtitle_removal"
    if current_step == "subtitle_removal":
        return resolve_post_subtitle_step(task)
    if current_step == "subsync":
        if _is_original_writing(task):
            return "fast_generate_writing"
        return "generate_writing" if task.get("use_existing_model") else "popular_learning"
    if current_step == "popular_learning":
        return "generate_writing"
    if current_step == "generate_writing":
        return "clip_data"
    if current_step == "fast_generate_writing":
        return "generate_fast_writing_clip_data"
    if current_step == "generate_fast_writing_clip_data":
        return "video_composing"
    if current_step == "clip_data":
        return "video_composing"
    if current_step == "video_composing":
        return None
    return None


def _first_task(remote_data: dict) -> dict:
    """Pull `results.tasks[0]` (or top-level `tasks[0]`) safely."""
    results = remote_data.get("results") if isinstance(remote_data, dict) else None
    tasks = (results or {}).get("tasks") if isinstance(results, dict) else None
    if not tasks:
        tasks = remote_data.get("tasks") if isinstance(remote_data, dict) else None
    if isinstance(tasks, list) and tasks:
        first = tasks[0]
        return first if isinstance(first, dict) else {}
    return {}


def extract_step_result(step: str, remote_data: dict) -> dict[str, Any]:
    """Pull the per-step result fields the next step's trigger needs out
    of the upstream query response. 1:1 port of sync/route.ts:73-128
    `extractStepResult`.

    Subtitle steps read `tasks[0].task_result`; commentary steps read
    `results.order_info` + `tasks[0]`. Each branch keeps the same field
    names the web side writes so the persisted JSON blob remains
    byte-compatible across sync.ts and orchestrator-written rows.
    """
    if not isinstance(remote_data, dict):
        remote_data = {}

    if step == "subtitle_extract":
        first = _first_task(remote_data)
        results = remote_data.get("results") if isinstance(remote_data, dict) else None
        results_dict = results if isinstance(results, dict) else None
        return {
            "raw_results": results_dict if results_dict is not None else remote_data,
            "srt_file_id": first.get("task_result")
            or (results_dict.get("file_id") if results_dict else None),
            "task_result": first.get("task_result"),
        }

    if step == "subtitle_removal":
        first = _first_task(remote_data)
        results = remote_data.get("results") if isinstance(remote_data, dict) else None
        results_dict = results if isinstance(results, dict) else None
        return {
            "raw_results": results_dict if results_dict is not None else remote_data,
            "video_file_id": first.get("task_result")
            or (results_dict.get("file_id") if results_dict else None),
            "task_result": first.get("task_result"),
        }

    # Commentary steps share this base shape.
    results = remote_data.get("results")
    results_dict = results if isinstance(results, dict) else {}
    order_info = results_dict.get("order_info") or {}
    if not isinstance(order_info, dict):
        order_info = {}
    first = _first_task(remote_data)

    base: dict[str, Any] = {
        "raw_results": results_dict,
        "order_num": order_info.get("order_num"),
        "step_name": order_info.get("step"),
        "status_code": order_info.get("status"),
        "finished_at": remote_data.get("completed_at"),
    }

    if step == "popular_learning":
        learning_model_id = order_info.get("learning_model_id") or order_info.get("result")
        if not learning_model_id:
            # Web side parses task_result JSON and picks .agent_unique_code on miss.
            raw_task_result = first.get("task_result")
            if isinstance(raw_task_result, str) and raw_task_result.strip():
                try:
                    parsed = json.loads(raw_task_result)
                    if isinstance(parsed, dict):
                        learning_model_id = parsed.get("agent_unique_code")
                except (ValueError, TypeError):
                    pass
        return {**base, "learning_model_id": learning_model_id}

    if step in ("generate_writing", "fast_generate_writing"):
        file_ids = results_dict.get("file_ids") if isinstance(results_dict, dict) else None
        first_file_id = file_ids[0] if isinstance(file_ids, list) and file_ids else None
        return {
            **base,
            "writing_task_id": order_info.get("task_id") or remote_data.get("task_id"),
            "writing_order_num": order_info.get("order_num"),
            "task_int_id": first.get("id"),
            "file_id": first_file_id,
        }

    if step == "generate_fast_writing_clip_data":
        return {
            **base,
            "fast_clip_task_id": order_info.get("task_id") or remote_data.get("task_id"),
            "task_order_num": remote_data.get("task_order_num"),
        }

    if step == "clip_data":
        return {
            **base,
            "clip_data_task_id": order_info.get("task_id") or remote_data.get("task_id"),
            "task_order_num": remote_data.get("task_order_num"),
        }

    if step == "video_composing":
        return {
            **base,
            "video_url": first.get("video_url") or order_info.get("video_url"),
            "project_zip": first.get("project_zip") or order_info.get("project_zip"),
        }

    return base
