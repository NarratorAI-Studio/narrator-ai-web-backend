"""Registry of narrator-metadata endpoints exposed by backend (the implementation requirement).

Each entry binds a backend route path to its upstream API
path plus the allowlist of query parameters that may be forwarded.

Keeping this in a single tuple makes the wrap-pattern auditable at a
glance: anyone adding a new metadata wrapper edits one place rather than
adding both a route decorator and a separate handler.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NarratorMetadataRoute:
    backend_path: str  # path served by this backend, e.g. "/narrator/types"
    upstream_path: str  # upstream path, e.g. "/v1/narrator/types"
    supported_params: tuple[str, ...]  # query keys forwarded upstream
    endpoint_label: str  # metrics label (Prometheus REQUEST_COUNT etc.)


ROUTES: tuple[NarratorMetadataRoute, ...] = (
    NarratorMetadataRoute(
        backend_path="/narrator/narrator-types",
        upstream_path="/v1/task/get_narrator_types",
        supported_params=("terminal_type",),
        endpoint_label="narrator-narrator-types",
    ),
    NarratorMetadataRoute(
        backend_path="/narrator/types",
        upstream_path="/v1/narrator/types",
        supported_params=(),
        endpoint_label="narrator-types",
    ),
    NarratorMetadataRoute(
        backend_path="/narrator/models",
        upstream_path="/v1/narrator/models",
        supported_params=(),
        endpoint_label="narrator-models",
    ),
    NarratorMetadataRoute(
        backend_path="/narrator/model-versions",
        upstream_path="/v1/task/get_model_versions",
        supported_params=("terminal_type",),
        endpoint_label="narrator-model-versions",
    ),
    NarratorMetadataRoute(
        backend_path="/narrator/bgm",
        upstream_path="/v1/narrator/bgm",
        supported_params=(),
        endpoint_label="narrator-bgm",
    ),
    NarratorMetadataRoute(
        backend_path="/narrator/bgm-list",
        upstream_path="/v2/res/movie-bgm",
        supported_params=("page", "size"),
        endpoint_label="narrator-bgm-list",
    ),
    NarratorMetadataRoute(
        backend_path="/narrator/dubbing-list",
        upstream_path="/v2/res/movie-dubbing",
        supported_params=("page", "size"),
        endpoint_label="narrator-dubbing-list",
    ),
    NarratorMetadataRoute(
        backend_path="/narrator/template-meta",
        upstream_path="/v2/res/baokuan/meta",
        supported_params=(),
        endpoint_label="narrator-template-meta",
    ),
)
