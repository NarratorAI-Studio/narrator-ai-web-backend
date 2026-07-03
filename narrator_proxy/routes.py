"""Route registry for group G (subtitle tools) + group E (commentary)
upstream proxy routes (the implementation requirement).

Each entry maps a backend URL pattern to its upstream counterpart.
`path_vars` lists the Flask URL variable names that appear in both
`backend_path` (as `<var>`) and `upstream_path` (as `{var}`).

Routes without path vars can be registered via the `add_url_rule` loop in
server.py. Routes with path vars need individual Flask route decorators
(or equivalent) because Flask's `add_url_rule` requires a unique view
function signature per variable name.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class NarratorProxyRoute:
    backend_path: str          # Flask route pattern, e.g. "/narrator/ocr-extraction/query/<task_id>"
    upstream_path: str         # upstream path template, e.g. "/v2/task/ocr_extraction/query/{task_id}"
    method: str                # "GET" or "POST"
    endpoint_label: str        # Prometheus / Flask endpoint name
    query_params: tuple[str, ...] = field(default_factory=tuple)  # GET param allowlist
    timeout_seconds: float = 60.0  # upstream call timeout


COMMENTARY_CREATE_TIMEOUT_SECONDS = 60.0


# ── group G: subtitle tools ─────────────────────────────────────────────────

SUBTITLE_STATIC_ROUTES: tuple[NarratorProxyRoute, ...] = (
    NarratorProxyRoute(
        backend_path="/narrator/ocr-extraction/create",
        upstream_path="/v2/task/ocr_extraction/create",
        method="POST",
        endpoint_label="narrator-ocr-extraction-create",
    ),
    NarratorProxyRoute(
        backend_path="/narrator/subtitle-removal/create",
        upstream_path="/v2/task/subtitle_removal/create",
        method="POST",
        endpoint_label="narrator-subtitle-removal-create",
    ),
)

# Dynamic routes (contain <task_id>) are registered individually in server.py.
OCR_QUERY_ROUTE = NarratorProxyRoute(
    backend_path="/narrator/ocr-extraction/query/<task_id>",
    upstream_path="/v2/task/ocr_extraction/query/{task_id}",
    method="GET",
    endpoint_label="narrator-ocr-extraction-query",
)

SUBTITLE_REMOVAL_QUERY_ROUTE = NarratorProxyRoute(
    backend_path="/narrator/subtitle-removal/query/<task_id>",
    upstream_path="/v2/task/subtitle_removal/query/{task_id}",
    method="GET",
    endpoint_label="narrator-subtitle-removal-query",
)

# ── group E: commentary + content ───────────────────────────────────────────

COMMENTARY_STATIC_ROUTES: tuple[NarratorProxyRoute, ...] = (
    NarratorProxyRoute(
        backend_path="/narrator/commentary/list",
        upstream_path="/v2/task/commentary/list",
        method="GET",
        endpoint_label="narrator-commentary-list",
        query_params=("page", "limit", "status", "task_type", "category"),
    ),
    # /narrator/commentary/writing supports both GET + POST but with
    # different upstream paths. It is registered as a combined route in
    # server.py (methods=["GET","POST"]) and dispatched by request.method,
    # so it is NOT included in STATIC_ROUTES to avoid duplicate registration.

    NarratorProxyRoute(
        backend_path="/narrator/commentary/consume-budget",
        upstream_path="/v2/task/commentary/consume_budget",
        method="POST",
        endpoint_label="narrator-commentary-consume-budget",
    ),
    NarratorProxyRoute(
        backend_path="/narrator/commentary/search-media",
        upstream_path="/v2/task/commentary/search_media_information",
        method="GET",
        endpoint_label="narrator-commentary-search-media",
        query_params=("query",),
        timeout_seconds=90.0,
    ),
    NarratorProxyRoute(
        backend_path="/narrator/movie-sucai",
        upstream_path="/v2/res/movie-sucai",
        method="GET",
        endpoint_label="narrator-movie-sucai",
        query_params=("page", "page_size", "size", "name"),
    ),
    NarratorProxyRoute(
        backend_path="/narrator/commentary/material-verification",
        upstream_path="/v2/task/commentary/material_verification",
        method="POST",
        endpoint_label="narrator-commentary-material-verification",
    ),
    # group B — creation chain (the implementation requirement). Seven POST routes that drive the
    # commentary task wizard: popular-learning → optional subsync → either
    # generate-writing→clip-data (long-form) or fast-writing→fast-clip-data
    # (short-form), then video-composing.
    NarratorProxyRoute(
        backend_path="/narrator/commentary/create-popular-learning",
        upstream_path="/v2/task/commentary/create_popular_learning",
        method="POST",
        endpoint_label="narrator-commentary-create-popular-learning",
        timeout_seconds=COMMENTARY_CREATE_TIMEOUT_SECONDS,
    ),
    NarratorProxyRoute(
        backend_path="/narrator/commentary/create-subsync",
        upstream_path="/v2/task/commentary/create_subsync_task",
        method="POST",
        endpoint_label="narrator-commentary-create-subsync",
        timeout_seconds=COMMENTARY_CREATE_TIMEOUT_SECONDS,
    ),
    NarratorProxyRoute(
        backend_path="/narrator/commentary/create-generate-writing",
        upstream_path="/v2/task/commentary/create_generate_writing",
        method="POST",
        endpoint_label="narrator-commentary-create-generate-writing",
        timeout_seconds=COMMENTARY_CREATE_TIMEOUT_SECONDS,
    ),
    NarratorProxyRoute(
        backend_path="/narrator/commentary/create-clip-data",
        upstream_path="/v2/task/commentary/create_generate_clip_data",
        method="POST",
        endpoint_label="narrator-commentary-create-clip-data",
        timeout_seconds=COMMENTARY_CREATE_TIMEOUT_SECONDS,
    ),
    NarratorProxyRoute(
        backend_path="/narrator/commentary/create-fast-writing",
        upstream_path="/v2/task/commentary/create_fast_generate_writing",
        method="POST",
        endpoint_label="narrator-commentary-create-fast-writing",
        timeout_seconds=COMMENTARY_CREATE_TIMEOUT_SECONDS,
    ),
    NarratorProxyRoute(
        backend_path="/narrator/commentary/create-fast-writing-clip-data",
        upstream_path="/v2/task/commentary/create_generate_fast_writing_clip_data",
        method="POST",
        endpoint_label="narrator-commentary-create-fast-writing-clip-data",
        timeout_seconds=COMMENTARY_CREATE_TIMEOUT_SECONDS,
    ),
    NarratorProxyRoute(
        backend_path="/narrator/commentary/create-video-composing",
        upstream_path="/v2/task/commentary/create_video_composing",
        method="POST",
        endpoint_label="narrator-commentary-create-video-composing",
        timeout_seconds=COMMENTARY_CREATE_TIMEOUT_SECONDS,
    ),
)

# Dynamic commentary route
COMMENTARY_QUERY_ROUTE = NarratorProxyRoute(
    backend_path="/narrator/commentary/query/<task_id>",
    upstream_path="/v2/task/commentary/query/{task_id}",
    method="GET",
    endpoint_label="narrator-commentary-query",
)

# All static routes (no path vars) that can be registered via loop
STATIC_ROUTES: tuple[NarratorProxyRoute, ...] = (
    *SUBTITLE_STATIC_ROUTES,
    *COMMENTARY_STATIC_ROUTES,
)

# All dynamic routes (have path vars), registered individually
DYNAMIC_ROUTES: tuple[NarratorProxyRoute, ...] = (
    OCR_QUERY_ROUTE,
    SUBTITLE_REMOVAL_QUERY_ROUTE,
    COMMENTARY_QUERY_ROUTE,
)

# The writing route serves both GET + POST on the same path but with
# different upstream targets. Exposed as constants for server.py's
# combined handler and for test assertions.
COMMENTARY_WRITING_GET = NarratorProxyRoute(
    backend_path="/narrator/commentary/writing",
    upstream_path="/v2/task/commentary/get_generate_writed",
    method="GET",
    endpoint_label="narrator-commentary-writing-get",
    query_params=("task_id", "file_id"),
)
COMMENTARY_WRITING_POST = NarratorProxyRoute(
    backend_path="/narrator/commentary/writing",
    upstream_path="/v2/task/commentary/save_generate_writed",
    method="POST",
    endpoint_label="narrator-commentary-writing-post",
)
