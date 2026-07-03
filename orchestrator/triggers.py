"""Per-step upstream trigger logic for one-stop delivery pipeline.

1:1 port of narrator-ai-web `sync/route.ts:132-226` (`triggerNextStep`).
Web side calls its own BFF endpoints (which are thin proxies to
`narrator_proxy/routes.py`); backend orchestrator skips the BFF hop and
goes directly to `proxy_narrator_upstream(<upstream_path>, ...)`. Body
shapes are kept byte-equivalent to what the web BFF receives, since the
BFF is a passthrough.

Identity model: every upstream call sends the backend's master
OPEN_FASTAPI_APP_KEY (handled inside `proxy_narrator_upstream`). The
caller's tenant identity (user_id / app_key) is used for backend-side
authorization / DB scoping only, never forwarded upstream.

Return: TriggerResult dataclass with success flag, upstream task_id on
success, error message on failure. `advance.py` consumes this and
writes either `{status: running, task_id}` or
`{status: failed, error}` to the step record.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from narrator_metadata.upstream import UpstreamNarratorError
from narrator_proxy.upstream import proxy_narrator_upstream


@dataclass(frozen=True)
class TriggerResult:
    success: bool
    task_id: str | None = None
    error: str | None = None


# Maps step name → upstream path. Mirrors the constants in
# narrator_proxy/routes.py. Kept inline (instead of importing the
# NarratorProxyRoute objects) because orchestrator only needs the
# upstream path; the backend HTTP-route metadata (endpoint_label,
# timeout, etc.) is irrelevant when we skip the BFF hop.
_UPSTREAM_PATHS: dict[str, str] = {
    "subtitle_extract":                "/v2/task/ocr_extraction/create",
    "subtitle_removal":                "/v2/task/subtitle_removal/create",
    "subsync":                         "/v2/task/commentary/create_subsync_task",
    "popular_learning":                "/v2/task/commentary/create_popular_learning",
    "generate_writing":                "/v2/task/commentary/create_generate_writing",
    "fast_generate_writing":           "/v2/task/commentary/create_fast_generate_writing",
    "clip_data":                       "/v2/task/commentary/create_generate_clip_data",
    "generate_fast_writing_clip_data": "/v2/task/commentary/create_generate_fast_writing_clip_data",
    "video_composing":                 "/v2/task/commentary/create_video_composing",
}
DEFAULT_UPSTREAM_TIMEOUT_SECONDS = 60.0


def _missing(field: str) -> TriggerResult:
    return TriggerResult(success=False, error=f"缺少 {field}")


def _build_episodes_data(task: dict) -> list[dict[str, Any]]:
    """Build the `episodes_data` payload for create-writing / clip-data
    upstream calls .

    Playlet multi-episode tasks persist every episode at
    `task['episodes_data']` (a list of ``{video_id, srt_id, ...}`` dicts
    written by web `page.tsx`). For everything else — and for legacy
    tasks created before that field existed — we fall back to the
    single ``native_video_id`` / ``native_srt_id`` pair so old tasks
    keep working unchanged.

    Upstream's create-writing / clip-data routes require both
    ``negative_oss_key`` and an integer ``num``, which is what this
    helper emits. (Subsync uses a different shape and is rejected by
    the orchestrator path anyway — see the ``step == "subsync"`` branch
    in ``_build_body`` — so a single shape is enough here.)
    """
    eps = task.get("episodes_data")
    if isinstance(eps, list) and eps:
        rows: list[dict[str, Any]] = []
        for i, ep in enumerate(eps):
            if not isinstance(ep, dict):
                continue
            rows.append(
                {
                    "video_oss_key": ep.get("video_id"),
                    "srt_oss_key": ep.get("srt_id"),
                    "negative_oss_key": ep.get("video_id"),
                    "num": i + 1,
                }
            )
        if rows:
            return rows
    return [
        {
            "video_oss_key": task.get("native_video_id"),
            "srt_oss_key": task.get("native_srt_id"),
            "negative_oss_key": task.get("native_video_id"),
            "num": 1,
        }
    ]


def _build_body(step: str, task: dict) -> TriggerResult | dict[str, Any]:
    """Construct the upstream POST body for a single step. Returns either
    a body dict on success or a TriggerResult(success=False) when a
    required field is missing. 1:1 port of sync/route.ts:132-222."""

    if step == "subtitle_extract":
        if not task.get("raw_video_id"):
            return _missing("raw_video_id")
        return {
            "file_id": [task["raw_video_id"]],
            "mode": 2,
            "language": "Auto-Detect",
            "subtitle_position": "auto",
        }

    if step == "subtitle_removal":
        if not task.get("raw_video_id"):
            return _missing("raw_video_id")
        return {
            "file_ids": [task["raw_video_id"]],
            "mode": task.get("removal_mode") or "standard",
        }

    if step == "popular_learning":
        return {
            "video_srt_path": task.get("learning_srt_id") or task.get("native_srt_id"),
            "narrator_type": task.get("narrator_type"),
            "model_version": task.get("model_version"),
        }

    if step == "generate_writing":
        steps = task.get("steps") or {}
        popular = steps.get("popular_learning") or {}
        popular_result = popular.get("result") or {}
        learning_model_id = (
            popular_result.get("learning_model_id") or task.get("existing_model_id")
        )
        if not learning_model_id:
            return _missing("learning_model_id")
        return {
            "learning_model_id": learning_model_id,
            "episodes_data": _build_episodes_data(task),
            "playlet_name": task.get("playlet_name"),
            "playlet_num": "1",
            "target_platform": task.get("target_platform"),
            "task_count": task.get("task_count"),
            "target_character_name": task.get("target_character_name") or "",
            "refine_srt_gaps": "1" if task.get("refine_gaps") else "0",
            "story_info": task.get("story_info") or "",
            "vendor_requirements": task.get("vendor_requirements")
            or "投放在短视频平台，吸引 18 - 35 岁的年轻用户观看",
        }

    if step == "fast_generate_writing":
        narrator_label = task.get("narrator_type_label") or ""
        narrator_type = task.get("narrator_type") or ""
        is_first_person = (
            "第一人称" in narrator_label or "first_person" in narrator_type
        )
        target_mode = 3 if narrator_label == "短剧" else (task.get("writing_type") or 2)
        body: dict[str, Any] = {
            "target_mode": target_mode,
            "playlet_name": task.get("playlet_name") or "",
            "episodes_data": _build_episodes_data(task),
            "confirmed_movie_json": (
                "" if narrator_label == "短剧" else (task.get("confirmed_movie_json") or "")
            ),
            "model": task.get("writing_model") or "flash",
            "language": task.get("writing_language") or "中文",
            "perspective": "first_person" if is_first_person else "third_person",
            "target_character_name": task.get("target_character_name") or "",
            "vendor_requirements": task.get("vendor_requirements")
            or "投放在短视频平台，吸引 18 - 35 岁的年轻用户观看",
        }
        if task.get("use_existing_model") and task.get("existing_model_id"):
            body["learning_model_id"] = task["existing_model_id"]
        elif not task.get("use_existing_model") and task.get("learning_srt_id"):
            body["learning_srt"] = task["learning_srt_id"]
        return body

    if step == "clip_data":
        steps = task.get("steps") or {}
        gen = (steps.get("generate_writing") or {}).get("result") or {}
        task_int_id = gen.get("task_int_id")
        if task_int_id is None:
            raw_first = (
                ((gen.get("raw_results") or {}).get("tasks") or [{}])[0]
                if isinstance(gen.get("raw_results"), dict)
                else {}
            )
            task_int_id = raw_first.get("id") if isinstance(raw_first, dict) else None
        order_num = gen.get("order_num") or gen.get("writing_order_num")
        if not task_int_id and not order_num:
            return _missing("generate_writing task int id")
        if not task.get("dubing_id"):
            return _missing("dubing_id")
        body: dict[str, Any] = {
            "bgm": task.get("bgm_id") or "NO_BGM",
            "dubbing": task["dubing_id"],
            "dubbing_type": "default",
        }
        if task_int_id is not None:
            body["generate_task_id"] = str(task_int_id)
        if order_num:
            body["order_num"] = order_num
        return body

    if step == "generate_fast_writing_clip_data":
        steps = task.get("steps") or {}
        fast = (steps.get("fast_generate_writing") or {}).get("result") or {}
        raw_results = fast.get("raw_results") if isinstance(fast, dict) else None
        raw_results = raw_results if isinstance(raw_results, dict) else {}
        order_info = raw_results.get("order_info") or {}
        if not isinstance(order_info, dict):
            order_info = {}

        task_id_val = fast.get("writing_task_id") or order_info.get("task_id") or ""
        task_id_str = str(task_id_val) if task_id_val else ""

        file_ids = raw_results.get("file_ids") if isinstance(raw_results, dict) else None
        first_file_id = (
            file_ids[0] if isinstance(file_ids, list) and file_ids else None
        )
        file_id_val = fast.get("file_id") or first_file_id or ""
        file_id_str = str(file_id_val) if file_id_val else ""

        if not file_id_str:
            tasks_list = raw_results.get("tasks") if isinstance(raw_results, dict) else None
            first_task = (
                tasks_list[0] if isinstance(tasks_list, list) and tasks_list else None
            )
            task_result = (
                first_task.get("task_result") if isinstance(first_task, dict) else None
            )
            if isinstance(task_result, str) and task_result.strip():
                try:
                    parsed = json.loads(task_result)
                    if isinstance(parsed, dict):
                        file_id_str = str(
                            parsed.get("file_id")
                            or ((parsed.get("data") or {}).get("file_id") if isinstance(parsed.get("data"), dict) else "")
                            or ""
                        )
                except (ValueError, TypeError):
                    file_id_str = ""

        if not task_id_str:
            return _missing("fast_generate_writing task_id")
        if not file_id_str:
            return _missing("fast_generate_writing file_id")
        if not task.get("dubing_id"):
            return _missing("dubing_id")
        return {
            "task_id": task_id_str,
            "file_id": file_id_str,
            "playlet_name": task.get("playlet_name") or "",
            "bgm": task.get("bgm_id") or "NO_BGM",
            "dubbing": task["dubing_id"],
            "dubbing_type": "普通话",
            "subtitle_style": {},
            "custom_cover": [],
            "episodes_data": _build_episodes_data(task),
        }

    if step == "video_composing":
        steps = task.get("steps") or {}
        clip = (steps.get("clip_data") or {}).get("result") or (
            (steps.get("generate_fast_writing_clip_data") or {}).get("result") or {}
        )
        order_num = clip.get("task_order_num") if isinstance(clip, dict) else None
        if not order_num:
            return TriggerResult(success=False, error="找不到剪辑步骤 task_order_num")
        return {"order_num": order_num}

    if step == "subsync":
        # Original sync/route.ts never triggers subsync from the auto-advance
        # path (it's only inserted between subtitle_removal and writing when
        # enable_subsync is true; the step that *triggers* subsync isn't
        # listed in triggerNextStep). Reject here so a future change to
        # resolve_next_step doesn't silently no-op.
        return TriggerResult(
            success=False, error="subsync 步骤当前不支持 orchestrator 触发"
        )

    return TriggerResult(success=False, error=f"未知步骤: {step}")


def _extract_task_id(upstream_resp: Any) -> str | None:
    """Pull `data.task_id` from an upstream create-* response. Upstream
    wraps in `{code, message, data: {task_id: ...}}`. Fall back to a
    top-level `task_id` for any upstream that doesn't wrap (defensive
    only — current commentary upstreams all wrap)."""
    if not isinstance(upstream_resp, dict):
        return None
    data = upstream_resp.get("data")
    if isinstance(data, dict) and data.get("task_id"):
        val = data["task_id"]
        return str(val) if val is not None else None
    top = upstream_resp.get("task_id")
    return str(top) if top else None


def trigger_next_step(
    next_step: str,
    task: dict,
    *,
    timeout_seconds: float = DEFAULT_UPSTREAM_TIMEOUT_SECONDS,
) -> TriggerResult:
    """Construct + POST the upstream create-* call for `next_step`.

    Errors are flattened into TriggerResult.error so the caller
    (advance.py) can persist a single failure string without worrying
    about exception types. Network / upstream errors are caught;
    programmer errors (KeyError on `task` etc.) are NOT caught — those
    indicate a state-machine bug and should surface in logs.
    """
    upstream_path = _UPSTREAM_PATHS.get(next_step)
    if upstream_path is None:
        return TriggerResult(success=False, error=f"未知步骤: {next_step}")

    body_or_err = _build_body(next_step, task)
    if isinstance(body_or_err, TriggerResult):
        return body_or_err
    body = body_or_err

    try:
        resp = proxy_narrator_upstream(
            upstream_path,
            method="POST",
            body=body,
            timeout_seconds=timeout_seconds,
        )
    except UpstreamNarratorError as exc:
        return TriggerResult(success=False, error=f"{exc.code}: {exc.message}")

    task_id = _extract_task_id(resp)
    if not task_id:
        return TriggerResult(
            success=False, error="upstream 未返回 task_id"
        )
    return TriggerResult(success=True, task_id=task_id)
