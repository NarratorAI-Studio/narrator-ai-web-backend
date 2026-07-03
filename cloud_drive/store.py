"""Persistence and quota accounting for Web personal cloud-drive files."""

from __future__ import annotations

import math
import re
import secrets
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.engine import Connection

from cloud_drive.schema import DEFAULT_USER_CLOUD_QUOTA_BYTES
from db.tables import user_cloud_files, users


ACTIVE_STATUSES = (
    "reserved",
    "completed",
    "delete_pending",
    "transfer_pending",
    "transfer_running",
    "transfer_completed",
)
SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")


class CloudDriveStoreError(Exception):
    def __init__(
        self,
        http_status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict | None = None,
    ):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def make_reservation_id() -> str:
    return f"cdr_{secrets.token_urlsafe(18)}"


def suffix_for(file_name: str) -> str:
    if "." not in file_name:
        return ""
    return file_name.rsplit(".", 1)[-1].lower()


# Upstream cloud drive shares one namespace across all narrator-ai users
# because Web authenticates upstream with the master OPEN_FASTAPI_APP_KEY.
# Upstream appends `(N)` against globally-existing names, which leaks into
# each user's private file list . Store fan-out children under a
# per-user deduped name instead.
#
# Match upstream's convention only: `<base>(N)<.ext>` with no space before
# the paren. User-typed names like `movie (1).mp4` are left intact.
_UPSTREAM_DUP_SUFFIX_RE = re.compile(r"^(?P<base>.*?)\((?P<n>\d+)\)(?P<ext>\.[^.]+)?$")


def strip_upstream_dup_suffix(file_name: str) -> str:
    """Remove upstream cloud-drive's `(N)` dedup tag, if present.

    Examples:
        ``"name.mp4"``     -> ``"name.mp4"``
        ``"name(1).mp4"``  -> ``"name.mp4"``
        ``"name (1).mp4"`` -> ``"name (1).mp4"``
        ``"name(2)"``      -> ``"name"``
    """
    m = _UPSTREAM_DUP_SUFFIX_RE.match(file_name)
    if not m:
        return file_name
    base = m.group("base")
    # Reject space-before-paren: that is part of the user's name, not an
    # upstream dedup tag.
    if base.endswith(" "):
        return file_name
    if not base:
        return file_name
    ext = m.group("ext") or ""
    return f"{base}{ext}"


def compute_per_user_dedup_name(
    conn: Connection,
    *,
    user_id: int,
    base_name: str,
    also_taken: Optional[set[str]] = None,
    exclude_reservation_ids: Optional[set[str]] = None,
) -> str:
    """Pick a name that does not collide with this user's existing
    cloud-drive files. Returns ``base_name`` if unused; otherwise
    appends ``(N)`` for the lowest available N >= 1.

    Per-user scope means test1 and test2 each start at the base name
    even if upstream has globally collided . Deleted rows do
    not block a name (we exclude ``status='deleted'``).

    ``also_taken`` lets callers reserve names assigned earlier in the
    same batch (e.g. fan-out siblings inserted in one transaction)
    that aren't yet visible to a SELECT.

    ``exclude_reservation_ids`` lets fan-out ignore the unsettled parent
    reservation whose name is about to be replaced by child rows.
    """
    base_name = base_name.strip()
    if not base_name:
        return base_name
    if "." in base_name:
        head, ext = base_name.rsplit(".", 1)
        ext_with_dot = f".{ext}"
    else:
        head = base_name
        ext_with_dot = ""

    # Pull every name the user already has that could collide. Cheaper
    # than counting per candidate; the per-user set is small.
    like_pattern = f"{head}%{ext_with_dot}" if ext_with_dot else f"{head}%"
    conditions = [
        user_cloud_files.c.user_id == user_id,
        user_cloud_files.c.status != "deleted",
        or_(
            user_cloud_files.c.file_name == base_name,
            user_cloud_files.c.file_name.like(like_pattern),
        ),
    ]
    if exclude_reservation_ids:
        conditions.append(
            user_cloud_files.c.reservation_id.notin_(exclude_reservation_ids)
        )
    rows = conn.execute(
        select(user_cloud_files.c.file_name).where(and_(*conditions))
    ).all()
    existing = {row[0] for row in rows}
    if also_taken:
        existing |= also_taken
    if base_name not in existing:
        return base_name
    for n in range(1, 10_000):
        candidate = f"{head}({n}){ext_with_dot}"
        if candidate not in existing:
            return candidate
    # Pathological fallback; should never happen in practice.
    return base_name


def category_for_suffix(suffix: str) -> int:
    if suffix in {"mp4", "mov", "avi", "mkv", "flv"}:
        return 1
    if suffix in {"mp3", "wav", "aac", "m4a"}:
        return 2
    if suffix in {"srt", "vtt", "ass", "txt", "json"}:
        return 3
    if suffix in {"jpg", "jpeg", "png", "webp"}:
        return 4
    return 0


def normalize_sha256(value: Optional[str]) -> Optional[str]:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise CloudDriveStoreError(
            400,
            "BAD_REQUEST",
            "srt_file_hash must be a 64-character sha256 hex string.",
            retryable=False,
        )
    return value.lower()


def _quota_value(raw) -> int:
    if raw is None:
        return DEFAULT_USER_CLOUD_QUOTA_BYTES
    return int(raw)


def used_bytes(conn: Connection, *, user_id: int) -> int:
    total = conn.execute(
        select(func.coalesce(func.sum(user_cloud_files.c.file_size), 0)).where(
            and_(
                user_cloud_files.c.user_id == user_id,
                user_cloud_files.c.status.in_(ACTIVE_STATUSES),
            )
        )
    ).scalar_one()
    return int(total or 0)


def _locked_user_quota(conn: Connection, *, user_id: int) -> int:
    user_row = (
        conn.execute(
            select(users.c.cloud_drive_quota_bytes)
            .where(users.c.id == user_id)
            .with_for_update()
        )
        .mappings()
        .first()
    )
    if user_row is None:
        raise CloudDriveStoreError(
            401,
            "WEB_APP_KEY_UNKNOWN",
            "X-Web-App-Key is not recognized.",
            retryable=False,
        )
    return _quota_value(user_row["cloud_drive_quota_bytes"])


def ensure_quota_available(
    conn: Connection,
    *,
    user_id: int,
    file_size: int,
) -> None:
    # Quota-mutating paths serialize on the users row before inserting active rows.
    quota = _locked_user_quota(conn, user_id=user_id)
    current = used_bytes(conn, user_id=user_id)
    if current + file_size > quota:
        raise CloudDriveStoreError(
            409,
            "CLOUD_DRIVE_QUOTA_EXCEEDED",
            "空间不足，请联系部署管理员",
            retryable=False,
            details={
                "used_size": current,
                "max_size": quota,
                "requested_size": file_size,
            },
        )


def ensure_callback_quota(
    conn: Connection,
    *,
    user_id: int,
    reserved_size: int,
    final_size: int,
) -> None:
    if final_size <= reserved_size:
        return
    quota = _locked_user_quota(conn, user_id=user_id)
    current = used_bytes(conn, user_id=user_id)
    projected = current - reserved_size + final_size
    if projected > quota:
        raise CloudDriveStoreError(
            409,
            "CLOUD_DRIVE_QUOTA_EXCEEDED",
            "空间不足，请联系部署管理员",
            retryable=False,
            details={
                "used_size": current,
                "max_size": quota,
                "requested_size": final_size,
            },
        )


def get_storage_usage(conn: Connection, *, user_id: int) -> dict:
    row = conn.execute(
        select(users.c.cloud_drive_quota_bytes).where(users.c.id == user_id)
    ).first()
    quota = _quota_value(row.cloud_drive_quota_bytes if row else None)
    used = used_bytes(conn, user_id=user_id)
    count = conn.execute(
        select(func.count())
        .select_from(user_cloud_files)
        .where(
            and_(
                user_cloud_files.c.user_id == user_id,
                user_cloud_files.c.status.in_(ACTIVE_STATUSES),
                ~and_(
                    user_cloud_files.c.source == "transfer",
                    user_cloud_files.c.parent_reservation_id.is_(None),
                    user_cloud_files.c.settled_at.is_not(None),
                ),
            )
        )
    ).scalar_one()
    return {
        "used_size": used,
        "max_size": quota,
        "file_count": int(count or 0),
        "usage_percentage": 0 if quota <= 0 else used / quota * 100,
    }


def reserve_local_upload(
    conn: Connection,
    *,
    user_id: int,
    app_key: str,
    file_name: str,
    file_size: int,
    content_type: Optional[str],
) -> dict:
    if file_size <= 0:
        raise CloudDriveStoreError(
            400,
            "BAD_REQUEST",
            "file_size must be a positive integer.",
            retryable=False,
        )
    ensure_quota_available(conn, user_id=user_id, file_size=file_size)

    now = utcnow()
    suffix = suffix_for(file_name)
    reservation_id = make_reservation_id()
    conn.execute(
        user_cloud_files.insert().values(
            reservation_id=reservation_id,
            user_id=user_id,
            app_key=app_key,
            file_name=file_name,
            suffix=suffix,
            category=category_for_suffix(suffix),
            file_size=file_size,
            content_type=content_type,
            source="local_upload",
            status="reserved",
            progress=0,
            upstream_payload={},
            created_at=now,
            updated_at=now,
        )
    )
    return {"reservation_id": reservation_id}


def attach_upload_file_id(
    conn: Connection,
    *,
    reservation_id: str,
    file_id: str,
    object_key: Optional[str],
    upstream_payload: dict | list,
) -> None:
    conn.execute(
        user_cloud_files.update()
        .where(user_cloud_files.c.reservation_id == reservation_id)
        .values(
            file_id=file_id,
            object_key=object_key,
            upstream_payload=upstream_payload,
            updated_at=utcnow(),
        )
    )


def mark_reservation_failed(
    conn: Connection,
    *,
    reservation_id: str,
    upstream_payload: dict | list | None = None,
    error_message: str | None = None,
    error_code: str | None = None,
) -> None:
    payload: dict | list = upstream_payload if upstream_payload is not None else {}
    if isinstance(payload, dict) and (error_message or error_code):
        # Web API contract / regression coverage: persist error context inside the
        # existing JSONB column so the list response can surface it
        # without a schema migration.
        payload = {**payload}
        if error_message:
            payload["error_message"] = error_message
        if error_code:
            payload["error_code"] = error_code
    conn.execute(
        user_cloud_files.update()
        .where(user_cloud_files.c.reservation_id == reservation_id)
        .values(
            status="failed",
            upstream_payload=payload,
            updated_at=utcnow(),
        )
    )


def complete_upload_callback(
    conn: Connection,
    *,
    user_id: int,
    file_id: str,
    object_key: str,
    upload_status: str,
    srt_file_hash: Optional[str],
    upstream_payload: dict | list,
    callback_file_size: Optional[int] = None,
) -> dict | None:
    row = (
        conn.execute(
            select(
                user_cloud_files.c.id,
                user_cloud_files.c.source,
                user_cloud_files.c.status,
                user_cloud_files.c.file_name,
                user_cloud_files.c.suffix,
                user_cloud_files.c.file_size,
                users.c.cloud_drive_quota_bytes,
            )
            .select_from(
                user_cloud_files.join(users, user_cloud_files.c.user_id == users.c.id)
            )
            .where(
                and_(
                    user_cloud_files.c.user_id == user_id,
                    user_cloud_files.c.file_id == file_id,
                    user_cloud_files.c.source == "local_upload",
                    user_cloud_files.c.status == "reserved",
                )
            )
            .with_for_update()
        )
        .mappings()
        .first()
    )
    if row is None:
        return None

    now = utcnow()
    file_name = row["file_name"]
    suffix = row["suffix"] or suffix_for(file_name)
    reserved_size = int(row["file_size"] or 0)
    final_size = reserved_size
    if callback_file_size is not None:
        callback_size = int(callback_file_size)
        if callback_size < 0:
            raise CloudDriveStoreError(
                400,
                "BAD_REQUEST",
                "file_size must be a non-negative integer.",
                retryable=False,
            )
        final_size = max(reserved_size, callback_size)

    if upload_status != "success":
        conn.execute(
            user_cloud_files.update()
            .where(user_cloud_files.c.id == row["id"])
            .values(
                status="failed",
                object_key=object_key,
                suffix=suffix,
                category=category_for_suffix(suffix),
                file_size=reserved_size,
                upstream_payload=upstream_payload,
                updated_at=now,
            )
        )
        return {"file_id": file_id, "upload_status": upload_status}

    normalized_hash = normalize_sha256(srt_file_hash)
    if suffix == "srt" and not normalized_hash:
        raise CloudDriveStoreError(
            400,
            "BAD_REQUEST",
            "srt_file_hash is required for local SRT uploads.",
            retryable=False,
        )

    ensure_callback_quota(
        conn,
        user_id=user_id,
        reserved_size=reserved_size,
        final_size=final_size,
    )

    conn.execute(
        user_cloud_files.update()
        .where(user_cloud_files.c.id == row["id"])
        .values(
            status="completed",
            object_key=object_key,
            suffix=suffix,
            category=category_for_suffix(suffix),
            file_size=final_size,
            progress=100,
            srt_file_hash=normalized_hash,
            upstream_payload=upstream_payload,
            completed_at=now,
            updated_at=now,
        )
    )
    return get_file(conn, user_id=user_id, file_id=file_id)


def get_local_upload_reservation(
    conn: Connection, *, user_id: int, file_id: str
) -> dict | None:
    row = (
        conn.execute(
            select(
                user_cloud_files.c.id,
                user_cloud_files.c.file_id,
                user_cloud_files.c.object_key,
                user_cloud_files.c.file_name,
                user_cloud_files.c.suffix,
                user_cloud_files.c.file_size,
                user_cloud_files.c.status,
                user_cloud_files.c.source,
            ).where(
                and_(
                    user_cloud_files.c.user_id == user_id,
                    user_cloud_files.c.file_id == file_id,
                    user_cloud_files.c.source == "local_upload",
                    user_cloud_files.c.status == "reserved",
                )
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


def get_file(conn: Connection, *, user_id: int, file_id: str) -> dict | None:
    row = (
        conn.execute(
            select(user_cloud_files).where(
                and_(
                    user_cloud_files.c.user_id == user_id,
                    user_cloud_files.c.file_id == file_id,
                    user_cloud_files.c.status.in_(("completed", "transfer_completed")),
                    user_cloud_files.c.file_id.is_not(None),
                )
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return row_to_cloud_file(row)


def list_files(
    conn: Connection,
    *,
    user_id: int,
    page: int,
    page_size: int,
    order_by: str,
    order: str,
    search: str,
) -> dict:
    filters = [
        user_cloud_files.c.user_id == user_id,
        user_cloud_files.c.status.in_(("completed", "transfer_completed")),
        user_cloud_files.c.file_id.is_not(None),
    ]
    if search:
        filters.append(user_cloud_files.c.file_name.ilike(f"%{search}%"))

    total = conn.execute(
        select(func.count()).select_from(user_cloud_files).where(and_(*filters))
    ).scalar_one()

    sortable = {
        "file_size": user_cloud_files.c.file_size,
        "completed_time": user_cloud_files.c.completed_at,
        "created_at": user_cloud_files.c.created_at,
    }
    order_column = sortable.get(order_by, user_cloud_files.c.created_at)
    order_clause = order_column.asc() if order == "asc" else order_column.desc()

    rows = (
        conn.execute(
            select(user_cloud_files)
            .where(and_(*filters))
            .order_by(order_clause)
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        .mappings()
        .all()
    )
    return {
        "total": int(total or 0),
        "page": page,
        "page_size": page_size,
        "total_pages": math.ceil((total or 0) / page_size) if page_size else 0,
        "items": [row_to_cloud_file(row) for row in rows],
    }


def soft_delete_file(conn: Connection, *, user_id: int, file_id: str) -> dict | None:
    row = (
        conn.execute(
            select(
                user_cloud_files.c.id,
                user_cloud_files.c.file_id,
                user_cloud_files.c.reservation_id,
                user_cloud_files.c.status,
            )
            .where(
                and_(
                    user_cloud_files.c.user_id == user_id,
                    or_(
                        user_cloud_files.c.file_id == file_id,
                        user_cloud_files.c.reservation_id == file_id,
                    ),
                    user_cloud_files.c.status != "deleted",
                )
            )
            .with_for_update()
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    identifier = row["file_id"] or row["reservation_id"]
    if row["status"] == "delete_pending":
        return {
            "file_id": identifier,
            "upstream_file_id": row["file_id"],
            "deleted": True,
            "previous_status": "delete_pending",
            "already_pending": True,
        }
    now = utcnow()
    conn.execute(
        user_cloud_files.update()
        .where(user_cloud_files.c.id == row["id"])
        .values(status="delete_pending", deleted_at=None, updated_at=now)
    )
    return {
        "file_id": identifier,
        "upstream_file_id": row["file_id"],
        "deleted": True,
        "previous_status": row["status"],
        "already_pending": False,
    }


def finalize_delete_file(conn: Connection, *, user_id: int, file_id: str) -> None:
    now = utcnow()
    conn.execute(
        user_cloud_files.update()
        .where(
            and_(
                user_cloud_files.c.user_id == user_id,
                user_cloud_files.c.file_id == file_id,
                user_cloud_files.c.status == "delete_pending",
            )
        )
        .values(status="deleted", deleted_at=now, updated_at=now)
    )


def finalize_delete_files(
    conn: Connection, *, user_id: int, file_ids: list[str]
) -> None:
    if not file_ids:
        return
    now = utcnow()
    conn.execute(
        user_cloud_files.update()
        .where(
            and_(
                user_cloud_files.c.user_id == user_id,
                or_(
                    user_cloud_files.c.file_id.in_(file_ids),
                    user_cloud_files.c.reservation_id.in_(file_ids),
                ),
                user_cloud_files.c.status == "delete_pending",
            )
        )
        .values(status="deleted", deleted_at=now, updated_at=now)
    )


def restore_file_status(
    conn: Connection,
    *,
    user_id: int,
    file_id: str,
    status: str,
) -> None:
    conn.execute(
        user_cloud_files.update()
        .where(
            and_(
                user_cloud_files.c.user_id == user_id,
                or_(
                    user_cloud_files.c.file_id == file_id,
                    user_cloud_files.c.reservation_id == file_id,
                ),
                user_cloud_files.c.status.in_(("delete_pending", "deleted")),
            )
        )
        .values(status=status, deleted_at=None, updated_at=utcnow())
    )


def create_transfer_record(
    conn: Connection,
    *,
    user_id: int,
    app_key: str,
    link: str,
    file_id: Optional[str],
    file_name: Optional[str],
    file_size: int,
    upstream_status: Optional[int],
    progress: int,
    upstream_payload: dict | list,
) -> dict:
    file_size = int(file_size or 0)
    if file_size < 0:
        raise CloudDriveStoreError(
            400,
            "BAD_REQUEST",
            "file_size must be a non-negative integer.",
            retryable=False,
        )
    if file_size > 0:
        ensure_quota_available(conn, user_id=user_id, file_size=file_size)

    now = utcnow()
    name = file_name or file_id or "transfer"
    suffix = suffix_for(name)
    reservation_id = make_reservation_id()
    values = {
        "reservation_id": reservation_id,
        "user_id": user_id,
        "app_key": app_key,
        "file_id": file_id,
        "file_name": name,
        "suffix": suffix,
        "category": category_for_suffix(suffix),
        "file_size": file_size,
        "content_type": None,
        "source": "transfer",
        "status": "transfer_pending",
        "upstream_status": upstream_status,
        "progress": max(0, min(int(progress or 0), 100)),
        "upstream_payload": upstream_payload,
        "created_at": now,
        "updated_at": now,
    }
    conn.execute(user_cloud_files.insert().values(**values))
    return {"reservation_id": reservation_id, "file_id": file_id, "link": link}


def attach_link_upload_id(
    conn: Connection,
    *,
    user_id: int,
    reservation_id: str,
    upload_id: str,
    file_name: Optional[str],
    file_size: int,
    upstream_status: Optional[int],
    progress: int,
    upstream_payload: dict | list,
) -> dict | None:
    # Parent reservation gets the `upload_id` (e.g. `baidu-…`) here;
    # `file_id` stays NULL until the refresh fan-out resolves child
    # rows from the upstream filelist .
    row = (
        conn.execute(
            select(
                user_cloud_files.c.id,
                user_cloud_files.c.file_size,
            )
            .where(
                and_(
                    user_cloud_files.c.user_id == user_id,
                    user_cloud_files.c.reservation_id == reservation_id,
                    user_cloud_files.c.source == "transfer",
                    user_cloud_files.c.status != "deleted",
                )
            )
            .with_for_update()
        )
        .mappings()
        .first()
    )
    if row is None:
        return None

    reserved_size = int(row["file_size"] or 0)
    final_size = max(reserved_size, int(file_size or 0))
    ensure_callback_quota(
        conn,
        user_id=user_id,
        reserved_size=reserved_size,
        final_size=final_size,
    )

    name = file_name or "transfer"
    suffix = suffix_for(name)
    safe_progress = max(0, min(int(progress or 0), 100))
    now = utcnow()
    conn.execute(
        user_cloud_files.update()
        .where(user_cloud_files.c.id == row["id"])
        .values(
            upload_id=upload_id,
            file_name=name,
            suffix=suffix,
            category=category_for_suffix(suffix),
            file_size=final_size,
            upstream_status=upstream_status,
            progress=safe_progress,
            upstream_payload=upstream_payload,
            updated_at=now,
        )
    )
    return {"reservation_id": reservation_id, "upload_id": upload_id}


# Upstream status enum, confirmed in the implementation requirement:
#   0 init / 1 uploading / 2 done / 3 failed / 4 deleted / 5 locked
# We treat {2, 3, 4} as terminal for settlement purposes — once every
# child in a filelist reaches one of these, the parent reservation can
# be settled and no more polling is needed (upstream commits to not
# adding further children to an existing upload_id).
TERMINAL_UPSTREAM_STATUSES = (2, 3, 4)


def list_unsettled_link_parents(conn: Connection, *, user_id: int) -> list[dict]:
    rows = conn.execute(
        select(
            user_cloud_files.c.reservation_id,
            user_cloud_files.c.upload_id,
            user_cloud_files.c.file_size,
            user_cloud_files.c.app_key,
        ).where(
            and_(
                user_cloud_files.c.user_id == user_id,
                user_cloud_files.c.source == "transfer",
                user_cloud_files.c.upload_id.is_not(None),
                user_cloud_files.c.parent_reservation_id.is_(None),
                user_cloud_files.c.settled_at.is_(None),
                user_cloud_files.c.status != "deleted",
            )
        )
    ).all()
    return [
        {
            "reservation_id": row.reservation_id,
            "upload_id": row.upload_id,
            "reserved_size": int(row.file_size or 0),
            "app_key": row.app_key,
        }
        for row in rows
    ]


def settle_link_parent_with_children(
    conn: Connection,
    *,
    user_id: int,
    app_key: str,
    parent_reservation_id: str,
    upload_id: str,
    reserved_parent_size: int,
    children: list[dict],
) -> dict:
    """Atomically materialize child rows + mark parent settled.

    `children` items are upstream filelist entries, each carrying
    `file_id` (32-hex), `file_name`, `file_size`, `upstream_status`,
    `progress`, plus the raw payload (under `upstream_payload`). Caller
    has already filtered to upload_id-matched, terminal-status rows.
    """
    # Re-lock parent to guard against a concurrent refresh.
    parent_row = (
        conn.execute(
            select(user_cloud_files.c.id, user_cloud_files.c.settled_at)
            .where(
                and_(
                    user_cloud_files.c.user_id == user_id,
                    user_cloud_files.c.reservation_id == parent_reservation_id,
                    user_cloud_files.c.source == "transfer",
                )
            )
            .with_for_update()
        )
        .mappings()
        .first()
    )
    if parent_row is None or parent_row["settled_at"] is not None:
        return {"already_settled": True, "inserted": 0}

    # Quota accounting at settlement: parent reservation held `reserved_parent_size`
    # bytes; children now claim their real per-file sizes. Net delta =
    # sum(child sizes) - reserved_parent_size (may be 0 or negative).
    success_children = [c for c in children if int(c.get("upstream_status") or 0) == 2]
    realized_total = sum(int(c.get("file_size") or 0) for c in success_children)
    if realized_total > reserved_parent_size:
        ensure_callback_quota(
            conn,
            user_id=user_id,
            reserved_size=reserved_parent_size,
            final_size=realized_total,
        )

    now = utcnow()
    inserted = 0
    # regression coverage: track names assigned within this batch so two children with
    # the same upstream base don't both get the same per-user name.
    batch_assigned: set[str] = set()
    for child in children:
        child_status_int = int(child.get("upstream_status") or 0)
        if child_status_int == 4:
            # Upstream-deleted children are excluded — the user doesn't
            # have them anyway. Their existence still contributes to
            # settlement (terminal status), so they participate in the
            # `all-terminal` decision in the caller.
            continue
        file_id = str(child["file_id"])
        raw_file_name = str(child.get("file_name") or file_id)
        # regression coverage: upstream shares one namespace across all narrator users,
        # so its `(N)` dedup suffix leaks per-user. Strip it and re-dedup
        # against this user's own files (and the in-batch siblings;
        # the freshly-inserted rows aren't yet visible to SELECT).
        stripped = strip_upstream_dup_suffix(raw_file_name)
        file_name = compute_per_user_dedup_name(
            conn,
            user_id=user_id,
            base_name=stripped,
            also_taken=batch_assigned,
            exclude_reservation_ids={parent_reservation_id},
        )
        batch_assigned.add(file_name)
        suffix = suffix_for(file_name)
        child_size = int(child.get("file_size") or 0)
        progress = max(0, min(int(child.get("progress") or 0), 100))
        if child_status_int == 2:
            row_status = "transfer_completed"
            progress = 100
            completed_at = now
        else:
            row_status = "failed"
            completed_at = None
        conn.execute(
            user_cloud_files.insert().values(
                reservation_id=make_reservation_id(),
                user_id=user_id,
                app_key=app_key,
                file_id=file_id,
                file_name=file_name,
                suffix=suffix,
                category=category_for_suffix(suffix),
                file_size=child_size,
                source="transfer",
                status=row_status,
                upstream_status=child_status_int,
                progress=progress,
                upstream_payload=child.get("upstream_payload") or {},
                upload_id=upload_id,
                parent_reservation_id=parent_reservation_id,
                created_at=now,
                updated_at=now,
                completed_at=completed_at,
            )
        )
        inserted += 1

    # Set parent settled + zero its file_size so it stops counting toward
    # quota; the child rows now carry the real accounting.
    conn.execute(
        user_cloud_files.update()
        .where(user_cloud_files.c.id == parent_row["id"])
        .values(
            settled_at=now,
            file_size=0,
            status="transfer_completed",
            progress=100,
            updated_at=now,
        )
    )
    return {"already_settled": False, "inserted": inserted}


def update_transfer_record(
    conn: Connection,
    *,
    user_id: int,
    reservation_id: str,
    file_id: Optional[str],
    file_name: Optional[str],
    file_size: int,
    upstream_status: Optional[int],
    progress: int,
    upstream_payload: dict | list,
) -> dict | None:
    row = (
        conn.execute(
            select(
                user_cloud_files.c.id,
                user_cloud_files.c.file_name,
                user_cloud_files.c.file_size,
            )
            .where(
                and_(
                    user_cloud_files.c.user_id == user_id,
                    user_cloud_files.c.reservation_id == reservation_id,
                    user_cloud_files.c.source == "transfer",
                    user_cloud_files.c.status != "deleted",
                )
            )
            .with_for_update()
        )
        .mappings()
        .first()
    )
    if row is None:
        return None

    reserved_size = int(row["file_size"] or 0)
    final_size = max(reserved_size, int(file_size or 0))
    ensure_callback_quota(
        conn,
        user_id=user_id,
        reserved_size=reserved_size,
        final_size=final_size,
    )

    safe_progress = max(0, min(int(progress or 0), 100))
    safe_upstream_status = int(upstream_status or 0)
    status = "transfer_pending"
    if safe_progress >= 100 or safe_upstream_status == 2:
        status = "transfer_completed"
        safe_progress = 100
    elif safe_progress > 0:
        status = "transfer_running"

    name = file_name or row["file_name"] or file_id or "transfer"
    suffix = suffix_for(name)
    now = utcnow()
    conn.execute(
        user_cloud_files.update()
        .where(user_cloud_files.c.id == row["id"])
        .values(
            file_id=file_id,
            file_name=name,
            suffix=suffix,
            category=category_for_suffix(suffix),
            file_size=final_size,
            status=status,
            upstream_status=upstream_status,
            progress=safe_progress,
            upstream_payload=upstream_payload,
            completed_at=now if status == "transfer_completed" else None,
            updated_at=now,
        )
    )
    return {"file_id": file_id, "reservation_id": reservation_id}


def refresh_transfer_record(
    conn: Connection,
    *,
    user_id: int,
    file_id: str,
    file_name: Optional[str],
    file_size: int,
    upstream_status: Optional[int],
    progress: int,
    upstream_payload: dict | list,
) -> dict | None:
    row = (
        conn.execute(
            select(
                user_cloud_files.c.id,
                user_cloud_files.c.file_name,
                user_cloud_files.c.file_size,
            )
            .where(
                and_(
                    user_cloud_files.c.user_id == user_id,
                    user_cloud_files.c.file_id == file_id,
                    user_cloud_files.c.source == "transfer",
                    user_cloud_files.c.status != "deleted",
                )
            )
            .with_for_update()
        )
        .mappings()
        .first()
    )
    if row is None:
        return None

    reserved_size = int(row["file_size"] or 0)
    final_size = max(reserved_size, int(file_size or 0))
    ensure_callback_quota(
        conn,
        user_id=user_id,
        reserved_size=reserved_size,
        final_size=final_size,
    )

    safe_progress = max(0, min(int(progress or 0), 100))
    safe_upstream_status = int(upstream_status or 0)
    status = "transfer_pending"
    if safe_progress >= 100 or safe_upstream_status == 2:
        status = "transfer_completed"
        safe_progress = 100
    elif safe_progress > 0:
        status = "transfer_running"

    name = file_name or row["file_name"] or file_id or "transfer"
    suffix = suffix_for(name)
    now = utcnow()
    conn.execute(
        user_cloud_files.update()
        .where(user_cloud_files.c.id == row["id"])
        .values(
            file_name=name,
            suffix=suffix,
            category=category_for_suffix(suffix),
            file_size=final_size,
            status=status,
            upstream_status=safe_upstream_status,
            progress=safe_progress,
            upstream_payload=upstream_payload,
            completed_at=now if status == "transfer_completed" else None,
            updated_at=now,
        )
    )
    return {"file_id": file_id}


def list_transfer_records(
    conn: Connection,
    *,
    user_id: int,
    page: int,
    limit: int,
    status: Optional[int],
    order: str,
    order_by: str,
) -> dict:
    filters = [
        user_cloud_files.c.user_id == user_id,
        user_cloud_files.c.source == "transfer",
        user_cloud_files.c.status != "deleted",
        # Hide settled link parents : once a parent has fanned out
        # into per-file child rows, the parent is a shell. Children
        # (parent_reservation_id IS NOT NULL) and unsettled parents
        # (settled_at IS NULL) both stay visible.
        or_(
            user_cloud_files.c.parent_reservation_id.is_not(None),
            user_cloud_files.c.settled_at.is_(None),
        ),
    ]
    if status is not None:
        if status == 3:
            filters.append(
                or_(
                    user_cloud_files.c.upstream_status == status,
                    user_cloud_files.c.status == "failed",
                )
            )
        else:
            filters.append(
                and_(
                    user_cloud_files.c.upstream_status == status,
                    user_cloud_files.c.status != "failed",
                )
            )

    total = conn.execute(
        select(func.count()).select_from(user_cloud_files).where(and_(*filters))
    ).scalar_one()
    sortable = {
        "created_at": user_cloud_files.c.created_at,
        "completed_time": user_cloud_files.c.completed_at,
        "file_size": user_cloud_files.c.file_size,
    }
    order_column = sortable.get(order_by, user_cloud_files.c.created_at)
    order_clause = order_column.asc() if order == "asc" else order_column.desc()
    rows = (
        conn.execute(
            select(user_cloud_files)
            .where(and_(*filters))
            .order_by(order_clause)
            .limit(limit)
            .offset((page - 1) * limit)
        )
        .mappings()
        .all()
    )
    return {
        "total": int(total or 0),
        "page": page,
        "limit": limit,
        "total_pages": math.ceil((total or 0) / limit) if limit else 0,
        "data": [row_to_transfer_task(row) for row in rows],
    }


def owned_existing_file_ids(
    conn: Connection,
    *,
    user_id: int,
    file_ids: list[str],
) -> set[str]:
    if not file_ids:
        return set()
    requested = {str(file_id) for file_id in file_ids}
    rows = conn.execute(
        select(user_cloud_files.c.file_id, user_cloud_files.c.reservation_id).where(
            and_(
                user_cloud_files.c.user_id == user_id,
                or_(
                    user_cloud_files.c.file_id.in_(requested),
                    user_cloud_files.c.reservation_id.in_(requested),
                ),
                user_cloud_files.c.status != "deleted",
            )
        )
    ).all()
    matched: set[str] = set()
    for row in rows:
        if row.file_id in requested:
            matched.add(str(row.file_id))
        if row.reservation_id in requested:
            matched.add(str(row.reservation_id))
    return matched


def row_to_cloud_file(row) -> dict:
    created_at = _iso(row.get("created_at"))
    completed_at = _iso(row.get("completed_at")) or created_at
    return {
        "file_id": row.get("file_id"),
        "file_name": row.get("file_name"),
        "file_size": int(row.get("file_size") or 0),
        "suffix": row.get("suffix") or "",
        "category": int(row.get("category") or 0),
        "completed_time": completed_at,
        "created_at": created_at,
        "srt_file_hash": row.get("srt_file_hash"),
    }


def row_to_transfer_task(row) -> dict:
    created_at = _iso(row.get("created_at"))
    completed_at = _iso(row.get("completed_at"))
    task_id = row.get("file_id") or row.get("reservation_id")
    status = 3 if row.get("status") == "failed" else int(row.get("upstream_status") or 0)
    result = {
        "file_id": task_id,
        "reservation_id": row.get("reservation_id"),
        "upstream_file_id": row.get("file_id"),
        "original_name": row.get("file_name"),
        "file_name": row.get("file_name"),
        "progress": int(row.get("progress") or 0),
        "category": int(row.get("category") or 0),
        "file_size": str(int(row.get("file_size") or 0)),
        "status": status,
        "created_at": created_at,
        "completed_time": completed_at,
    }
    payload = row.get("upstream_payload")
    if isinstance(payload, dict):
        error_message = payload.get("error_message")
        error_code = payload.get("error_code")
        if isinstance(error_message, str) and error_message:
            result["error_message"] = error_message
        if isinstance(error_code, str) and error_code:
            result["error_code"] = error_code
    return result


def _iso(value) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
