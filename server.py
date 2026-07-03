"""Narrator AI Web backend — lightweight Flask service for template price pricing and wallet lifecycle.

Serves fa_template_price data from Fly Postgres (toC database).
Deployed alongside narrator-ai-web for toC pricing.
"""

import os
import json
import time
import hmac
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import quote
from flask import Flask, Response, jsonify, request
from werkzeug.exceptions import BadRequest, UnsupportedMediaType
import psycopg2
import psycopg2.errors
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import (
    IntegrityError,
    SQLAlchemyError,
    TimeoutError as SQLAlchemyTimeoutError,
)
from pricing import (
    SUPPORTED_QUERY_PARAMS,
    SrtRealtimeError,
    UpstreamBaokuanError,
    compute_srt_realtime_quote,
    fetch_movie_baokuan,
    group_v2_catalog_tiers_by_template_id,
    select_all_hard_prices,
    select_v2_catalog_tiers_by_template_ids,
    select_single_hard_price,
    template_id_from_baokuan_code,
)
from account.format import format_balance, mask_mobile
from cloud_drive.store import (
    CloudDriveStoreError,
    TERMINAL_UPSTREAM_STATUSES,
    attach_link_upload_id,
    attach_upload_file_id,
    complete_upload_callback,
    create_transfer_record,
    finalize_delete_file,
    finalize_delete_files,
    get_file as cd_get_file,
    get_local_upload_reservation,
    get_storage_usage as cd_get_storage_usage,
    list_files as cd_list_files,
    list_transfer_records as cd_list_transfer_records,
    list_unsettled_link_parents,
    mark_reservation_failed,
    normalize_sha256,
    owned_existing_file_ids,
    reserve_local_upload,
    restore_file_status,
    settle_link_parent_with_children,
    soft_delete_file,
    suffix_for,
)
from cloud_drive.upstream import (
    UpstreamConfig,
    UpstreamCloudDriveError,
    call_cloud_drive_upstream,
    load_upstream_config,
)
from narrator_metadata.endpoints import ROUTES as _METADATA_ROUTES
from narrator_metadata.upstream import (
    UpstreamNarratorError,
    fetch_narrator_upstream,
)
from narrator_proxy.routes import (
    COMMENTARY_WRITING_GET,
    COMMENTARY_WRITING_POST,
    DYNAMIC_ROUTES as _PROXY_DYNAMIC_ROUTES,
    STATIC_ROUTES as _PROXY_STATIC_ROUTES,
)
from narrator_proxy.upstream import proxy_narrator_upstream
from pricing_catalog_v2 import (
    CatalogInheritedInvariantViolation,
    CatalogPersistenceError,
    CatalogTemplateNotFound,
    CatalogTierMissing,
    CatalogValidationError,
    list_all_versions,
    resolve_effective_tiers,
    upsert_tiers,
)
from pricing_quote_v2 import (
    QuoteBreakdownMismatch,
    QuoteNotFound,
    QuotePersistenceError,
    commit_master_task_snapshot,
    generate_quote,
)
from pricing_quote_v2.auto_refund import apply_auto_refund_if_eligible
from pricing_quote_v2.errors import QuoteValidationError
from narrator_tasks.store import (
    CAS_MISMATCH,
    create_task as nt_create_task,
    get_task as nt_get_task,
    list_tasks as nt_list_tasks,
    replace_task as nt_replace_task,
    upsert_task as nt_upsert_task,
)
from users.admin import (
    AppKeyAlreadyExists,
    DEFAULT_BALANCE_POINTS as DEFAULT_NEW_USER_BALANCE,
    InvalidAppKeyFormat,
    InvalidBalance,
    UserNotFound,
    create_user as admin_create_user,
    get_user_by_app_key as admin_get_user_by_app_key,
    update_user as admin_update_user,
)
from users.auth import require_web_user_auth
from users.schema import generate_app_key
from wallet import PostgresWalletStore, WalletError, WalletService

app = Flask(__name__)
_wallet_service = None
_db_engine = None
_db_pool_lock = threading.Lock()
_cloud_drive_transfer_executor = None
_cloud_drive_transfer_slots = None
_cloud_drive_transfer_executor_config = None
_cloud_drive_transfer_executor_lock = threading.Lock()

# Prometheus metrics
REQUEST_COUNT = Counter(
    "pricing_requests_total", "Total pricing requests", ["endpoint", "status"]
)
REQUEST_LATENCY = Histogram(
    "pricing_request_duration_seconds", "Request latency", ["endpoint"]
)
QUERY_ERRORS = Counter("pricing_query_errors_total", "DB query errors")
WALLET_ERRORS = Counter("wallet_errors_total", "Wallet API errors", ["code"])
DB_ERRORS = Counter("backend_db_errors_total", "Backend DB errors", ["surface", "code"])
HARD_PRICE_METRIC_EVENTS = Counter(
    "hard_price_metric_events_total",
    "Hard-price order modification metric sink events",
    ["status"],
)


class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


app.json.encoder = DecimalEncoder


def positive_int_env(name, default):
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise WalletError(
            503,
            "WALLET_CONFIG_INVALID",
            f"{name} must be a positive integer.",
            retryable=True,
        ) from exc
    if value <= 0:
        raise WalletError(
            503,
            "WALLET_CONFIG_INVALID",
            f"{name} must be a positive integer.",
            retryable=True,
        )
    return value


def non_negative_int_env(name, default):
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise WalletError(
            503,
            "WALLET_CONFIG_INVALID",
            f"{name} must be a non-negative integer.",
            retryable=True,
        ) from exc
    if value < 0:
        raise WalletError(
            503,
            "WALLET_CONFIG_INVALID",
            f"{name} must be a non-negative integer.",
            retryable=True,
        )
    return value


def normalize_database_url(dsn: str) -> str:
    if dsn.startswith("postgres://"):
        return "postgresql+psycopg2://" + dsn.removeprefix("postgres://")
    if dsn.startswith("postgresql://"):
        return "postgresql+psycopg2://" + dsn.removeprefix("postgresql://")
    return dsn


def db_url():
    dsn = os.environ.get("DATABASE_URL", "")
    if dsn:
        return normalize_database_url(dsn)
    return URL.create(
        "postgresql+psycopg2",
        username=os.environ.get("NARRATOR_DB_USER", "postgres"),
        password=os.environ.get("NARRATOR_DB_PASSWORD", ""),
        host=os.environ.get("NARRATOR_DB_HOST", "localhost"),
        port=int(os.environ.get("NARRATOR_DB_PORT", "5432")),
        database=os.environ.get("NARRATOR_DB_NAME", "postgres"),
    )


def get_db_engine():
    global _db_engine
    if _db_engine is None:
        with _db_pool_lock:
            if _db_engine is None:
                _db_engine = create_engine(
                    db_url(),
                    pool_pre_ping=True,
                    pool_use_lifo=True,
                    pool_size=positive_int_env("NARRATOR_DB_POOL_SIZE", 10),
                    max_overflow=non_negative_int_env(
                        "NARRATOR_DB_POOL_MAX_OVERFLOW", 0
                    ),
                    pool_timeout=positive_int_env(
                        "NARRATOR_DB_POOL_TIMEOUT_SECONDS", 5
                    ),
                    pool_recycle=positive_int_env(
                        "NARRATOR_DB_POOL_RECYCLE_SECONDS", 1800
                    ),
                    connect_args={
                        "connect_timeout": positive_int_env(
                            "NARRATOR_DB_CONNECT_TIMEOUT_SECONDS", 10
                        ),
                        "options": "-c statement_timeout="
                        f"{positive_int_env('NARRATOR_DB_STATEMENT_TIMEOUT_MS', 15000)}",
                    },
                )
    return _db_engine


def get_db_core_connection():
    return get_db_engine().connect()


def get_wallet_service():
    global _wallet_service
    if _wallet_service is None:
        _wallet_service = WalletService(
            store=PostgresWalletStore(get_db_core_connection),
            quote_ttl_seconds=positive_int_env("WALLET_QUOTE_TTL_SECONDS", 900),
            idempotency_ttl_hours=positive_int_env("WALLET_IDEMPOTENCY_TTL_HOURS", 168),
        )
    return _wallet_service


def wallet_error_response(error):
    return (
        jsonify(
            {
                "success": False,
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "retryable": error.retryable,
                    "details": error.details,
                },
            }
        ),
        error.http_status,
    )


def require_wallet_auth():
    expected = os.environ.get("WALLET_BFF_AUTH_TOKEN")
    if not expected:
        raise WalletError(
            503,
            "WALLET_NOT_CONFIGURED",
            "Wallet BFF auth token is not configured.",
            retryable=True,
        )
    auth = request.headers.get("Authorization", "")
    if not hmac.compare_digest(auth, f"Bearer {expected}"):
        raise WalletError(401, "UNAUTHORIZED", "Unauthorized wallet request.")


def require_pricing_bff_auth():
    """Verify the Web-tier caller before proxying to upstream movie-baokuan.

    Returns None on success or a Flask `(response, status)` tuple on failure;
    callers should `if result is not None: return result`. Kept inline rather
    than raising WalletError because the pricing endpoints already return
    error responses via direct jsonify tuples, not via the wallet exception
    flow.

    Non-ASCII handling: `hmac.compare_digest` rejects str inputs containing
    non-ASCII codepoints with a TypeError. Without the encode-guard below an
    attacker could send `Authorization: Bearer 中文…` (or operators could
    mis-configure a non-ASCII token) and get HTTP 500 instead of the
    documented 401/503, creating a cheap unauthenticated 500 path
    (review security-sensitive).

    The token is scoped to `/pricing/movie-baokuan` for now (HP-XX); existing
    `/pricing/hard-price*` endpoints remain unauthenticated to avoid a wider
    refactor — adding auth there is a separate, deliberate change.
    """
    expected = os.environ.get("PRICING_BFF_AUTH_TOKEN")
    # Fail-closed on misconfiguration: empty or non-ASCII tokens are not
    # usable inputs to hmac.compare_digest, so we surface as 503 rather
    # than letting the compare blow up later.
    if not expected or not expected.isascii():
        return jsonify(
            {
                "success": False,
                "error": {
                    "code": "PRICING_BFF_NOT_CONFIGURED",
                    "message": "Pricing BFF auth token is not configured.",
                    "retryable": True,
                    "details": {},
                },
            }
        ), 503

    auth = request.headers.get("Authorization", "")
    # Encode both sides to ASCII bytes inside a try so a hostile / malformed
    # header (e.g. `Authorization: Bearer 中文`) returns 401 instead of 500.
    try:
        candidate = auth.encode("ascii")
        reference = f"Bearer {expected}".encode("ascii")
    except UnicodeEncodeError:
        return jsonify(
            {
                "success": False,
                "error": {
                    "code": "UNAUTHORIZED",
                    "message": "Unauthorized pricing request.",
                    "retryable": False,
                    "details": {},
                },
            }
        ), 401

    if not hmac.compare_digest(candidate, reference):
        return jsonify(
            {
                "success": False,
                "error": {
                    "code": "UNAUTHORIZED",
                    "message": "Unauthorized pricing request.",
                    "retryable": False,
                    "details": {},
                },
            }
        ), 401
    return None


def wallet_json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise WalletError(400, "BAD_REQUEST", "Request body must be a JSON object.")
    return data


def wallet_idempotency_key():
    return request.headers.get("Idempotency-Key")


HARD_PRICE_METRIC_FIELDS = [
    "original_template_minutes",
    "modified_script_minutes",
    "delta_minutes",
    "modification_ratio",
]


def hard_price_metric_logstore_name():
    return os.environ.get(
        "HARD_PRICE_METRIC_SLS_LOGSTORE",
        "hard-price-order-modification-metrics",
    )


def validate_hard_price_metric_payload(body):
    required = [
        "order_id",
        "template_id",
        "final_script_minutes",
        *HARD_PRICE_METRIC_FIELDS,
        "created_at",
    ]
    missing = [field for field in required if field not in body]
    if missing:
        raise WalletError(
            400,
            "HARD_PRICE_METRIC_BAD_REQUEST",
            "Hard-price metric payload is missing required fields.",
            details={"missing_fields": missing},
        )

    normalized = {}
    for field in ["order_id", "template_id"]:
        value = body[field]
        if not isinstance(value, str) or not value.strip():
            raise WalletError(
                400,
                "HARD_PRICE_METRIC_BAD_REQUEST",
                f"{field} must be a non-empty string.",
                details={"field": field},
            )
        normalized[field] = value.strip()

    for field in ["final_script_minutes", *HARD_PRICE_METRIC_FIELDS]:
        try:
            value = Decimal(str(body[field])).quantize(Decimal("0.0001"))
        except Exception as exc:
            raise WalletError(
                400,
                "HARD_PRICE_METRIC_BAD_REQUEST",
                f"{field} must be a numeric value.",
                details={"field": field},
            ) from exc
        if field != "modification_ratio" and value < 0:
            raise WalletError(
                400,
                "HARD_PRICE_METRIC_BAD_REQUEST",
                f"{field} must be non-negative.",
                details={"field": field},
            )
        normalized[field] = str(value)

    created_at = str(body["created_at"])
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WalletError(
            400,
            "HARD_PRICE_METRIC_BAD_REQUEST",
            "created_at must be ISO 8601 UTC.",
            details={"field": "created_at"},
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise WalletError(
            400,
            "HARD_PRICE_METRIC_BAD_REQUEST",
            "created_at must include UTC timezone.",
            details={"field": "created_at"},
        )
    normalized["created_at"] = (
        parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    return normalized


def write_hard_price_metric_to_sls(payload):
    app.logger.info(
        "hard_price_order_modification_metric %s",
        json.dumps(payload, separators=(",", ":"), sort_keys=True),
    )


def handle_wallet_call(callback):
    try:
        require_wallet_auth()
        result = callback()
        return jsonify({"success": True, "data": result.data}), result.status_code
    except WalletError as error:
        WALLET_ERRORS.labels(code=error.code).inc()
        return wallet_error_response(error)
    except psycopg2.errors.UniqueViolation as error:
        DB_ERRORS.labels(surface="wallet", code=error.__class__.__name__).inc()
        return wallet_error_response(
            WalletError(
                409,
                "DUPLICATE_SUBMIT",
                "Wallet request conflicted with an existing committed operation.",
                details={"db_error": error.__class__.__name__},
            )
        )
    except IntegrityError as error:
        DB_ERRORS.labels(surface="wallet", code=error.__class__.__name__).inc()
        if isinstance(error.orig, psycopg2.errors.UniqueViolation):
            return wallet_error_response(
                WalletError(
                    409,
                    "DUPLICATE_SUBMIT",
                    "Wallet request conflicted with an existing committed operation.",
                    details={"db_error": error.orig.__class__.__name__},
                )
            )
        return wallet_error_response(
            WalletError(
                503,
                "WALLET_DB_ERROR",
                "Wallet database request failed.",
                retryable=True,
                details={"db_error": error.__class__.__name__},
            )
        )
    except (
        psycopg2.errors.DeadlockDetected,
        psycopg2.errors.SerializationFailure,
        psycopg2.errors.LockNotAvailable,
        psycopg2.OperationalError,
        psycopg2.InterfaceError,
        SQLAlchemyTimeoutError,
        SQLAlchemyError,
    ) as error:
        DB_ERRORS.labels(surface="wallet", code=error.__class__.__name__).inc()
        return wallet_error_response(
            WalletError(
                503,
                "WALLET_DB_UNAVAILABLE",
                "Wallet database is temporarily unavailable.",
                retryable=True,
                details={"db_error": error.__class__.__name__},
            )
        )
    except psycopg2.DatabaseError as error:
        DB_ERRORS.labels(surface="wallet", code=error.__class__.__name__).inc()
        return wallet_error_response(
            WalletError(
                503,
                "WALLET_DB_ERROR",
                "Wallet database request failed.",
                retryable=True,
                details={"db_error": error.__class__.__name__},
            )
        )


def db_unavailable_response(surface, error):
    DB_ERRORS.labels(surface=surface, code=error.__class__.__name__).inc()
    code = "PRICING_DB_UNAVAILABLE" if surface == "pricing" else "DB_UNAVAILABLE"
    return wallet_error_response(
        WalletError(
            503,
            code,
            "Database is temporarily unavailable.",
            retryable=True,
            details={"db_error": error.__class__.__name__},
        )
    )


def run_db_query(surface, callback):
    try:
        return callback()
    except WalletError as error:
        return wallet_error_response(error)
    except (
        psycopg2.errors.DeadlockDetected,
        psycopg2.errors.SerializationFailure,
        psycopg2.errors.LockNotAvailable,
        psycopg2.OperationalError,
        psycopg2.InterfaceError,
        SQLAlchemyError,
    ) as error:
        QUERY_ERRORS.inc()
        return db_unavailable_response(surface, error)


def run_pricing_query(callback):
    return run_db_query("pricing", callback)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/ready")
def ready():
    def check():
        conn = get_db_core_connection()
        try:
            row = (
                conn.execute(
                    text(
                        "SELECT count(*) AS wallet_tables "
                        "FROM (VALUES "
                        "(to_regclass('wallet_accounts')), "
                        "(to_regclass('wallet_quotes')), "
                        "(to_regclass('wallet_transactions')), "
                        "(to_regclass('wallet_ledger_entries')), "
                        "(to_regclass('wallet_idempotency_records'))"
                        ") AS required(table_name) "
                        "WHERE table_name IS NOT NULL"
                    )
                )
                .mappings()
                .first()
            )
        finally:
            conn.close()
        if int(row["wallet_tables"]) != 5:
            raise WalletError(
                503,
                "WALLET_SCHEMA_NOT_READY",
                "Wallet schema is not ready.",
                retryable=True,
                details={"wallet_tables": row["wallet_tables"]},
            )
        return jsonify({"status": "ready", "wallet_tables": row["wallet_tables"]})

    return run_db_query("readiness", check)


@app.route("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


@app.route("/api/metrics/hard-price", methods=["POST"])
def hard_price_metric_sink():
    start = time.time()
    endpoint_label = "hard-price-metric-sink"

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="400").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        HARD_PRICE_METRIC_EVENTS.labels(status="rejected").inc()
        return wallet_error_response(
            WalletError(
                400,
                "HARD_PRICE_METRIC_BAD_REQUEST",
                "Request body must be a JSON object.",
            )
        )

    try:
        payload = validate_hard_price_metric_payload(body)
    except WalletError as error:
        REQUEST_COUNT.labels(
            endpoint=endpoint_label, status=str(error.http_status)
        ).inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        HARD_PRICE_METRIC_EVENTS.labels(status="rejected").inc()
        return wallet_error_response(error)

    logstore = hard_price_metric_logstore_name()
    event = {
        **payload,
        "schema_version": "hard_price_order_modification_metric_v1",
        "sls_logstore": logstore,
    }
    try:
        write_hard_price_metric_to_sls(event)
    except Exception as error:
        app.logger.error(
            "Hard-price metric sink degraded: order_id=%s error=%s",
            payload["order_id"],
            error.__class__.__name__,
        )
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="202_degraded").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        HARD_PRICE_METRIC_EVENTS.labels(status="degraded").inc()
        return jsonify(
            {
                "success": True,
                "data": {
                    "accepted": True,
                    "telemetry_status": "degraded",
                    "error_code": "HARD_PRICE_METRIC_SINK_FAILED",
                    "retryable": True,
                    "sls_logstore": logstore,
                },
            }
        ), 202

    REQUEST_COUNT.labels(endpoint=endpoint_label, status="202").inc()
    REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
    HARD_PRICE_METRIC_EVENTS.labels(status="accepted").inc()
    return jsonify(
        {
            "success": True,
            "data": {
                "accepted": True,
                "telemetry_status": "accepted",
                "schema_version": event["schema_version"],
                "sls_logstore": logstore,
                "fields": [
                    "order_id",
                    "template_id",
                    "final_script_minutes",
                    *HARD_PRICE_METRIC_FIELDS,
                    "created_at",
                ],
            },
        }
    ), 202


@app.route("/openapi.json")
def openapi_spec():
    path = os.path.join(os.path.dirname(__file__), "openapi.json")
    with open(path, encoding="utf-8") as f:
        return Response(f.read(), mimetype="application/json; charset=utf-8")


@app.route("/pricing/hard-price", methods=["POST"])
def query_hard_price():
    def query():
        start = time.time()
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            REQUEST_COUNT.labels(endpoint="hard-price", status="400").inc()
            return jsonify({"error": "request body must be a JSON object"}), 400
        template_id = data.get("template_id")
        combo_key = data.get("combo_key")
        if (
            not isinstance(template_id, int)
            or isinstance(template_id, bool)
            or not isinstance(combo_key, str)
        ):
            REQUEST_COUNT.labels(endpoint="hard-price", status="400").inc()
            return jsonify(
                {"error": "template_id (int) and combo_key (string) required"}
            ), 400

        conn = get_db_core_connection()
        try:
            row = (
                conn.execute(select_single_hard_price(template_id, combo_key))
                .mappings()
                .first()
            )
        finally:
            conn.close()

        if not row:
            REQUEST_COUNT.labels(endpoint="hard-price", status="404").inc()
            REQUEST_LATENCY.labels(endpoint="hard-price").observe(time.time() - start)
            return jsonify({"error": "not found"}), 404
        REQUEST_COUNT.labels(endpoint="hard-price", status="200").inc()
        REQUEST_LATENCY.labels(endpoint="hard-price").observe(time.time() - start)
        return jsonify({"success": True, "data": dict(row)})

    return run_pricing_query(query)


@app.route("/pricing/hard-price/all")
def query_all_hard_prices():
    def query():
        template_id = request.args.get("template_id", type=int)
        if not template_id:
            return jsonify({"error": "template_id required"}), 400

        conn = get_db_core_connection()
        try:
            rows = list(
                conn.execute(select_all_hard_prices(template_id)).mappings().all()
            )
        finally:
            conn.close()

        if not rows:
            return jsonify({"error": "not found"}), 404
        return jsonify(
            {
                "success": True,
                "data": {"template_id": template_id, "prices": [dict(r) for r in rows]},
            }
        )

    return run_pricing_query(query)


@app.route("/pricing/srt-realtime-quote", methods=["POST"])
def query_srt_realtime_quote():
    start = time.time()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        REQUEST_COUNT.labels(endpoint="srt-realtime-quote", status="400").inc()
        return jsonify(
            {
                "success": False,
                "error": {
                    "code": "BAD_REQUEST",
                    "message": "request body must be a JSON object",
                    "retryable": False,
                    "details": {},
                },
            }
        ), 400

    try:
        pricing_rule_version = positive_int_env("PRICING_RULE_VERSION", 1)
        quote = compute_srt_realtime_quote(
            data.get("combo_key"),
            srt_payload=data.get("srt_payload"),
            srt_metrics=data.get("srt_metrics"),
            pricing_rule_version=pricing_rule_version,
            correlation_id=data.get("correlation_id"),
        )
    except SrtRealtimeError as error:
        REQUEST_COUNT.labels(
            endpoint="srt-realtime-quote", status=str(error.http_status)
        ).inc()
        REQUEST_LATENCY.labels(endpoint="srt-realtime-quote").observe(
            time.time() - start
        )
        return jsonify(
            {
                "success": False,
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "retryable": error.retryable,
                    "details": error.details,
                },
            }
        ), error.http_status
    except WalletError as error:
        REQUEST_COUNT.labels(
            endpoint="srt-realtime-quote", status=str(error.http_status)
        ).inc()
        REQUEST_LATENCY.labels(endpoint="srt-realtime-quote").observe(
            time.time() - start
        )
        return wallet_error_response(error)
    except Exception as error:  # noqa: BLE001 — surface as 503 for caller retry
        REQUEST_COUNT.labels(endpoint="srt-realtime-quote", status="503").inc()
        REQUEST_LATENCY.labels(endpoint="srt-realtime-quote").observe(
            time.time() - start
        )
        return jsonify(
            {
                "success": False,
                "error": {
                    "code": "PRICING_SERVICE_UNAVAILABLE",
                    "message": "srt realtime quote service is temporarily unavailable",
                    "retryable": True,
                    "details": {"error": error.__class__.__name__},
                },
            }
        ), 503

    REQUEST_COUNT.labels(endpoint="srt-realtime-quote", status="200").inc()
    REQUEST_LATENCY.labels(endpoint="srt-realtime-quote").observe(time.time() - start)
    return jsonify({"success": True, "data": quote})


def _movie_baokuan_schema_error(start: float, endpoint_label: str, *, reason: str):
    """Return a 502 UPSTREAM_SCHEMA_ERROR when upstream claims success
    (code=10000) but the payload shape violates the documented contract.
    Non-retryable: an upstream schema drift will not self-heal on retry."""
    REQUEST_COUNT.labels(endpoint=endpoint_label, status="502").inc()
    REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
    return jsonify(
        {
            "success": False,
            "error": {
                "code": "UPSTREAM_SCHEMA_ERROR",
                "message": "Upstream movie-baokuan returned a malformed success payload.",
                "retryable": False,
                "details": {"reason": reason},
            },
        }
    ), 502


@app.route("/pricing/movie-baokuan", methods=["GET"])
def query_movie_baokuan_with_hard_price():
    """HP-XX: proxy upstream `/v2/res/movie-baokuan` and augment each
    returned item with its complete 5-tier v2 catalog. Join key is upstream
    `item.code` (`xy0046`) to local `pricing_template_v2.template_id`
    (`46`). Items without a complete 5-tier v2 catalog are omitted from
    the page.

    Auth (two independent checks, both must pass):
      1. `Authorization: Bearer <PRICING_BFF_AUTH_TOKEN>` — service identity;
         only the web tier holds this token. Protects upstream quota at the
         network layer.
      2. `X-Web-App-Key: <users.app_key>` — end-user identity; ties the
         request to a row in the reseller `users` table . Without
         this, the BFF Bearer becomes a free pass for any internet caller
         to consume upstream quota via the web route handler.
    """
    start = time.time()
    endpoint_label = "movie-baokuan"
    auth_error = require_pricing_bff_auth()
    if auth_error is not None:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status=str(auth_error[1])).inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return auth_error

    user_auth_error = require_web_user_auth(get_db_core_connection)
    if user_auth_error is not None:
        REQUEST_COUNT.labels(
            endpoint=endpoint_label, status=str(user_auth_error[1])
        ).inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return user_auth_error

    params = {key: request.args.get(key) for key in SUPPORTED_QUERY_PARAMS}

    try:
        upstream_payload = fetch_movie_baokuan(params)
    except UpstreamBaokuanError as error:
        REQUEST_COUNT.labels(
            endpoint=endpoint_label, status=str(error.http_status)
        ).inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return jsonify(
            {
                "success": False,
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "retryable": error.retryable,
                    "details": error.details,
                },
            }
        ), error.http_status

    # Boundary check regression coverage: a valid backend response is always a top-level
    # object. A list/string/null payload is treated as schema drift, not a
    # business-error pass-through, so it doesn't leak as a contract-violating
    # 200 to Web.
    if not isinstance(upstream_payload, dict):
        return _movie_baokuan_schema_error(
            start, endpoint_label, reason="upstream payload is not an object"
        )

    if upstream_payload.get("code") != 10000:
        # Forward upstream business error as-is (the response shape already
        # matches what Web expects).
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="upstream_non_10000").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return jsonify(upstream_payload), 200

    # `code == 10000` means upstream claims success — but the success payload
    # shape is still our boundary, so validate it before reading. Without this
    # guard, a contract drift (data as string, items missing, etc.) crashes
    # the route with AttributeError → 500.
    data = upstream_payload.get("data")
    if not isinstance(data, dict):
        return _movie_baokuan_schema_error(
            start, endpoint_label, reason="`data` is not an object"
        )
    items = data.get("items")
    if not isinstance(items, list):
        return _movie_baokuan_schema_error(
            start, endpoint_label, reason="`data.items` is not a list"
        )
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            return _movie_baokuan_schema_error(
                start,
                endpoint_label,
                reason=f"`data.items[{idx}]` is not an object",
            )
    template_ids_by_code = {
        item.get("code"): template_id_from_baokuan_code(item.get("code"))
        for item in items
    }
    template_ids = sorted({tid for tid in template_ids_by_code.values() if tid})

    catalog_map: dict = {}
    if template_ids:

        def fetch_catalog():
            conn = get_db_core_connection()
            try:
                rows = list(
                    conn.execute(
                        select_v2_catalog_tiers_by_template_ids(template_ids)
                    )
                    .mappings()
                    .all()
                )
            finally:
                conn.close()
            return group_v2_catalog_tiers_by_template_id(rows)

        result = run_pricing_query(fetch_catalog)
        # run_pricing_query swallows DB errors and returns a Flask (response,
        # status) tuple in that case — forward it instead of trying to read
        # prices off it.
        if not isinstance(result, dict):
            return result
        catalog_map = result

    filtered_items = []
    for item in items:
        template_id = template_ids_by_code.get(item.get("code"))
        entry = catalog_map.get(template_id) if template_id else None
        if not entry:
            continue
        item["tiers"] = entry["tiers"]
        item["pricing_rule_version"] = entry["pricing_rule_version"]
        filtered_items.append(item)
    data["items"] = filtered_items
    # Preserve upstream `data.total` for pagination. We still filter the
    # current page's `items` to priced templates, but the UI needs the
    # upstream total count rather than the filtered count of this page.

    REQUEST_COUNT.labels(endpoint=endpoint_label, status="200").inc()
    REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
    return jsonify(upstream_payload)


@app.route("/account/me", methods=["GET"])
def get_account_me():
    """Return the current end-user's profile + balance.

    Auth: `X-Web-App-Key` (validated via `require_web_user_auth`). The
    Bearer token used by `/pricing/movie-baokuan` is intentionally NOT
    required here — `/account/me` is a per-user read against the reseller's
    own users table, not a proxy to upstream paid quota.

    Response shape : `data.balance` is a decimal-string, `data.mobile`
    is masked at the API boundary (raw value never leaves backend), nullable
    fields are JSON `null` (not omitted, not empty string).
    """
    start = time.time()
    endpoint_label = "account-me"

    user_auth_error = require_web_user_auth(get_db_core_connection)
    if user_auth_error is not None:
        REQUEST_COUNT.labels(
            endpoint=endpoint_label, status=str(user_auth_error[1])
        ).inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return user_auth_error

    app_key = request.headers.get("X-Web-App-Key", "")

    try:
        conn = get_db_core_connection()
        try:
            row = (
                conn.execute(
                    text(
                        "SELECT id, nickname, mobile, email, balance_points, company_name "
                        "FROM users WHERE app_key = :k"
                    ),
                    {"k": app_key},
                )
                .mappings()
                .first()
            )
        finally:
            conn.close()
    except Exception:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="503").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return jsonify(
            {
                "success": False,
                "error": {
                    "code": "USER_LOOKUP_FAILED",
                    "message": "Failed to load user profile.",
                    "retryable": True,
                    "details": {},
                },
            }
        ), 503

    # Defense-in-depth: middleware just verified the row exists; if it has
    # somehow vanished between the two queries (manual delete during the
    # request lifecycle), treat as UNKNOWN rather than crashing on None.
    if row is None:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="401").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return jsonify(
            {
                "success": False,
                "error": {
                    "code": "WEB_APP_KEY_UNKNOWN",
                    "message": "X-Web-App-Key is not recognized.",
                    "retryable": False,
                    "details": {},
                },
            }
        ), 401

    body = {
        "success": True,
        "data": {
            "user_id": row["id"],
            "nickname": row["nickname"],
            "mobile": mask_mobile(row["mobile"]),
            "email": row["email"],
            "balance": format_balance(row["balance_points"]),
            "company_name": row["company_name"],
        },
    }
    REQUEST_COUNT.labels(endpoint=endpoint_label, status="200").inc()
    REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
    return jsonify(body)


def _narrator_tasks_error(code: str, message: str, status: int, *, retryable: bool):
    return (
        jsonify(
            {
                "success": False,
                "error": {
                    "code": code,
                    "message": message,
                    "retryable": retryable,
                    "details": {},
                },
            }
        ),
        status,
    )


def _lookup_user_id(conn, app_key: str):
    """Return the `users.id` for the supplied app_key. Defense-in-depth
    against the (race) case where the user row disappeared between the
    auth middleware's check and the route handler's own lookup."""
    row = conn.execute(
        text("SELECT id FROM users WHERE app_key = :k"), {"k": app_key}
    ).first()
    if row is None:
        return None
    return row.id


def _validate_task_body(body):
    """Type-check the hot columns we forward to the DB layer
    (narrator_task_id / status / current_step). Other fields land in
    the JSONB `data` blob where any JSON value is valid.

    Without this guard, a malformed body like `{"narrator_task_id": 123}`
    either persists a non-string ID (violating the openapi contract) or
    blows up at the psycopg/sqlite adapter and gets mapped to a
    retryable 503 by the route's catch-all exception handler — turning
    client input errors into apparent store outages (review
    on regression coverage).

    Returns None on success, or a Flask `(response, status)` tuple on
    failure (the same envelope as other 400s in this route family).
    """
    nti = body.get("narrator_task_id")
    if nti is not None and not (isinstance(nti, str) and nti):
        return _narrator_tasks_error(
            "BAD_REQUEST",
            "narrator_task_id must be a non-empty string.",
            400,
            retryable=False,
        )
    status = body.get("status")
    if status is not None and not isinstance(status, str):
        return _narrator_tasks_error(
            "BAD_REQUEST",
            "status must be a string.",
            400,
            retryable=False,
        )
    current_step = body.get("current_step")
    if current_step is not None and not isinstance(current_step, str):
        return _narrator_tasks_error(
            "BAD_REQUEST",
            "current_step must be a string or null.",
            400,
            retryable=False,
        )
    return None


def _parse_pagination(args):
    """Mirror the `/api/narrator/master-tasks` web route's validation:
    page >= 1 integer, 1 <= limit <= 9999 integer. Returns
    (page, limit, error_response_or_none)."""
    raw_page = args.get("page", "1")
    raw_limit = args.get("limit", "20")
    try:
        page = int(raw_page)
        limit = int(raw_limit)
    except (TypeError, ValueError):
        return (
            None,
            None,
            _narrator_tasks_error(
                "BAD_REQUEST",
                "page and limit must be integers.",
                400,
                retryable=False,
            ),
        )
    if page < 1:
        return (
            None,
            None,
            _narrator_tasks_error(
                "BAD_REQUEST", "page must be >= 1.", 400, retryable=False
            ),
        )
    if limit < 1 or limit > 9999:
        return (
            None,
            None,
            _narrator_tasks_error(
                "BAD_REQUEST", "limit must be between 1 and 9999.", 400, retryable=False
            ),
        )
    return page, limit, None


# ── /narrator/* metadata wrappers (the implementation requirement) ──────────────────────────────
#
# 8 read-only GET endpoints that thin-wrap upstream narrator
# metadata lookups (types / models / bgm / templates etc). All share the
# same shape, so a single shared handler covers them and routes register
# via `app.add_url_rule` from the `narrator_metadata.endpoints.ROUTES`
# registry. Adding a new metadata wrapper is now a 1-line edit in
# `narrator_metadata/endpoints.py`.


def _serve_narrator_metadata(route_meta):
    """Shared handler for all narrator-metadata wrappers.

    Auth (matches /pricing/movie-baokuan): Bearer for service identity +
    X-Web-App-Key for end-user identity. Both required, both checked
    before any upstream call so an unauth'd internet caller can't probe
    upstream quota through us.

    Upstream forwarding: `narrator_metadata.fetch_narrator_upstream` filters
    request.args through the route's `supported_params` allowlist (drops
    anything undocumented), calls upstream with backend's master
    `OPEN_FASTAPI_APP_KEY`, and returns the parsed body for verbatim
    pass-through. Wire / decode / timeout / oversized failures surface as
    `UpstreamNarratorError` which we map to the standard backend error
    envelope.
    """
    start = time.time()
    label = route_meta.endpoint_label

    auth_error = require_pricing_bff_auth()
    if auth_error is not None:
        REQUEST_COUNT.labels(endpoint=label, status=str(auth_error[1])).inc()
        REQUEST_LATENCY.labels(endpoint=label).observe(time.time() - start)
        return auth_error

    user_auth_error = require_web_user_auth(get_db_core_connection)
    if user_auth_error is not None:
        REQUEST_COUNT.labels(endpoint=label, status=str(user_auth_error[1])).inc()
        REQUEST_LATENCY.labels(endpoint=label).observe(time.time() - start)
        return user_auth_error

    params = {key: request.args.get(key) for key in route_meta.supported_params}
    try:
        upstream_payload = fetch_narrator_upstream(
            route_meta.upstream_path,
            params,
            route_meta.supported_params,
        )
    except UpstreamNarratorError as error:
        REQUEST_COUNT.labels(endpoint=label, status=str(error.http_status)).inc()
        REQUEST_LATENCY.labels(endpoint=label).observe(time.time() - start)
        _log_narrator_upstream_error(label, error)
        return jsonify(
            {
                "success": False,
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "retryable": error.retryable,
                    "details": error.details,
                },
            }
        ), error.http_status

    REQUEST_COUNT.labels(endpoint=label, status="200").inc()
    REQUEST_LATENCY.labels(endpoint=label).observe(time.time() - start)
    return jsonify(upstream_payload)


def _log_narrator_upstream_error(label: str, error: UpstreamNarratorError) -> None:
    details = error.details if isinstance(error.details, dict) else {}
    app.logger.warning(
        "Narrator upstream error: endpoint=%s code=%s status=%s retryable=%s "
        "upstream_path=%s reason=%s",
        label,
        error.code,
        error.http_status,
        error.retryable,
        details.get("upstream_path"),
        details.get("reason"),
    )


def _make_narrator_metadata_view(route_meta):
    """Bind `route_meta` into a Flask view function. Separate factory so
    each registered route has a distinct view callable + name; that keeps
    Flask's endpoint registry sane and makes the routes findable in
    `app.url_map.iter_rules()` for tests."""

    def view():
        return _serve_narrator_metadata(route_meta)

    view.__name__ = f"narrator_metadata_{route_meta.endpoint_label.replace('-', '_')}"
    return view


for _route_meta in _METADATA_ROUTES:
    app.add_url_rule(
        _route_meta.backend_path,
        endpoint=_route_meta.endpoint_label,
        view_func=_make_narrator_metadata_view(_route_meta),
        methods=["GET"],
    )


# ── /narrator/* proxy routes (group G + E, the implementation requirement) ─────────────────────
#
# 11 routes that proxy commentary/subtitle-tools calls to upstream using
# the end-user's app_key (task operations must run under the user's account).
# Auth: Bearer (service identity) + X-Web-App-Key (user identity, forwarded
# to upstream as `app-key`). Static routes register via loop; dynamic ones
# (with <task_id>) and the dual-method writing route register individually.


def _serve_narrator_proxy(route_meta, *, path_args: dict | None = None):
    """Shared handler for narrator_proxy static + dynamic routes.

    Auth mirrors _serve_narrator_metadata (Bearer + X-Web-App-Key). Unlike
    the metadata routes, the user's app_key is forwarded upstream as `app-key`
    rather than the backend's master key — task operations are scoped to the
    user's account identity on the configured upstream API.
    """
    start = time.time()
    label = route_meta.endpoint_label

    auth_error = require_pricing_bff_auth()
    if auth_error is not None:
        REQUEST_COUNT.labels(endpoint=label, status=str(auth_error[1])).inc()
        REQUEST_LATENCY.labels(endpoint=label).observe(time.time() - start)
        return auth_error

    user_auth_error = require_web_user_auth(get_db_core_connection)
    if user_auth_error is not None:
        REQUEST_COUNT.labels(endpoint=label, status=str(user_auth_error[1])).inc()
        REQUEST_LATENCY.labels(endpoint=label).observe(time.time() - start)
        return user_auth_error

    # The user's `X-Web-App-Key` is identity-only (validated above via
    # `require_web_user_auth`) and is intentionally NOT forwarded upstream;
    # `proxy_narrator_upstream` uses the backend's master `OPEN_FASTAPI_APP_KEY`
    # instead (the implementation requirement — reseller keys aren't upstream-registered, and
    # upstream billing/quota accrue to the backend's account).

    # Substitute any dynamic path variables into the upstream path template.
    # URL-encode each arg with `safe=''` so reserved characters (`?`, `/`, `#`,
    # `&`, `=`) inside a Flask path segment cannot break out of the path and
    # smuggle a query string into the upstream URL. Flask URL-decodes path
    # segments before calling the view, so a caller sending `%3F` arrives here
    # as a literal `?` in `path_args` (regression coverage security-sensitive).
    upstream_path = route_meta.upstream_path
    if path_args:
        safe_args = {k: quote(str(v), safe="") for k, v in path_args.items()}
        upstream_path = upstream_path.format(**safe_args)

    # Collect GET query params from the allowlist.
    query_params = None
    if route_meta.query_params:
        query_params = {k: request.args.get(k) for k in route_meta.query_params}
        if route_meta.endpoint_label == "narrator-movie-sucai":
            if query_params.get("page_size") and not query_params.get("size"):
                query_params["size"] = query_params["page_size"]
            query_params.pop("page_size", None)

    # Forward POST body verbatim. Reject malformed JSON with a 400 envelope
    # rather than silently substituting an empty body, which would turn a
    # client input error into a confusing upstream failure (regression coverage
    # caution).
    body = None
    if route_meta.method == "POST":

        def _bad_request(message: str):
            REQUEST_COUNT.labels(endpoint=label, status="400").inc()
            REQUEST_LATENCY.labels(endpoint=label).observe(time.time() - start)
            return jsonify(
                {
                    "success": False,
                    "error": {
                        "code": "BAD_REQUEST",
                        "message": message,
                        "retryable": False,
                        "details": {},
                    },
                }
            ), 400

        # Flask 3 raises `UnsupportedMediaType` (415) when Content-Type is not
        # application/json. Catch it alongside BadRequest so callers always get
        # this route family's JSON envelope + the 400 metric counter, never
        # Flask's default 415 HTML page (regression coverage caution).
        try:
            body = request.get_json(silent=False)
        except (BadRequest, UnsupportedMediaType):
            return _bad_request("Request body must be valid JSON.")
        if not isinstance(body, dict):
            return _bad_request("Request body must be a JSON object.")

    try:
        upstream_payload = proxy_narrator_upstream(
            upstream_path,
            method=route_meta.method,
            query_params=query_params,
            body=body,
            timeout_seconds=route_meta.timeout_seconds,
        )
    except UpstreamNarratorError as error:
        REQUEST_COUNT.labels(endpoint=label, status=str(error.http_status)).inc()
        REQUEST_LATENCY.labels(endpoint=label).observe(time.time() - start)
        _log_narrator_upstream_error(label, error)
        return jsonify(
            {
                "success": False,
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "retryable": error.retryable,
                    "details": error.details,
                },
            }
        ), error.http_status

    REQUEST_COUNT.labels(endpoint=label, status="200").inc()
    REQUEST_LATENCY.labels(endpoint=label).observe(time.time() - start)
    return jsonify(upstream_payload)


def _make_narrator_proxy_view(route_meta):
    def view():
        return _serve_narrator_proxy(route_meta)

    view.__name__ = f"narrator_proxy_{route_meta.endpoint_label.replace('-', '_')}"
    return view


def _make_narrator_proxy_dynamic_view(route_meta):
    def view(task_id):
        return _serve_narrator_proxy(route_meta, path_args={"task_id": task_id})

    view.__name__ = f"narrator_proxy_{route_meta.endpoint_label.replace('-', '_')}"
    return view


for _proxy_meta in _PROXY_STATIC_ROUTES:
    app.add_url_rule(
        _proxy_meta.backend_path,
        endpoint=_proxy_meta.endpoint_label,
        view_func=_make_narrator_proxy_view(_proxy_meta),
        methods=[_proxy_meta.method],
    )

for _proxy_meta in _PROXY_DYNAMIC_ROUTES:
    app.add_url_rule(
        _proxy_meta.backend_path,
        endpoint=_proxy_meta.endpoint_label,
        view_func=_make_narrator_proxy_dynamic_view(_proxy_meta),
        methods=[_proxy_meta.method],
    )


@app.route("/narrator/commentary/writing", methods=["GET", "POST"])
def narrator_commentary_writing():
    route_meta = (
        COMMENTARY_WRITING_GET if request.method == "GET" else COMMENTARY_WRITING_POST
    )
    return _serve_narrator_proxy(route_meta)


# ── /pricing/catalog/* — template price v2 catalog read (the implementation requirement) ────────────
#
# Returns the effective tier list for one template per
# `docs/pricing/v2/catalog-tier-contract.md` §3-§6. Inheritance + multiplier are resolved
# server-side so the caller sees a flat tier list ready for display or
# quote computation.


@app.route("/pricing/catalog/<template_id>/tiers", methods=["GET"])
def pricing_catalog_v2_tiers(template_id: str):
    endpoint_label = "pricing-catalog-v2-tiers"
    start = time.time()

    auth_error = require_pricing_bff_auth()
    if auth_error is not None:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status=str(auth_error[1])).inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return auth_error

    user_auth_error = require_web_user_auth(get_db_core_connection)
    if user_auth_error is not None:
        REQUEST_COUNT.labels(
            endpoint=endpoint_label, status=str(user_auth_error[1])
        ).inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return user_auth_error

    # Wrap BOTH the connection acquisition and the store call in the
    # same persistence-error mapping; otherwise a connection-pool /
    # connect-timeout / DNS failure raises a bare SQLAlchemyError that
    # falls through to Flask's default 500 instead of the contract's
    # 503 CATALOG_PERSISTENCE_ERROR envelope.
    conn = None
    try:
        try:
            conn = get_db_core_connection()
        except SQLAlchemyError as error:
            raise CatalogPersistenceError(
                "Failed to acquire a catalog DB connection.",
                details={"error_class": error.__class__.__name__},
            ) from error
        try:
            metadata, tiers = resolve_effective_tiers(conn, template_id)
        finally:
            conn.close()
    except CatalogTierMissing as error:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="404").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return jsonify(
            {
                "success": False,
                "error": {
                    "code": "CATALOG_TIER_MISSING",
                    "message": (
                        "No catalog entry for this template (after family fallback)."
                    ),
                    "retryable": False,
                    "details": {"template_id": error.template_id},
                },
            }
        ), 404
    except CatalogPersistenceError as error:
        # Log the internal class name + raw message server-side for operators
        # debugging. Do NOT return them to the client — security review flagged
        # exception-derived fields (`error_class` etc.) as info-exposure
        # vectors. Public envelope carries a generic recoverable message
        # plus empty details (security hardening / security regression coverage).
        app.logger.error(
            "Catalog persistence error in /pricing/catalog/<tid>/tiers: "
            "message=%s internal_details=%s",
            error.message,
            error.details,
        )
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="503").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return jsonify(
            {
                "success": False,
                "error": {
                    "code": "CATALOG_PERSISTENCE_ERROR",
                    "message": (
                        "The pricing catalog backend is temporarily unavailable."
                    ),
                    "retryable": True,
                    "details": {},
                },
            }
        ), 503
    except CatalogInheritedInvariantViolation as error:
        # security hardening: distinct from the upsert-side
        # CATALOG_PRO_SURCHARGE_MISMATCH. This one only surfaces on read
        # when family-inheritance arithmetic rounds Pro and Flash to a
        # pair that no longer satisfies §4.1. Caller (admin UX) needs
        # this to differentiate "operator typed wrong values" vs
        # "family base × multiplier inherited badly".
        # Message is a HARDCODED constant (not str(error)) so the
        # exception's __str__ traceback-derived data never reaches the
        # client. The structured `details` fields are all sourced from
        # request/catalog data, not exception internals, so they're safe.
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="422").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return jsonify(
            {
                "success": False,
                "error": {
                    "code": "CATALOG_INHERITED_INVARIANT_VIOLATION",
                    "message": (
                        "Family inheritance produced a Pro/Flash pair that "
                        "no longer satisfies the surcharge invariant after "
                        "rounding."
                    ),
                    "retryable": False,
                    "details": {
                        "template_id": error.template_id,
                        "product_line": error.product_line,
                        "mode": error.mode,
                        "flash_manual_price": error.flash_manual_price,
                        "pro_manual_price": error.pro_manual_price,
                        "inherited_surcharge": error.inherited_surcharge,
                        "derived_surcharge": error.derived_surcharge,
                    },
                },
            }
        ), 422

    REQUEST_COUNT.labels(endpoint=endpoint_label, status="200").inc()
    REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
    return jsonify(
        {
            "success": True,
            "data": {
                "template_id": metadata.template_id,
                "template_family_id": metadata.template_family_id,
                "tier_multiplier": str(metadata.tier_multiplier),
                # The implementation requirement: upstream identity surfaced to admin UI.
                # NULL when the seeder hasn't populated this template
                # yet — frontend renders `-` in that case.
                "code": metadata.code,
                "name": metadata.name,
                "learning_model_id": metadata.learning_model_id,
                "tiers": [
                    {
                        "tier_code": t.tier_code,
                        "source": t.source,
                        "catalog_entry_id": t.catalog_entry_id,
                        "product_line": t.product_line,
                        "mode": t.mode,
                        "quality": t.quality,
                        "flash_pro_axis": t.flash_pro_axis,
                        "manual_price": t.manual_price,
                        "manual_price_raw": t.manual_price_raw,
                        "pro_surcharge_display": t.pro_surcharge_display,
                        "system_reference_price": t.system_reference_price,
                        "system_reference_price_raw": t.system_reference_price_raw,
                        "currency_unit": t.currency_unit,
                        "raw_rate": str(t.raw_rate),
                        "final_rate": str(t.final_rate),
                        "rounding_rule_version": t.rounding_rule_version,
                        "manual_override_warning": t.manual_override_warning,
                        "effective_version": t.effective_version,
                    }
                    for t in tiers
                ],
            },
        }
    )


# ── PUT /pricing/catalog/<template_id>/tiers — admin batch upsert  ────


@app.route("/pricing/catalog/<template_id>/tiers", methods=["PUT"])
def pricing_catalog_v2_upsert(template_id: str):
    endpoint_label = "pricing-catalog-v2-upsert"
    start = time.time()

    auth_error = require_pricing_bff_auth()
    if auth_error is not None:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status=str(auth_error[1])).inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return auth_error

    user_auth_error = require_web_user_auth(get_db_core_connection)
    if user_auth_error is not None:
        REQUEST_COUNT.labels(
            endpoint=endpoint_label, status=str(user_auth_error[1])
        ).inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return user_auth_error

    # Strict JSON parsing: malformed body / wrong content-type → 400
    # envelope (matches narrator_proxy POST pattern from security hardening).
    try:
        body = request.get_json(silent=False)
    except (BadRequest, UnsupportedMediaType):
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="400").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return jsonify(
            {
                "success": False,
                "error": {
                    "code": "BAD_REQUEST",
                    "message": "Request body must be valid JSON.",
                    "retryable": False,
                    "details": {},
                },
            }
        ), 400
    if not isinstance(body, dict) or not isinstance(body.get("tiers"), list):
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="400").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return jsonify(
            {
                "success": False,
                "error": {
                    "code": "BAD_REQUEST",
                    "message": "Request body must be {tiers: [...]}.",
                    "retryable": False,
                    "details": {},
                },
            }
        ), 400

    submitted_tiers = body["tiers"]
    app_key = request.headers.get("X-Web-App-Key", "")

    conn = None
    try:
        try:
            conn = get_db_core_connection()
        except SQLAlchemyError as error:
            raise CatalogPersistenceError(
                "Failed to acquire a catalog DB connection.",
                details={"error_class": error.__class__.__name__},
            ) from error

        # Resolve X-Web-App-Key → users.id for `updated_by`. Defense-in-depth
        # against the race where the auth check passed but the row vanished.
        user_id = _lookup_user_id(conn, app_key)
        if user_id is None:
            REQUEST_COUNT.labels(endpoint=endpoint_label, status="401").inc()
            REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
            return jsonify(
                {
                    "success": False,
                    "error": {
                        "code": "WEB_APP_KEY_UNKNOWN",
                        "message": "X-Web-App-Key is not recognized.",
                        "retryable": False,
                        "details": {},
                    },
                }
            ), 401

        try:
            try:
                inserted = upsert_tiers(
                    conn,
                    template_id=template_id,
                    submitted_tiers=submitted_tiers,
                    updated_by=user_id,
                )
            finally:
                # Commit is needed for SQLAlchemy 2.x autobegin; explicit
                # so failures bubble up and trigger the route's catch.
                pass
            conn.commit()
        finally:
            conn.close()
    except CatalogTemplateNotFound as error:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="404").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return jsonify(
            {
                "success": False,
                "error": {
                    "code": "CATALOG_TEMPLATE_NOT_FOUND",
                    "message": "Template is not registered.",
                    "retryable": False,
                    "details": {"template_id": error.template_id},
                },
            }
        ), 404
    except CatalogValidationError as error:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="422").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return jsonify(
            {
                "success": False,
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "retryable": False,
                    "details": error.details,
                },
            }
        ), 422
    except CatalogPersistenceError as error:
        app.logger.error(
            "Catalog persistence error on PUT /pricing/catalog/<tid>/tiers: "
            "message=%s internal_details=%s",
            error.message,
            error.details,
        )
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="503").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return jsonify(
            {
                "success": False,
                "error": {
                    "code": "CATALOG_PERSISTENCE_ERROR",
                    "message": (
                        "The pricing catalog backend is temporarily unavailable."
                    ),
                    "retryable": True,
                    "details": {},
                },
            }
        ), 503

    REQUEST_COUNT.labels(endpoint=endpoint_label, status="200").inc()
    REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
    return jsonify(
        {
            "success": True,
            "data": {
                "template_id": template_id,
                "tiers": [
                    {
                        "tier_code": row["tier_code"],
                        "catalog_entry_id": row["catalog_entry_id"],
                        "effective_version": row["effective_version"],
                        "product_line": row["product_line"],
                        "mode": row.get("mode"),
                        "quality": row.get("quality"),
                        "flash_pro_axis": row["flash_pro_axis"],
                        "manual_price": row["manual_price"],
                        "pro_surcharge_display": row.get("pro_surcharge_display"),
                        "system_reference_price": row["system_reference_price"],
                        "currency_unit": row["currency_unit"],
                        "raw_rate": str(row["raw_rate"]),
                        "final_rate": str(row["final_rate"]),
                        "rounding_rule_version": row["rounding_rule_version"],
                        "manual_override_warning": row["manual_override_warning"],
                        "enabled": row["enabled"],
                        "updated_by": row["updated_by"],
                    }
                    for row in inserted
                ],
            },
        }
    )


# ── GET /pricing/catalog/<template_id>/history — full version timeline


@app.route("/pricing/catalog/<template_id>/history", methods=["GET"])
def pricing_catalog_v2_history(template_id: str):
    endpoint_label = "pricing-catalog-v2-history"
    start = time.time()

    auth_error = require_pricing_bff_auth()
    if auth_error is not None:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status=str(auth_error[1])).inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return auth_error

    user_auth_error = require_web_user_auth(get_db_core_connection)
    if user_auth_error is not None:
        REQUEST_COUNT.labels(
            endpoint=endpoint_label, status=str(user_auth_error[1])
        ).inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return user_auth_error

    conn = None
    try:
        try:
            conn = get_db_core_connection()
        except SQLAlchemyError as error:
            raise CatalogPersistenceError(
                "Failed to acquire a catalog DB connection.",
                details={"error_class": error.__class__.__name__},
            ) from error
        try:
            grouped = list_all_versions(conn, template_id)
        finally:
            conn.close()
    except CatalogPersistenceError as error:
        app.logger.error(
            "Catalog persistence error on GET /pricing/catalog/<tid>/history: "
            "message=%s internal_details=%s",
            error.message,
            error.details,
        )
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="503").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return jsonify(
            {
                "success": False,
                "error": {
                    "code": "CATALOG_PERSISTENCE_ERROR",
                    "message": (
                        "The pricing catalog backend is temporarily unavailable."
                    ),
                    "retryable": True,
                    "details": {},
                },
            }
        ), 503

    if not grouped:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="404").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return jsonify(
            {
                "success": False,
                "error": {
                    "code": "CATALOG_TIER_MISSING",
                    "message": "No history exists for this template.",
                    "retryable": False,
                    "details": {"template_id": template_id},
                },
            }
        ), 404

    REQUEST_COUNT.labels(endpoint=endpoint_label, status="200").inc()
    REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
    return jsonify(
        {
            "success": True,
            "data": {
                "template_id": template_id,
                "tiers": {
                    tier_code: [
                        {
                            "catalog_entry_id": row["catalog_entry_id"],
                            "effective_version": int(row["effective_version"]),
                            "manual_price": int(row["manual_price"]),
                            "pro_surcharge_display": (
                                int(row["pro_surcharge_display"])
                                if row.get("pro_surcharge_display") is not None
                                else None
                            ),
                            "system_reference_price": int(
                                row["system_reference_price"]
                            ),
                            "raw_rate": str(row["raw_rate"]),
                            "final_rate": str(row["final_rate"]),
                            "rounding_rule_version": row["rounding_rule_version"],
                            "manual_override_warning": bool(
                                row["manual_override_warning"]
                            ),
                            "enabled": bool(row["enabled"]),
                            "created_at": row["created_at"].isoformat()
                            if hasattr(row["created_at"], "isoformat")
                            else str(row["created_at"]),
                            "updated_at": row["updated_at"].isoformat()
                            if hasattr(row["updated_at"], "isoformat")
                            else str(row["updated_at"]),
                            "updated_by": row["updated_by"],
                        }
                        for row in rows
                    ]
                    for tier_code, rows in grouped.items()
                },
            },
        }
    )


# ── POST /pricing/quote — template price v2 quote endpoint  ───────────────


@app.route("/pricing/quote", methods=["POST"])
def pricing_quote_v2_create():
    endpoint_label = "pricing-quote-v2"
    start = time.time()

    auth_error = require_pricing_bff_auth()
    if auth_error is not None:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status=str(auth_error[1])).inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return auth_error

    user_auth_error = require_web_user_auth(get_db_core_connection)
    if user_auth_error is not None:
        REQUEST_COUNT.labels(
            endpoint=endpoint_label, status=str(user_auth_error[1])
        ).inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return user_auth_error

    try:
        body = request.get_json(silent=False)
    except (BadRequest, UnsupportedMediaType):
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="400").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return jsonify(
            {
                "success": False,
                "error": {
                    "code": "BAD_REQUEST",
                    "message": "Request body must be valid JSON.",
                    "retryable": False,
                    "details": {},
                },
            }
        ), 400
    if not isinstance(body, dict):
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="400").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return jsonify(
            {
                "success": False,
                "error": {
                    "code": "BAD_REQUEST",
                    "message": "Request body must be a JSON object.",
                    "retryable": False,
                    "details": {},
                },
            }
        ), 400

    app_key = request.headers.get("X-Web-App-Key", "")
    conn = None
    try:
        try:
            conn = get_db_core_connection()
        except SQLAlchemyError as error:
            raise QuotePersistenceError(
                "Failed to acquire DB connection.",
                details={"error_class": error.__class__.__name__},
            ) from error
        try:
            user_id = _lookup_user_id(conn, app_key)
            if user_id is None:
                REQUEST_COUNT.labels(endpoint=endpoint_label, status="401").inc()
                REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(
                    time.time() - start
                )
                return jsonify(
                    {
                        "success": False,
                        "error": {
                            "code": "WEB_APP_KEY_UNKNOWN",
                            "message": "X-Web-App-Key is not recognized.",
                            "retryable": False,
                            "details": {},
                        },
                    }
                ), 401

            quote = generate_quote(conn, request_body=body, user_id=user_id)
            conn.commit()
        finally:
            conn.close()
    except CatalogTierMissing as error:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="404").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return jsonify(
            {
                "success": False,
                "error": {
                    "code": "CATALOG_TIER_MISSING",
                    "message": "No catalog entry for this template.",
                    "retryable": False,
                    "details": {"template_id": error.template_id},
                },
            }
        ), 404
    except QuoteValidationError as error:
        REQUEST_COUNT.labels(
            endpoint=endpoint_label, status=str(error.http_status)
        ).inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return jsonify(
            {
                "success": False,
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "retryable": False,
                    "details": error.details,
                },
            }
        ), error.http_status
    except QuoteBreakdownMismatch as error:
        # Server-side invariant; the caller should never see this in
        # practice. Log loudly + return 500 with no internal details.
        app.logger.error(
            "Quote breakdown mismatch — expected=%s breakdown=%s",
            error.expected_total,
            error.breakdown_total,
        )
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="500").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return jsonify(
            {
                "success": False,
                "error": {
                    "code": "QUOTE_BREAKDOWN_MISMATCH",
                    "message": "Server-side quote invariant violated.",
                    "retryable": False,
                    "details": {},
                },
            }
        ), 500
    except (QuotePersistenceError, CatalogPersistenceError) as error:
        app.logger.error(
            "Pricing persistence error on POST /pricing/quote: details=%s",
            error.details,
        )
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="503").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return jsonify(
            {
                "success": False,
                "error": {
                    "code": "QUOTE_PERSISTENCE_ERROR",
                    "message": "The pricing backend is temporarily unavailable.",
                    "retryable": True,
                    "details": {},
                },
            }
        ), 503

    REQUEST_COUNT.labels(endpoint=endpoint_label, status="200").inc()
    REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
    return jsonify(
        {
            "success": True,
            "data": {
                "quote_id": quote.quote_id,
                "pricing_rule_version": quote.pricing_rule_version,
                "price_source": quote.price_source,
                "template_id": quote.template_id,
                "code": quote.code,
                "custom_template_id": quote.custom_template_id,
                "combo_key": quote.combo_key,
                "starting_price": quote.starting_price,
                "final_charge_price": quote.final_charge_price,
                "flash_total": quote.flash_total,
                "pro_total": quote.pro_total,
                "pro_upgrade_delta": quote.pro_upgrade_delta,
                "pricing_minutes": float(quote.pricing_minutes),
                "valid_line_count": quote.valid_line_count,
                "breakdown": quote.breakdown,
                "expires_at": quote.expires_at.isoformat(),
                "currency_unit": quote.currency_unit,
            },
        }
    )


# ── /cloud-drive/* wrappers (the implementation requirement) ────────────────────────────────────
#
# The internal cloud-drive API gives Web one expandable master account. These
# routes add the missing Web-user boundary: Bearer service auth, X-Web-App-Key
# identity, per-user quota, and file_id ownership.


def _cloud_drive_error(
    code: str,
    message: str,
    status: int,
    *,
    retryable: bool,
    details: dict | None = None,
):
    return (
        jsonify(
            {
                "success": False,
                "error": {
                    "code": code,
                    "message": message,
                    "retryable": retryable,
                    "details": details or {},
                },
            }
        ),
        status,
    )


def _record_cloud_drive(label: str, start: float, status: int | str) -> None:
    REQUEST_COUNT.labels(endpoint=label, status=str(status)).inc()
    REQUEST_LATENCY.labels(endpoint=label).observe(time.time() - start)


def _cloud_drive_auth_error(label: str, start: float):
    auth_error = require_pricing_bff_auth()
    if auth_error is not None:
        _record_cloud_drive(label, start, auth_error[1])
        return auth_error

    user_auth_error = require_web_user_auth(get_db_core_connection)
    if user_auth_error is not None:
        _record_cloud_drive(label, start, user_auth_error[1])
        return user_auth_error

    return None


def _cloud_drive_json_body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise CloudDriveStoreError(
            400,
            "BAD_REQUEST",
            "Request body must be a JSON object.",
            retryable=False,
        )
    return body


def _parse_cloud_drive_page(default_limit_key: str = "page_size"):
    raw_page = request.args.get("page", "1")
    raw_limit = request.args.get(default_limit_key, request.args.get("limit", "20"))
    try:
        page = int(raw_page)
        limit = int(raw_limit)
    except (TypeError, ValueError):
        raise CloudDriveStoreError(
            400,
            "BAD_REQUEST",
            "page and limit must be integers.",
            retryable=False,
        )
    if page < 1:
        raise CloudDriveStoreError(
            400, "BAD_REQUEST", "page must be >= 1.", retryable=False
        )
    if limit < 1 or limit > 9999:
        raise CloudDriveStoreError(
            400,
            "BAD_REQUEST",
            "limit must be between 1 and 9999.",
            retryable=False,
        )
    return page, limit


_UPSTREAM_BUSINESS_FALLBACK_MESSAGE = "Internal cloud-drive returned a business error."


def _coerce_upstream_user_message(payload: dict) -> str:
    # Surface the upstream user-facing message when present so the web
    # mapper can show it directly to the user instead of a generic
    # English fallback . Upstream APIs use either `message` or
    # `msg`; both are accepted.
    for key in ("message", "msg"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _UPSTREAM_BUSINESS_FALLBACK_MESSAGE


def _extract_upstream_data(payload):
    if isinstance(payload, dict) and "code" in payload and payload.get("code") != 10000:
        raise CloudDriveStoreError(
            502,
            "UPSTREAM_BUSINESS_ERROR",
            _coerce_upstream_user_message(payload),
            retryable=False,
            details={"upstream_payload": payload},
        )
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _find_file_id(value):
    if isinstance(value, dict):
        for key in ("file_id", "id"):
            candidate = value.get(key)
            if candidate:
                return str(candidate)
        for nested_key in ("data", "item", "file"):
            nested = _find_file_id(value.get(nested_key))
            if nested:
                return nested
    if isinstance(value, list) and value:
        return _find_file_id(value[0])
    return None


def _find_transfer_file_id(value):
    return _find_file_id(value) or _str_from_payload(value, ("upload_id", "task_id"))


def _find_object_key(value):
    if isinstance(value, dict):
        candidate = value.get("object_key")
        if candidate:
            return str(candidate)
        for nested_key in ("data", "item", "file"):
            nested = _find_object_key(value.get(nested_key))
            if nested:
                return nested
    if isinstance(value, list) and value:
        return _find_object_key(value[0])
    return None


def _int_from_payload(value, keys, default=0):
    if isinstance(value, dict):
        for key in keys:
            raw = value.get(key)
            if raw is not None and raw != "":
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    return default
        for nested_key in ("data", "item", "file"):
            nested = _int_from_payload(value.get(nested_key), keys, default=None)
            if nested is not None:
                return nested
    if isinstance(value, list) and value:
        return _int_from_payload(value[0], keys, default)
    return default


def _str_from_payload(value, keys):
    if isinstance(value, dict):
        for key in keys:
            raw = value.get(key)
            if raw:
                return str(raw)
        for nested_key in ("data", "item", "file"):
            nested = _str_from_payload(value.get(nested_key), keys)
            if nested:
                return nested
    if isinstance(value, list) and value:
        return _str_from_payload(value[0], keys)
    return None


def _transfer_items_from_payload(value):
    if isinstance(value, dict):
        for key in ("data", "items", "list", "records", "tasks", "files"):
            raw = value.get(key)
            if isinstance(raw, list):
                return raw
            nested = _transfer_items_from_payload(raw)
            if nested:
                return nested
        if _find_transfer_file_id(value):
            return [value]
    if isinstance(value, list):
        return value
    return []


def _lookup_cloud_drive_user(conn, app_key: str):
    user_id = _lookup_user_id(conn, app_key)
    if user_id is None:
        raise CloudDriveStoreError(
            401,
            "WEB_APP_KEY_UNKNOWN",
            "X-Web-App-Key is not recognized.",
            retryable=False,
        )
    return user_id


def _handle_cloud_drive_store_error(label, start, error):
    _record_cloud_drive(label, start, error.http_status)
    return _cloud_drive_error(
        error.code,
        error.message,
        error.http_status,
        retryable=error.retryable,
        details=error.details,
    )


def _handle_cloud_drive_upstream_error(label, start, error):
    _record_cloud_drive(label, start, error.http_status)
    return _cloud_drive_error(
        error.code,
        error.message,
        error.http_status,
        retryable=error.retryable,
        details=error.details,
    )


@app.route("/cloud-drive/upload-url", methods=["POST"])
def cloud_drive_upload_url():
    start = time.time()
    label = "cloud-drive-upload-url"
    auth_response = _cloud_drive_auth_error(label, start)
    if auth_response is not None:
        return auth_response
    app_key = request.headers.get("X-Web-App-Key", "")

    reservation_id = None
    try:
        body = _cloud_drive_json_body()
        file_name = body.get("file_name")
        try:
            file_size = int(body.get("file_size") or 0)
        except (TypeError, ValueError) as error:
            raise CloudDriveStoreError(
                400,
                "BAD_REQUEST",
                "file_size must be a positive integer.",
                retryable=False,
            ) from error
        content_type = body.get("content_type") or "application/octet-stream"
        if not isinstance(file_name, str) or not file_name.strip():
            raise CloudDriveStoreError(
                400,
                "BAD_REQUEST",
                "file_name is required.",
                retryable=False,
            )

        conn = get_db_core_connection()
        try:
            user_id = _lookup_cloud_drive_user(conn, app_key)
            reservation = reserve_local_upload(
                conn,
                user_id=user_id,
                app_key=app_key,
                file_name=file_name.strip(),
                file_size=file_size,
                content_type=content_type,
            )
            reservation_id = reservation["reservation_id"]
            conn.commit()
        finally:
            conn.close()

        upstream_payload = call_cloud_drive_upstream(
            "POST",
            "/v2/files/upload/presigned-url",
            body={
                "file_name": file_name.strip(),
                "file_size": file_size,
                "content_type": content_type,
            },
        )
        data = _extract_upstream_data(upstream_payload)
        file_id = _find_file_id(data)
        if not file_id:
            raise CloudDriveStoreError(
                502,
                "UPSTREAM_SCHEMA_ERROR",
                "Internal cloud-drive upload-url response is missing file_id.",
                retryable=False,
            )
        object_key = _find_object_key(data)

        conn = get_db_core_connection()
        try:
            attach_upload_file_id(
                conn,
                reservation_id=reservation_id,
                file_id=file_id,
                object_key=object_key,
                upstream_payload=upstream_payload,
            )
            conn.commit()
        finally:
            conn.close()
    except CloudDriveStoreError as error:
        if reservation_id:
            _mark_cloud_drive_reservation_failed(
                reservation_id,
                error_message=error.message,
                error_code=error.code,
            )
        return _handle_cloud_drive_store_error(label, start, error)
    except UpstreamCloudDriveError as error:
        if reservation_id:
            _mark_cloud_drive_reservation_failed(
                reservation_id,
                error_message=error.message,
                error_code=error.code,
            )
        return _handle_cloud_drive_upstream_error(label, start, error)
    except Exception:
        if reservation_id:
            _mark_cloud_drive_reservation_failed(
                reservation_id,
                error_message="云盘服务暂时不可用，请稍后重试",
                error_code="INTERNAL_ERROR",
            )
        _record_cloud_drive(label, start, 503)
        return _cloud_drive_error(
            "CLOUD_DRIVE_DB_UNAVAILABLE",
            "Cloud-drive store is temporarily unavailable.",
            503,
            retryable=True,
        )

    _record_cloud_drive(label, start, 200)
    return jsonify({"success": True, "data": data})


def _mark_cloud_drive_reservation_failed(
    reservation_id: str,
    *,
    error_message: str | None = None,
    error_code: str | None = None,
) -> None:
    try:
        conn = get_db_core_connection()
        try:
            mark_reservation_failed(
                conn,
                reservation_id=reservation_id,
                error_message=error_message,
                error_code=error_code,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        DB_ERRORS.labels(surface="cloud_drive", code="reservation_cleanup_failed").inc()


@app.route("/cloud-drive/upload-callback", methods=["POST"])
def cloud_drive_upload_callback():
    start = time.time()
    label = "cloud-drive-upload-callback"
    auth_response = _cloud_drive_auth_error(label, start)
    if auth_response is not None:
        return auth_response
    app_key = request.headers.get("X-Web-App-Key", "")

    try:
        body = _cloud_drive_json_body()
        file_id = body.get("file_id")
        object_key = body.get("object_key")
        upload_status = body.get("upload_status") or "success"
        if not file_id or not object_key:
            raise CloudDriveStoreError(
                400,
                "BAD_REQUEST",
                "file_id and object_key are required.",
                retryable=False,
            )
        callback_file_size = None
        if body.get("file_size") not in (None, ""):
            callback_file_size = int(body.get("file_size"))
            if callback_file_size < 0:
                raise CloudDriveStoreError(
                    400,
                    "BAD_REQUEST",
                    "file_size must be a non-negative integer.",
                    retryable=False,
                )
        hash_value = (
            body.get("srt_file_hash") or body.get("sha256") or body.get("file_sha256")
        )

        conn = get_db_core_connection()
        try:
            user_id = _lookup_cloud_drive_user(conn, app_key)
            reservation = get_local_upload_reservation(
                conn, user_id=user_id, file_id=str(file_id)
            )
            if reservation is None:
                _record_cloud_drive(label, start, 404)
                return _cloud_drive_error(
                    "NOT_FOUND",
                    "File reservation not found.",
                    404,
                    retryable=False,
                )
            reservation_object_key = reservation.get("object_key")
            if reservation_object_key and str(object_key) != str(
                reservation_object_key
            ):
                raise CloudDriveStoreError(
                    400,
                    "BAD_REQUEST",
                    "object_key does not match the upload reservation.",
                    retryable=False,
                )
            object_key = reservation_object_key or str(object_key)
            file_name = reservation["file_name"]
            reserved_size = int(reservation["file_size"] or 0)
            final_size = max(reserved_size, callback_file_size or reserved_size)
            reservation_suffix = reservation["suffix"] or suffix_for(str(file_name))
            if upload_status == "success" and reservation_suffix == "srt":
                if not normalize_sha256(hash_value):
                    raise CloudDriveStoreError(
                        400,
                        "BAD_REQUEST",
                        "srt_file_hash is required for local SRT uploads.",
                        retryable=False,
                    )

            if upload_status == "success":
                usage = cd_get_storage_usage(conn, user_id=user_id)
                projected = usage["used_size"] - reserved_size + final_size
                if projected > usage["max_size"]:
                    raise CloudDriveStoreError(
                        409,
                        "CLOUD_DRIVE_QUOTA_EXCEEDED",
                        "空间不足，请联系部署管理员",
                        retryable=False,
                        details={
                            "used_size": usage["used_size"],
                            "max_size": usage["max_size"],
                            "requested_size": final_size,
                        },
                    )
        finally:
            conn.close()

        if upload_status == "success":
            upstream_payload = call_cloud_drive_upstream(
                "POST",
                "/v2/files/upload/callback",
                body={
                    "upload_status": upload_status,
                    "file_size": final_size,
                    "file_name": file_name,
                    "file_id": file_id,
                    "object_key": object_key,
                    "upload_url": body.get("upload_url"),
                    "expires_in": body.get("expires_in"),
                    "upload_directory": body.get("upload_directory"),
                },
            )
        else:
            upstream_payload = {"upload_status": upload_status}

        data = _extract_upstream_data(upstream_payload)

        conn = get_db_core_connection()
        try:
            user_id = _lookup_cloud_drive_user(conn, app_key)
            result = complete_upload_callback(
                conn,
                user_id=user_id,
                file_id=str(file_id),
                object_key=str(object_key),
                upload_status=str(upload_status),
                srt_file_hash=hash_value,
                upstream_payload=upstream_payload,
                callback_file_size=callback_file_size,
            )
            if result is None:
                conn.close()
                _record_cloud_drive(label, start, 404)
                return _cloud_drive_error(
                    "NOT_FOUND",
                    "File reservation not found.",
                    404,
                    retryable=False,
                )
            conn.commit()
        finally:
            conn.close()
    except UpstreamCloudDriveError as error:
        return _handle_cloud_drive_upstream_error(label, start, error)
    except (TypeError, ValueError):
        return _handle_cloud_drive_store_error(
            label,
            start,
            CloudDriveStoreError(
                400,
                "BAD_REQUEST",
                "file_size must be an integer.",
                retryable=False,
            ),
        )
    except CloudDriveStoreError as error:
        return _handle_cloud_drive_store_error(label, start, error)
    except Exception:
        _record_cloud_drive(label, start, 503)
        return _cloud_drive_error(
            "CLOUD_DRIVE_DB_UNAVAILABLE",
            "Cloud-drive store is temporarily unavailable.",
            503,
            retryable=True,
        )

    _record_cloud_drive(label, start, 200)
    return jsonify({"success": True, "data": data})


def _restore_cloud_drive_file(app_key: str, file_id: str, previous_status: str) -> None:
    try:
        conn = get_db_core_connection()
        try:
            user_id = _lookup_user_id(conn, app_key)
            if user_id is not None:
                restore_file_status(
                    conn,
                    user_id=user_id,
                    file_id=file_id,
                    status=previous_status,
                )
                conn.commit()
        finally:
            conn.close()
    except Exception:
        DB_ERRORS.labels(surface="cloud_drive", code="delete_restore_failed").inc()


def _finalize_cloud_drive_file_delete(app_key: str, file_id: str) -> None:
    conn = get_db_core_connection()
    try:
        user_id = _lookup_cloud_drive_user(conn, app_key)
        finalize_delete_file(conn, user_id=user_id, file_id=file_id)
        conn.commit()
    finally:
        conn.close()


def _finalize_cloud_drive_files_delete(app_key: str, file_ids: list[str]) -> None:
    conn = get_db_core_connection()
    try:
        user_id = _lookup_cloud_drive_user(conn, app_key)
        finalize_delete_files(conn, user_id=user_id, file_ids=file_ids)
        conn.commit()
    finally:
        conn.close()


def _delete_upstream_file_best_effort(file_id: str | None) -> None:
    if not file_id:
        return
    try:
        call_cloud_drive_upstream("DELETE", f"/v2/files/user/files/{file_id}")
    except Exception:
        DB_ERRORS.labels(surface="cloud_drive", code="upstream_cleanup_failed").inc()


def _cloud_drive_transfer_upstream_config() -> UpstreamConfig:
    cfg = load_upstream_config()
    timeout_raw = os.environ.get("OPEN_FASTAPI_TRANSFER_TIMEOUT_SECONDS", "60")
    try:
        timeout = float(timeout_raw)
    except ValueError:
        timeout = 60.0
    if timeout <= 0:
        timeout = 60.0
    return UpstreamConfig(
        base_url=cfg.base_url,
        app_key=cfg.app_key,
        timeout_seconds=timeout,
    )


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


class _CloudDriveTransferSubmissionSlot:
    def __init__(self, executor, semaphore):
        self.executor = executor
        self._semaphore = semaphore
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._semaphore.release()
        self._released = True


def _cloud_drive_transfer_executor_state():
    global _cloud_drive_transfer_executor
    global _cloud_drive_transfer_slots
    global _cloud_drive_transfer_executor_config

    workers = _positive_int_env("CLOUD_DRIVE_TRANSFER_WORKERS", 2)
    queue_size = _positive_int_env("CLOUD_DRIVE_TRANSFER_QUEUE_SIZE", 20)
    desired_config = (workers, queue_size)

    with _cloud_drive_transfer_executor_lock:
        if (
            _cloud_drive_transfer_executor is None
            or _cloud_drive_transfer_slots is None
            or _cloud_drive_transfer_executor_config != desired_config
        ):
            if _cloud_drive_transfer_executor is not None:
                _cloud_drive_transfer_executor.shutdown(
                    wait=False, cancel_futures=True
                )
            _cloud_drive_transfer_executor = ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="cloud-drive-transfer",
            )
            _cloud_drive_transfer_slots = threading.BoundedSemaphore(
                workers + queue_size
            )
            _cloud_drive_transfer_executor_config = desired_config
        return _cloud_drive_transfer_executor, _cloud_drive_transfer_slots


def _reserve_cloud_drive_transfer_submission_slot():
    executor, slots = _cloud_drive_transfer_executor_state()
    if not slots.acquire(blocking=False):
        raise CloudDriveStoreError(
            429,
            "TRANSFER_QUEUE_FULL",
            "Cloud-drive transfer queue is full. Please retry later.",
            retryable=True,
        )
    return _CloudDriveTransferSubmissionSlot(executor, slots)


def _run_cloud_drive_transfer_submission(
    *, app_key: str, reservation_id: str, link: str
) -> None:
    try:
        upstream_payload = call_cloud_drive_upstream(
            "POST",
            "/v2/files/upload",
            body={"link": link},
            config=_cloud_drive_transfer_upstream_config(),
        )
        data = _extract_upstream_data(upstream_payload)
        upload_id = _find_transfer_file_id(data)
        if not upload_id:
            raise CloudDriveStoreError(
                502,
                "UPSTREAM_SCHEMA_ERROR",
                "Internal cloud-drive transfer response is missing upload_id.",
                retryable=False,
            )
        conn = get_db_core_connection()
        try:
            user_id = _lookup_cloud_drive_user(conn, app_key)
            result = attach_link_upload_id(
                conn,
                user_id=user_id,
                reservation_id=reservation_id,
                upload_id=upload_id,
                file_name=_str_from_payload(
                    data, ("file_name", "original_name", "name")
                ),
                file_size=_int_from_payload(data, ("file_size", "size"), default=0),
                upstream_status=_int_from_payload(data, ("status",), default=0),
                progress=_int_from_payload(data, ("progress",), default=0),
                upstream_payload=upstream_payload,
            )
            if result is None:
                raise CloudDriveStoreError(
                    404,
                    "NOT_FOUND",
                    "Transfer reservation not found.",
                    retryable=False,
                )
            conn.commit()
        finally:
            if not conn.closed:
                conn.close()
    except CloudDriveStoreError as error:
        _mark_cloud_drive_reservation_failed(
            reservation_id,
            error_message=error.message,
            error_code=error.code,
        )
        DB_ERRORS.labels(surface="cloud_drive", code=error.code.lower()).inc()
    except UpstreamCloudDriveError as error:
        _mark_cloud_drive_reservation_failed(
            reservation_id,
            error_message=error.message,
            error_code=error.code,
        )
        DB_ERRORS.labels(surface="cloud_drive", code=error.code.lower()).inc()
    except Exception:
        _mark_cloud_drive_reservation_failed(
            reservation_id,
            error_message="云盘服务暂时不可用，请稍后重试",
            error_code="INTERNAL_ERROR",
        )
        DB_ERRORS.labels(surface="cloud_drive", code="transfer_submit_failed").inc()


def _enqueue_cloud_drive_transfer_submission(
    *,
    app_key: str,
    reservation_id: str,
    link: str,
    slot: _CloudDriveTransferSubmissionSlot | None = None,
) -> None:
    submission_slot = slot or _reserve_cloud_drive_transfer_submission_slot()

    def run_with_release():
        try:
            _run_cloud_drive_transfer_submission(
                app_key=app_key, reservation_id=reservation_id, link=link
            )
        finally:
            submission_slot.release()

    try:
        submission_slot.executor.submit(run_with_release)
    except Exception as error:
        submission_slot.release()
        raise CloudDriveStoreError(
            503,
            "TRANSFER_QUEUE_UNAVAILABLE",
            "Cloud-drive transfer queue is temporarily unavailable.",
            retryable=True,
        ) from error



def _refresh_cloud_drive_transfer_records(user_id: int) -> None:
    # regression coverage fan-out: each link reservation carries an `upload_id`; the
    # upstream filelist exposes the resulting child files keyed by that
    # `upload_id`. Once every child has reached a terminal status
    # (2/3/4), atomically insert per-file rows + mark the parent
    # settled. Mid-flight parents are skipped — next refresh tries
    # again.
    conn = get_db_core_connection()
    try:
        unsettled = list_unsettled_link_parents(conn, user_id=user_id)
    finally:
        conn.close()
    if not unsettled:
        return

    for parent in unsettled:
        try:
            upstream_payload = call_cloud_drive_upstream(
                "GET",
                "/v2/files/user/filelist",
                query={
                    "page": 1,
                    "limit": 100,
                    "order": "desc",
                    "order_by": "created_at",
                    "upload_id": parent["upload_id"],
                },
            )
        except UpstreamCloudDriveError:
            DB_ERRORS.labels(
                surface="cloud_drive", code="transfer_refresh_failed"
            ).inc()
            continue
        data = _extract_upstream_data(upstream_payload)
        items = _transfer_items_from_payload(data)
        if not items:
            continue
        all_terminal = all(
            _int_from_payload(item, ("status",), default=0) in TERMINAL_UPSTREAM_STATUSES
            for item in items
        )
        if not all_terminal:
            continue

        children = []
        for item in items:
            child_file_id = _str_from_payload(item, ("file_id",))
            if not child_file_id:
                continue
            children.append(
                {
                    "file_id": child_file_id,
                    "file_name": _str_from_payload(
                        item, ("file_name", "original_name", "name")
                    )
                    or "transfer",
                    "file_size": _int_from_payload(
                        item, ("file_size", "size"), default=0
                    ),
                    "upstream_status": _int_from_payload(
                        item, ("status",), default=0
                    ),
                    "progress": _int_from_payload(item, ("progress",), default=0),
                    "upstream_payload": item,
                }
            )
        if not children:
            continue

        conn = get_db_core_connection()
        try:
            settle_link_parent_with_children(
                conn,
                user_id=user_id,
                app_key=parent["app_key"],
                parent_reservation_id=parent["reservation_id"],
                upload_id=parent["upload_id"],
                reserved_parent_size=parent["reserved_size"],
                children=children,
            )
            conn.commit()
        except CloudDriveStoreError as error:
            DB_ERRORS.labels(
                surface="cloud_drive", code=error.code.lower()
            ).inc()
        finally:
            if not conn.closed:
                conn.close()


@app.route("/cloud-drive/download-url", methods=["POST"])
def cloud_drive_download_url():
    start = time.time()
    label = "cloud-drive-download-url"
    auth_response = _cloud_drive_auth_error(label, start)
    if auth_response is not None:
        return auth_response
    app_key = request.headers.get("X-Web-App-Key", "")
    try:
        body = _cloud_drive_json_body()
        file_id = body.get("file_id")
        if not file_id:
            raise CloudDriveStoreError(
                400, "BAD_REQUEST", "file_id is required.", retryable=False
            )
        conn = get_db_core_connection()
        try:
            user_id = _lookup_cloud_drive_user(conn, app_key)
            owned = cd_get_file(conn, user_id=user_id, file_id=str(file_id))
        finally:
            conn.close()
        if owned is None:
            _record_cloud_drive(label, start, 404)
            return _cloud_drive_error(
                "NOT_FOUND", "File not found.", 404, retryable=False
            )

        upstream_payload = call_cloud_drive_upstream(
            "POST",
            "/v2/files/download/presigned-url",
            body={"file_id": file_id},
        )
        data = _extract_upstream_data(upstream_payload)
    except CloudDriveStoreError as error:
        return _handle_cloud_drive_store_error(label, start, error)
    except UpstreamCloudDriveError as error:
        return _handle_cloud_drive_upstream_error(label, start, error)
    except Exception:
        _record_cloud_drive(label, start, 503)
        return _cloud_drive_error(
            "CLOUD_DRIVE_DB_UNAVAILABLE",
            "Cloud-drive store is temporarily unavailable.",
            503,
            retryable=True,
        )
    _record_cloud_drive(label, start, 200)
    return jsonify({"success": True, "data": data})


@app.route("/cloud-drive/files", methods=["GET"])
def cloud_drive_files_list():
    start = time.time()
    label = "cloud-drive-files-list"
    auth_response = _cloud_drive_auth_error(label, start)
    if auth_response is not None:
        return auth_response
    app_key = request.headers.get("X-Web-App-Key", "")
    try:
        page, page_size = _parse_cloud_drive_page("page_size")
        order_by = request.args.get("order_by") or "created_at"
        order = request.args.get("order") or "desc"
        search = request.args.get("search") or ""
        conn = get_db_core_connection()
        try:
            user_id = _lookup_cloud_drive_user(conn, app_key)
            result = cd_list_files(
                conn,
                user_id=user_id,
                page=page,
                page_size=page_size,
                order_by=order_by,
                order=order,
                search=search,
            )
        finally:
            conn.close()
    except CloudDriveStoreError as error:
        return _handle_cloud_drive_store_error(label, start, error)
    except Exception:
        _record_cloud_drive(label, start, 503)
        return _cloud_drive_error(
            "CLOUD_DRIVE_DB_UNAVAILABLE",
            "Cloud-drive store is temporarily unavailable.",
            503,
            retryable=True,
        )
    _record_cloud_drive(label, start, 200)
    return jsonify({"success": True, "data": result})


@app.route("/cloud-drive/files/<file_id>", methods=["DELETE"])
def cloud_drive_file_delete(file_id):
    start = time.time()
    label = "cloud-drive-file-delete"
    auth_response = _cloud_drive_auth_error(label, start)
    if auth_response is not None:
        return auth_response
    app_key = request.headers.get("X-Web-App-Key", "")
    previous_status = None
    upstream_delete_confirmed = False
    try:
        conn = get_db_core_connection()
        try:
            user_id = _lookup_cloud_drive_user(conn, app_key)
            deleted = soft_delete_file(conn, user_id=user_id, file_id=file_id)
            if deleted is None:
                conn.close()
                _record_cloud_drive(label, start, 404)
                return _cloud_drive_error(
                    "NOT_FOUND", "File not found.", 404, retryable=False
                )
            previous_status = deleted["previous_status"]
            conn.commit()
        finally:
            if not conn.closed:
                conn.close()

        upstream_payload = call_cloud_drive_upstream(
            "DELETE", f"/v2/files/user/files/{file_id}"
        )
        data = _extract_upstream_data(upstream_payload)
        upstream_delete_confirmed = True
        _finalize_cloud_drive_file_delete(app_key, file_id)
    except CloudDriveStoreError as error:
        if previous_status is not None and not upstream_delete_confirmed:
            _restore_cloud_drive_file(app_key, file_id, previous_status)
        return _handle_cloud_drive_store_error(label, start, error)
    except UpstreamCloudDriveError as error:
        _restore_cloud_drive_file(app_key, file_id, previous_status or "completed")
        return _handle_cloud_drive_upstream_error(label, start, error)
    except Exception:
        if previous_status is not None and not upstream_delete_confirmed:
            _restore_cloud_drive_file(app_key, file_id, previous_status)
        _record_cloud_drive(label, start, 503)
        return _cloud_drive_error(
            "CLOUD_DRIVE_DB_UNAVAILABLE",
            "Cloud-drive store is temporarily unavailable.",
            503,
            retryable=True,
        )
    _record_cloud_drive(label, start, 200)
    return jsonify({"success": True, "data": data})


@app.route("/cloud-drive/files/batch-delete", methods=["POST"])
def cloud_drive_files_batch_delete():
    start = time.time()
    label = "cloud-drive-files-batch-delete"
    auth_response = _cloud_drive_auth_error(label, start)
    if auth_response is not None:
        return auth_response
    app_key = request.headers.get("X-Web-App-Key", "")
    previous_statuses: dict[str, str] = {}
    upstream_file_ids: list[str] = []
    upstream_to_local: dict[str, str] = {}
    upstream_delete_confirmed = False
    try:
        body = _cloud_drive_json_body()
        file_ids = body.get("file_ids")
        if not isinstance(file_ids, list) or not file_ids or not all(file_ids):
            raise CloudDriveStoreError(
                400,
                "BAD_REQUEST",
                "file_ids must be a non-empty array.",
                retryable=False,
            )
        if len(file_ids) > 50:
            raise CloudDriveStoreError(
                400,
                "BAD_REQUEST",
                "file_ids cannot exceed 50 entries per request.",
                retryable=False,
            )
        # De-duplicate while preserving order.
        seen: set[str] = set()
        file_ids = [
            str(fid)
            for fid in file_ids
            if not (str(fid) in seen or seen.add(str(fid)))
        ]
        conn = get_db_core_connection()
        try:
            user_id = _lookup_cloud_drive_user(conn, app_key)
            owned_ids = owned_existing_file_ids(
                conn, user_id=user_id, file_ids=file_ids
            )
            if owned_ids != set(file_ids):
                conn.close()
                _record_cloud_drive(label, start, 404)
                return _cloud_drive_error(
                    "NOT_FOUND", "File not found.", 404, retryable=False
                )
            for file_id in file_ids:
                deleted = soft_delete_file(conn, user_id=user_id, file_id=file_id)
                if deleted is None:
                    conn.close()
                    _record_cloud_drive(label, start, 404)
                    return _cloud_drive_error(
                        "NOT_FOUND", "File not found.", 404, retryable=False
                    )
                previous_statuses[file_id] = deleted["previous_status"]
                upstream_fid = deleted.get("upstream_file_id")
                if upstream_fid:
                    upstream_file_ids.append(upstream_fid)
                    upstream_to_local[str(upstream_fid)] = file_id
            conn.commit()
        finally:
            if not conn.closed:
                conn.close()

        if upstream_file_ids:
            upstream_payload = call_cloud_drive_upstream(
                "POST",
                "/v2/files/user/files/batch-delete",
                body={"file_ids": upstream_file_ids},
            )
            data = _extract_upstream_data(upstream_payload)
            upstream_delete_confirmed = True
        else:
            data = {"deleted": True}

        # Honor upstream partial-success: keep files that upstream rejected
        # locally visible so the next /cloud-drive/files listing matches the
        # `failed_items` the frontend shows to the user.
        failed_local_ids: set[str] = set()
        if isinstance(data, dict):
            for item in data.get("failed_items") or []:
                if not isinstance(item, dict):
                    continue
                upstream_fid = item.get("file_id")
                if upstream_fid is None:
                    continue
                local_fid = upstream_to_local.get(str(upstream_fid))
                if local_fid is not None:
                    failed_local_ids.add(local_fid)
        for failed_id in failed_local_ids:
            _restore_cloud_drive_file(
                app_key, failed_id, previous_statuses[failed_id]
            )
        finalizable_ids = [fid for fid in file_ids if fid not in failed_local_ids]
        if finalizable_ids:
            _finalize_cloud_drive_files_delete(app_key, finalizable_ids)
    except CloudDriveStoreError as error:
        if not upstream_delete_confirmed:
            for file_id, previous_status in previous_statuses.items():
                _restore_cloud_drive_file(app_key, file_id, previous_status)
        return _handle_cloud_drive_store_error(label, start, error)
    except UpstreamCloudDriveError as error:
        for file_id, previous_status in previous_statuses.items():
            _restore_cloud_drive_file(app_key, file_id, previous_status)
        return _handle_cloud_drive_upstream_error(label, start, error)
    except Exception:
        if not upstream_delete_confirmed:
            for file_id, previous_status in previous_statuses.items():
                _restore_cloud_drive_file(app_key, file_id, previous_status)
        _record_cloud_drive(label, start, 503)
        return _cloud_drive_error(
            "CLOUD_DRIVE_DB_UNAVAILABLE",
            "Cloud-drive store is temporarily unavailable.",
            503,
            retryable=True,
        )
    _record_cloud_drive(label, start, 200)
    return jsonify({"success": True, "data": data})


@app.route("/cloud-drive/storage-usage", methods=["GET"])
def cloud_drive_storage_usage():
    start = time.time()
    label = "cloud-drive-storage-usage"
    auth_response = _cloud_drive_auth_error(label, start)
    if auth_response is not None:
        return auth_response
    app_key = request.headers.get("X-Web-App-Key", "")
    try:
        conn = get_db_core_connection()
        try:
            user_id = _lookup_cloud_drive_user(conn, app_key)
            result = cd_get_storage_usage(conn, user_id=user_id)
        finally:
            conn.close()
    except CloudDriveStoreError as error:
        return _handle_cloud_drive_store_error(label, start, error)
    except Exception:
        _record_cloud_drive(label, start, 503)
        return _cloud_drive_error(
            "CLOUD_DRIVE_DB_UNAVAILABLE",
            "Cloud-drive store is temporarily unavailable.",
            503,
            retryable=True,
        )
    _record_cloud_drive(label, start, 200)
    return jsonify({"success": True, "data": result})


@app.route("/cloud-drive/transfer", methods=["POST"])
def cloud_drive_transfer_create():
    start = time.time()
    label = "cloud-drive-transfer-create"
    auth_response = _cloud_drive_auth_error(label, start)
    if auth_response is not None:
        return auth_response
    app_key = request.headers.get("X-Web-App-Key", "")
    reservation_id = None
    file_id = None
    async_transfer_slot = None
    try:
        body = _cloud_drive_json_body()
        link = body.get("link")
        if not isinstance(link, str) or not link.strip():
            raise CloudDriveStoreError(
                400, "BAD_REQUEST", "link is required.", retryable=False
            )
        raw_file_size = body.get("file_size")
        if raw_file_size in (None, ""):
            file_size = 0
        else:
            try:
                file_size = int(raw_file_size)
            except (TypeError, ValueError) as error:
                raise CloudDriveStoreError(
                    400,
                    "BAD_REQUEST",
                    "file_size must be a positive integer when provided.",
                    retryable=False,
                ) from error
            if file_size <= 0:
                raise CloudDriveStoreError(
                    400,
                    "BAD_REQUEST",
                    "file_size must be a positive integer when provided.",
                    retryable=False,
                )
        if file_size == 0:
            async_transfer_slot = _reserve_cloud_drive_transfer_submission_slot()
        conn = get_db_core_connection()
        try:
            user_id = _lookup_cloud_drive_user(conn, app_key)
            reservation = create_transfer_record(
                conn,
                user_id=user_id,
                app_key=app_key,
                link=link.strip(),
                file_id=None,
                file_name=str(body["file_name"]) if body.get("file_name") else None,
                file_size=file_size,
                upstream_status=0,
                progress=0,
                upstream_payload={"link": link.strip()},
            )
            reservation_id = reservation["reservation_id"]
            conn.commit()
        finally:
            if not conn.closed:
                conn.close()

        if file_size == 0:
            file_name = str(body["file_name"]) if body.get("file_name") else "transfer"
            slot = async_transfer_slot
            async_transfer_slot = None
            _enqueue_cloud_drive_transfer_submission(
                app_key=app_key,
                reservation_id=reservation_id,
                link=link.strip(),
                slot=slot,
            )
            data = {
                "reservation_id": reservation_id,
                "file_id": reservation_id,
                "file_name": file_name,
                "file_size": 0,
                "status": 0,
                "progress": 0,
            }
            _record_cloud_drive(label, start, 200)
            return jsonify({"success": True, "data": data})

        upstream_payload = call_cloud_drive_upstream(
            "POST", "/v2/files/upload", body={"link": link.strip()}
        )
        data = _extract_upstream_data(upstream_payload)
        upload_id = _find_transfer_file_id(data)
        if not upload_id:
            raise CloudDriveStoreError(
                502,
                "UPSTREAM_SCHEMA_ERROR",
                "Internal cloud-drive transfer response is missing upload_id.",
                retryable=False,
            )
        conn = get_db_core_connection()
        try:
            user_id = _lookup_cloud_drive_user(conn, app_key)
            result = attach_link_upload_id(
                conn,
                user_id=user_id,
                reservation_id=reservation_id,
                upload_id=upload_id,
                file_name=_str_from_payload(
                    data, ("file_name", "original_name", "name")
                ),
                file_size=_int_from_payload(data, ("file_size", "size"), default=0),
                upstream_status=_int_from_payload(data, ("status",), default=0),
                progress=_int_from_payload(data, ("progress",), default=0),
                upstream_payload=upstream_payload,
            )
            if result is None:
                raise CloudDriveStoreError(
                    404,
                    "NOT_FOUND",
                    "Transfer reservation not found.",
                    retryable=False,
            )
            conn.commit()
        finally:
            if not conn.closed:
                conn.close()
    except CloudDriveStoreError as error:
        if async_transfer_slot is not None:
            async_transfer_slot.release()
        if reservation_id:
            _mark_cloud_drive_reservation_failed(
                reservation_id,
                error_message=error.message,
                error_code=error.code,
            )
        if file_id:
            _delete_upstream_file_best_effort(file_id)
        return _handle_cloud_drive_store_error(label, start, error)
    except UpstreamCloudDriveError as error:
        if async_transfer_slot is not None:
            async_transfer_slot.release()
        if reservation_id:
            _mark_cloud_drive_reservation_failed(
                reservation_id,
                error_message=error.message,
                error_code=error.code,
            )
        return _handle_cloud_drive_upstream_error(label, start, error)
    except Exception:
        if async_transfer_slot is not None:
            async_transfer_slot.release()
        if reservation_id:
            _mark_cloud_drive_reservation_failed(
                reservation_id,
                error_message="云盘服务暂时不可用，请稍后重试",
                error_code="INTERNAL_ERROR",
            )
        if file_id:
            _delete_upstream_file_best_effort(file_id)
        _record_cloud_drive(label, start, 503)
        return _cloud_drive_error(
            "CLOUD_DRIVE_DB_UNAVAILABLE",
            "Cloud-drive store is temporarily unavailable.",
            503,
            retryable=True,
        )
    _record_cloud_drive(label, start, 200)
    return jsonify({"success": True, "data": data})


@app.route("/cloud-drive/transfer", methods=["GET"])
def cloud_drive_transfer_list():
    start = time.time()
    label = "cloud-drive-transfer-list"
    auth_response = _cloud_drive_auth_error(label, start)
    if auth_response is not None:
        return auth_response
    app_key = request.headers.get("X-Web-App-Key", "")
    try:
        page, limit = _parse_cloud_drive_page("limit")
        raw_status = request.args.get("status")
        status = int(raw_status) if raw_status not in (None, "") else None
        order = request.args.get("order") or "desc"
        order_by = request.args.get("order_by") or "created_at"
        conn = get_db_core_connection()
        try:
            user_id = _lookup_cloud_drive_user(conn, app_key)
        finally:
            conn.close()

        try:
            _refresh_cloud_drive_transfer_records(user_id)
        except (CloudDriveStoreError, UpstreamCloudDriveError):
            DB_ERRORS.labels(
                surface="cloud_drive", code="transfer_refresh_failed"
            ).inc()

        conn = get_db_core_connection()
        try:
            result = cd_list_transfer_records(
                conn,
                user_id=user_id,
                page=page,
                limit=limit,
                status=status,
                order=order,
                order_by=order_by,
            )
        finally:
            conn.close()
    except (TypeError, ValueError):
        return _handle_cloud_drive_store_error(
            label,
            start,
            CloudDriveStoreError(
                400, "BAD_REQUEST", "status must be an integer.", retryable=False
            ),
        )
    except CloudDriveStoreError as error:
        return _handle_cloud_drive_store_error(label, start, error)
    except Exception:
        _record_cloud_drive(label, start, 503)
        return _cloud_drive_error(
            "CLOUD_DRIVE_DB_UNAVAILABLE",
            "Cloud-drive store is temporarily unavailable.",
            503,
            retryable=True,
        )
    _record_cloud_drive(label, start, 200)
    return jsonify({"success": True, "data": result})


@app.route("/cloud-drive/transfer/batch-delete", methods=["POST"])
def cloud_drive_transfer_batch_delete():
    start = time.time()
    label = "cloud-drive-transfer-batch-delete"
    auth_response = _cloud_drive_auth_error(label, start)
    if auth_response is not None:
        return auth_response
    app_key = request.headers.get("X-Web-App-Key", "")
    previous_statuses = {}
    upstream_file_ids = []
    upstream_delete_confirmed = False
    try:
        body = _cloud_drive_json_body()
        file_ids = body.get("file_ids") or body.get("file_id")
        if not isinstance(file_ids, list) or not all(file_ids):
            raise CloudDriveStoreError(
                400,
                "BAD_REQUEST",
                "file_ids must be a non-empty array.",
                retryable=False,
            )
        file_ids = [str(file_id) for file_id in file_ids]
        conn = get_db_core_connection()
        try:
            user_id = _lookup_cloud_drive_user(conn, app_key)
            owned_ids = owned_existing_file_ids(
                conn, user_id=user_id, file_ids=file_ids
            )
            if owned_ids != set(file_ids):
                conn.close()
                _record_cloud_drive(label, start, 404)
                return _cloud_drive_error(
                    "NOT_FOUND", "File not found.", 404, retryable=False
                )
            for file_id in file_ids:
                deleted = soft_delete_file(conn, user_id=user_id, file_id=file_id)
                if deleted is None:
                    conn.close()
                    _record_cloud_drive(label, start, 404)
                    return _cloud_drive_error(
                        "NOT_FOUND", "File not found.", 404, retryable=False
                    )
                previous_statuses[file_id] = deleted["previous_status"]
                if deleted.get("upstream_file_id"):
                    upstream_file_ids.append(deleted["upstream_file_id"])
            conn.commit()
        finally:
            if not conn.closed:
                conn.close()

        if upstream_file_ids:
            upstream_payload = call_cloud_drive_upstream(
                "POST",
                "/v2/files/user/files/batch-delete",
                body={"file_ids": upstream_file_ids},
            )
            data = _extract_upstream_data(upstream_payload)
            upstream_delete_confirmed = True
        else:
            data = {"deleted": True}
        _finalize_cloud_drive_files_delete(app_key, file_ids)
    except CloudDriveStoreError as error:
        if not upstream_delete_confirmed:
            for file_id, previous_status in previous_statuses.items():
                _restore_cloud_drive_file(app_key, file_id, previous_status)
        return _handle_cloud_drive_store_error(label, start, error)
    except UpstreamCloudDriveError as error:
        for file_id, previous_status in previous_statuses.items():
            _restore_cloud_drive_file(app_key, file_id, previous_status)
        return _handle_cloud_drive_upstream_error(label, start, error)
    except Exception:
        if not upstream_delete_confirmed:
            for file_id, previous_status in previous_statuses.items():
                _restore_cloud_drive_file(app_key, file_id, previous_status)
        _record_cloud_drive(label, start, 503)
        return _cloud_drive_error(
            "CLOUD_DRIVE_DB_UNAVAILABLE",
            "Cloud-drive store is temporarily unavailable.",
            503,
            retryable=True,
        )
    _record_cloud_drive(label, start, 200)
    return jsonify({"success": True, "data": data})


@app.route("/narrator/tasks", methods=["POST"])
def narrator_tasks_create():
    """Create a new master task (server-assigned ID + timestamps) or upsert
    a client-supplied one (preserving the original `narrator_task_id` and
    `created_at`). Upsert is required by the web-side step orchestrator,
    which mints IDs client-side and resubmits on retry.

    Cross-tenant upsert (the supplied `narrator_task_id` already exists
    but belongs to another `users.id`) returns 403 — no silent overwrite.
    """
    start = time.time()
    endpoint_label = "narrator-tasks-create"

    auth_error = require_web_user_auth(get_db_core_connection)
    if auth_error is not None:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status=str(auth_error[1])).inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return auth_error

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="400").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return _narrator_tasks_error(
            "BAD_REQUEST",
            "request body must be a JSON object.",
            400,
            retryable=False,
        )

    type_error = _validate_task_body(body)
    if type_error is not None:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="400").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return type_error

    app_key = request.headers.get("X-Web-App-Key", "")

    # quote_id is strictly typed: present iff a non-empty string. A
    # wrong-typed quote_id MUST NOT silently fall back to the v1
    # (snapshot-less) path — that would bypass the required quote ↔
    # snapshot billing linkage (regression coverage).
    quote_id: Optional[str] = None
    if "quote_id" in body:
        raw_quote_id = body.get("quote_id")
        if not isinstance(raw_quote_id, str) or not raw_quote_id:
            REQUEST_COUNT.labels(endpoint=endpoint_label, status="400").inc()
            REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
            return _narrator_tasks_error(
                "BAD_REQUEST",
                "quote_id must be a non-empty string when present.",
                400,
                retryable=False,
            )
        quote_id = raw_quote_id

    snapshot_id: Optional[str] = None
    try:
        conn = get_db_core_connection()
        try:
            user_id = _lookup_user_id(conn, app_key)
            if user_id is None:
                REQUEST_COUNT.labels(endpoint=endpoint_label, status="401").inc()
                REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(
                    time.time() - start
                )
                return _narrator_tasks_error(
                    "WEB_APP_KEY_UNKNOWN",
                    "X-Web-App-Key is not recognized.",
                    401,
                    retryable=False,
                )

            if body.get("narrator_task_id"):
                result = nt_upsert_task(
                    conn, user_id=user_id, app_key=app_key, body=body
                )
                if result is None:
                    REQUEST_COUNT.labels(endpoint=endpoint_label, status="403").inc()
                    REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(
                        time.time() - start
                    )
                    return _narrator_tasks_error(
                        "FORBIDDEN",
                        "narrator_task_id belongs to another tenant.",
                        403,
                        retryable=False,
                    )
            else:
                result = nt_create_task(
                    conn, user_id=user_id, app_key=app_key, body=body
                )

            if quote_id:
                master_task_id = (
                    result.get("narrator_task_id") if isinstance(result, dict) else None
                )
                if not master_task_id:
                    REQUEST_COUNT.labels(endpoint=endpoint_label, status="500").inc()
                    REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(
                        time.time() - start
                    )
                    return _narrator_tasks_error(
                        "INTERNAL_ERROR",
                        "Master task row missing narrator_task_id after insert.",
                        500,
                        retryable=False,
                    )
                snapshot_id = commit_master_task_snapshot(
                    conn,
                    quote_id=quote_id,
                    master_task_id=master_task_id,
                    request_body=body,
                    user_id=user_id,
                )

            conn.commit()
        finally:
            conn.close()
    except QuoteNotFound as error:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="404").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return jsonify(
            {
                "success": False,
                "error": {
                    "code": "QUOTE_NOT_FOUND",
                    "message": "quote_id was not recognized.",
                    "retryable": False,
                    "details": {"quote_id": error.quote_id},
                },
            }
        ), 404
    except QuoteValidationError as error:
        REQUEST_COUNT.labels(
            endpoint=endpoint_label, status=str(error.http_status)
        ).inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return jsonify(
            {
                "success": False,
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "retryable": False,
                    "details": error.details,
                },
            }
        ), error.http_status
    except QuotePersistenceError as error:
        app.logger.error(
            "Quote persistence error on POST /narrator/tasks: details=%s",
            error.details,
        )
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="503").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return _narrator_tasks_error(
            "QUOTE_PERSISTENCE_ERROR",
            "The pricing backend is temporarily unavailable.",
            503,
            retryable=True,
        )
    except Exception:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="503").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return _narrator_tasks_error(
            "NARRATOR_TASKS_DB_UNAVAILABLE",
            "Narrator tasks store is temporarily unavailable.",
            503,
            retryable=True,
        )

    REQUEST_COUNT.labels(endpoint=endpoint_label, status="200").inc()
    REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
    response_data = dict(result) if isinstance(result, dict) else result
    if snapshot_id and isinstance(response_data, dict):
        response_data["snapshot_id"] = snapshot_id
    return jsonify({"success": True, "data": response_data})


@app.route("/narrator/tasks", methods=["GET"])
def narrator_tasks_list():
    """List the caller's master tasks. Paginated; optional `status` filter."""
    start = time.time()
    endpoint_label = "narrator-tasks-list"

    auth_error = require_web_user_auth(get_db_core_connection)
    if auth_error is not None:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status=str(auth_error[1])).inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return auth_error

    page, limit, pagination_error = _parse_pagination(request.args)
    if pagination_error is not None:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="400").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return pagination_error

    status_filter = request.args.get("status") or None
    app_key = request.headers.get("X-Web-App-Key", "")

    try:
        conn = get_db_core_connection()
        try:
            user_id = _lookup_user_id(conn, app_key)
            if user_id is None:
                REQUEST_COUNT.labels(endpoint=endpoint_label, status="401").inc()
                REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(
                    time.time() - start
                )
                return _narrator_tasks_error(
                    "WEB_APP_KEY_UNKNOWN",
                    "X-Web-App-Key is not recognized.",
                    401,
                    retryable=False,
                )

            result = nt_list_tasks(
                conn,
                user_id=user_id,
                status=status_filter,
                page=page,
                limit=limit,
            )
        finally:
            conn.close()
    except Exception:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="503").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return _narrator_tasks_error(
            "NARRATOR_TASKS_DB_UNAVAILABLE",
            "Narrator tasks store is temporarily unavailable.",
            503,
            retryable=True,
        )

    REQUEST_COUNT.labels(endpoint=endpoint_label, status="200").inc()
    REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
    return jsonify({"success": True, "data": result})


@app.route("/narrator/tasks/<narrator_task_id>", methods=["GET"])
def narrator_tasks_get(narrator_task_id):
    """Fetch a single task scoped to the caller. 404 if missing OR owned
    by another user — existence isn't leaked."""
    start = time.time()
    endpoint_label = "narrator-tasks-get"

    auth_error = require_web_user_auth(get_db_core_connection)
    if auth_error is not None:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status=str(auth_error[1])).inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return auth_error

    app_key = request.headers.get("X-Web-App-Key", "")

    try:
        conn = get_db_core_connection()
        try:
            user_id = _lookup_user_id(conn, app_key)
            if user_id is None:
                REQUEST_COUNT.labels(endpoint=endpoint_label, status="401").inc()
                REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(
                    time.time() - start
                )
                return _narrator_tasks_error(
                    "WEB_APP_KEY_UNKNOWN",
                    "X-Web-App-Key is not recognized.",
                    401,
                    retryable=False,
                )

            task = nt_get_task(conn, user_id=user_id, narrator_task_id=narrator_task_id)
        finally:
            conn.close()
    except Exception:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="503").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return _narrator_tasks_error(
            "NARRATOR_TASKS_DB_UNAVAILABLE",
            "Narrator tasks store is temporarily unavailable.",
            503,
            retryable=True,
        )

    if task is None:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="404").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return _narrator_tasks_error(
            "NOT_FOUND",
            "Task not found.",
            404,
            retryable=False,
        )

    REQUEST_COUNT.labels(endpoint=endpoint_label, status="200").inc()
    REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
    return jsonify({"success": True, "data": task})


@app.route("/narrator/tasks/<narrator_task_id>", methods=["PUT"])
def narrator_tasks_replace(narrator_task_id):
    """Replace the full task body. Optional CAS preconditions via query:
    `expected_status=a,b` and/or `expected_step=c`. Returns 409
    CAS_MISMATCH when the row exists for this user but its current state
    doesn't satisfy the preconditions — distinct from the 404 case
    (missing / cross-tenant)."""
    start = time.time()
    endpoint_label = "narrator-tasks-replace"

    auth_error = require_web_user_auth(get_db_core_connection)
    if auth_error is not None:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status=str(auth_error[1])).inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return auth_error

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="400").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return _narrator_tasks_error(
            "BAD_REQUEST",
            "request body must be a JSON object.",
            400,
            retryable=False,
        )
    type_error = _validate_task_body(body)
    if type_error is not None:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="400").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return type_error
    if body.get("narrator_task_id") and body["narrator_task_id"] != narrator_task_id:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="400").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return _narrator_tasks_error(
            "BAD_REQUEST",
            "narrator_task_id in body does not match URL.",
            400,
            retryable=False,
        )

    raw_expected_status = request.args.get("expected_status")
    expected_status = (
        [s for s in raw_expected_status.split(",") if s]
        if raw_expected_status
        else None
    )
    expected_step = request.args.get("expected_step") or None
    app_key = request.headers.get("X-Web-App-Key", "")

    try:
        conn = get_db_core_connection()
        try:
            user_id = _lookup_user_id(conn, app_key)
            if user_id is None:
                REQUEST_COUNT.labels(endpoint=endpoint_label, status="401").inc()
                REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(
                    time.time() - start
                )
                return _narrator_tasks_error(
                    "WEB_APP_KEY_UNKNOWN",
                    "X-Web-App-Key is not recognized.",
                    401,
                    retryable=False,
                )

            result = nt_replace_task(
                conn,
                user_id=user_id,
                app_key=app_key,
                narrator_task_id=narrator_task_id,
                body=body,
                expected_status=expected_status,
                expected_step=expected_step,
            )
            # Only commit when the write actually happened. The CAS_MISMATCH
            # and missing-row branches did not modify state; rollback on
            # close is the correct outcome there.
            if isinstance(result, dict):
                # Fail-fast auto-refund (regression coverage / Web API contract §
                # 方案 D · case 2). Runs in the same transaction so
                # the refund commits or rolls back together with the
                # task status update. Re-raises SQLAlchemyError on any
                # storage failure so the outer except handler returns
                # a retryable 503 and the entire write rolls back —
                # see review (security hardening): silently
                # committing the task PUT while skipping the refund
                # would leave the user charged with no recovery.
                apply_auto_refund_if_eligible(
                    conn,
                    narrator_task_id=narrator_task_id,
                    user_id=user_id,
                    body=body,
                )
                conn.commit()
        finally:
            conn.close()
    except Exception:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="503").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return _narrator_tasks_error(
            "NARRATOR_TASKS_DB_UNAVAILABLE",
            "Narrator tasks store is temporarily unavailable.",
            503,
            retryable=True,
        )

    if result is None:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="404").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return _narrator_tasks_error(
            "NOT_FOUND",
            "Task not found.",
            404,
            retryable=False,
        )
    if result == CAS_MISMATCH:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="409").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return _narrator_tasks_error(
            "CAS_MISMATCH",
            "Current row state does not match the expected_status / expected_step preconditions.",
            409,
            retryable=False,
        )

    REQUEST_COUNT.labels(endpoint=endpoint_label, status="200").inc()
    REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
    return jsonify({"success": True, "data": result})


@app.route("/wallet/quotes", methods=["POST"])
def wallet_quotes():
    return handle_wallet_call(
        lambda: get_wallet_service().create_quote(
            wallet_json_body(),
            wallet_idempotency_key(),
        )
    )


@app.route("/wallet/freezes", methods=["POST"])
def wallet_freezes():
    return handle_wallet_call(
        lambda: get_wallet_service().freeze(
            wallet_json_body(),
            wallet_idempotency_key(),
        )
    )


@app.route("/wallet/confirms", methods=["POST"])
def wallet_confirms():
    return handle_wallet_call(
        lambda: get_wallet_service().confirm(
            wallet_json_body(),
            wallet_idempotency_key(),
        )
    )


@app.route("/wallet/refunds", methods=["POST"])
def wallet_refunds():
    return handle_wallet_call(
        lambda: get_wallet_service().refund(
            wallet_json_body(),
            wallet_idempotency_key(),
        )
    )


@app.route("/finance/refunds/overbill", methods=["POST"])
def finance_overbill_refunds():
    return handle_wallet_call(
        lambda: get_wallet_service().refund_overbill(
            wallet_json_body(),
            wallet_idempotency_key(),
        )
    )


@app.route("/finance/quota-board", methods=["GET"])
def finance_quota_board():
    def call():
        limit_raw = request.args.get("limit", "100")
        web_tenant_id = request.args.get("web_tenant_id")
        if web_tenant_id is not None and not web_tenant_id.strip():
            raise WalletError(
                400,
                "BAD_REQUEST",
                "web_tenant_id must be non-empty when provided.",
            )
        try:
            limit = int(limit_raw)
        except ValueError as exc:
            raise WalletError(
                400,
                "BAD_REQUEST",
                "limit must be an integer.",
            ) from exc
        return get_wallet_service().get_quota_board(
            web_tenant_id=web_tenant_id,
            limit=limit,
        )

    return handle_wallet_call(call)


@app.route("/wallet/transactions/<wallet_transaction_id>")
def wallet_transaction(wallet_transaction_id):
    return handle_wallet_call(
        lambda: get_wallet_service().get_transaction(
            wallet_transaction_id,
            request.args.get("web_tenant_id", ""),
            request.args.get("web_user_id", ""),
        )
    )


@app.route("/wallet/transactions/by-order/<web_order_id>")
def wallet_transaction_by_order(web_order_id):
    return handle_wallet_call(
        lambda: get_wallet_service().get_transaction_by_order(
            request.args.get("web_tenant_id", ""),
            request.args.get("web_user_id", ""),
            web_order_id,
        )
    )


@app.route("/wallet/hard-price/steps/by-order/<web_order_id>")
def wallet_hard_price_steps_by_order(web_order_id):
    return handle_wallet_call(
        lambda: get_wallet_service().get_hard_price_step_state_by_order(
            request.args.get("web_tenant_id", ""),
            request.args.get("web_user_id", ""),
            web_order_id,
        )
    )


@app.route("/wallet/hard-price/order-subtasks/by-order/<web_order_id>")
def wallet_order_subtasks_by_order(web_order_id):
    return handle_wallet_call(
        lambda: get_wallet_service().get_order_subtask_mapping_by_order(
            request.args.get("web_tenant_id", ""),
            request.args.get("web_user_id", ""),
            web_order_id,
        )
    )


@app.route("/wallet/transactions/by-idempotency-key/<idempotency_key>")
def wallet_transaction_by_idempotency_key(idempotency_key):
    return handle_wallet_call(
        lambda: get_wallet_service().get_transaction_by_idempotency_key(
            idempotency_key,
            request.args.get("web_tenant_id", ""),
            request.args.get("web_user_id", ""),
        )
    )


# ── /admin/users — operator-only user provisioning ──────────────────────────
#
# Backs the `/admin/create_user` page in the web frontend. The frontend
# gates `/admin/*` behind HTTP Basic Auth at the Next.js middleware layer;
# the BFF forwards here with the standard `PRICING_BFF_AUTH_TOKEN` Bearer.
# No second admin-token layer here yet — when one lands, slot it in
# alongside `require_pricing_bff_auth`.
#
# Audit (subsequent update): write paths emit a structured `app.logger.info`
# line so balance / profile changes can be traced from fly logs. Because
# /admin/* gates on a single shared Basic Auth credential, the actor IP
# (forwarded by the Next.js BFF as `X-Admin-Operator-IP`) is the only
# identity-ish signal we have today; treat it as "narrows the suspect
# set" rather than proof.


def _admin_actor_ip() -> str:
    """Identity hint for admin audit logging. The Next.js BFF resolves
    the operator's real IP from `x-forwarded-for` / `fly-client-ip` and
    sets it as `X-Admin-Operator-IP` on the outbound request — so we
    only trust this one header here, not the proxy chain.
    """
    return request.headers.get("X-Admin-Operator-IP", "unknown")


def _admin_users_error(code: str, message: str, status: int, *, retryable: bool):
    return (
        jsonify(
            {
                "success": False,
                "error": {
                    "code": code,
                    "message": message,
                    "retryable": retryable,
                    "details": {},
                },
            }
        ),
        status,
    )


@app.route("/admin/users", methods=["POST"])
def admin_create_user_route():
    start = time.time()
    endpoint_label = "admin-create-user"

    auth_error = require_pricing_bff_auth()
    if auth_error is not None:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status=str(auth_error[1])).inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return auth_error

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="400").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return _admin_users_error(
            "BAD_REQUEST",
            "Request body must be a JSON object.",
            400,
            retryable=False,
        )

    raw_balance = body.get("balance")
    if raw_balance is None:
        balance = DEFAULT_NEW_USER_BALANCE
    else:
        try:
            balance = Decimal(str(raw_balance))
        except Exception:
            REQUEST_COUNT.labels(endpoint=endpoint_label, status="400").inc()
            REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
            return _admin_users_error(
                "BAD_REQUEST",
                "balance must be a numeric value.",
                400,
                retryable=False,
            )
        if not balance.is_finite():
            # `Decimal("NaN")` / `Decimal("Infinity")` are valid Decimals
            # but downstream `< 0` comparisons raise InvalidOperation, so
            # without this guard non-finite inputs crash the route with
            # 500 instead of being rejected as bad input. security hardening.
            REQUEST_COUNT.labels(endpoint=endpoint_label, status="400").inc()
            REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
            return _admin_users_error(
                "BAD_REQUEST",
                "balance must be a finite numeric value.",
                400,
                retryable=False,
            )

    profile_fields = ("nickname", "mobile", "email", "company_name")
    profile: dict[str, Optional[str]] = {}
    for field in profile_fields:
        value = body.get(field)
        if value is None:
            profile[field] = None
            continue
        if not isinstance(value, str):
            REQUEST_COUNT.labels(endpoint=endpoint_label, status="400").inc()
            REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
            return _admin_users_error(
                "BAD_REQUEST",
                f"{field} must be a string.",
                400,
                retryable=False,
            )
        stripped = value.strip()
        profile[field] = stripped or None

    app_key = generate_app_key()
    try:
        admin_create_user(
            get_db_engine(),
            app_key,
            balance,
            **profile,
        )
    except InvalidBalance as error:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="400").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return _admin_users_error(
            "BAD_REQUEST", str(error), 400, retryable=False
        )
    except (AppKeyAlreadyExists, InvalidAppKeyFormat):
        # Both are operationally impossible here (we just generated the key),
        # but surface as 503 retryable rather than crashing — the caller can
        # POST again to get a fresh key.
        app.logger.exception("admin_create_user: unexpected generated-key conflict")
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="503").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return _admin_users_error(
            "ADMIN_CREATE_USER_RETRY",
            "Generated app_key was unusable; retry the request.",
            503,
            retryable=True,
        )
    except SQLAlchemyError as error:
        DB_ERRORS.labels(
            surface="admin-create-user", code=error.__class__.__name__
        ).inc()
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="503").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return _admin_users_error(
            "ADMIN_CREATE_USER_DB_UNAVAILABLE",
            "User provisioning database is temporarily unavailable.",
            503,
            retryable=True,
        )

    # Audit log: keep balance (financial signal, not PII) but record
    # profile fields as a name list only — never the raw values. The
    # observability pipeline retains logs longer than the DB and is
    # accessible to more people, so a verbatim copy of mobile/email/
    # nickname/company_name there would broaden PII exposure beyond
    # what the table already permits. security hardening.
    app.logger.info(
        "admin.users.create",
        extra={
            "event": "admin.users.create",
            "actor_ip": _admin_actor_ip(),
            "app_key": app_key,
            "balance": str(balance),
            "profile_fields_set": [
                f for f, v in profile.items() if v is not None
            ],
        },
    )

    REQUEST_COUNT.labels(endpoint=endpoint_label, status="201").inc()
    REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
    return (
        jsonify({"success": True, "data": {"app_key": app_key}}),
        201,
    )


def _serialize_admin_user(user: dict) -> dict:
    """Render the user dict from `users.admin.get_user_by_app_key` for
    the wire. `Decimal` → str (stable across driver / locale), datetime
    → isoformat str if not already, NULL columns stay as null."""
    balance = user["balance"]
    created_at = user["created_at"]
    return {
        "app_key": user["app_key"],
        "balance": str(balance),
        "nickname": user["nickname"],
        "mobile": user["mobile"],
        "email": user["email"],
        "company_name": user["company_name"],
        "created_at": (
            created_at.isoformat()
            if hasattr(created_at, "isoformat")
            else created_at
        ),
    }


@app.route("/admin/users/<string:app_key>", methods=["GET"])
def admin_get_user_route(app_key: str):
    start = time.time()
    endpoint_label = "admin-get-user"

    auth_error = require_pricing_bff_auth()
    if auth_error is not None:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status=str(auth_error[1])).inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return auth_error

    try:
        user = admin_get_user_by_app_key(get_db_engine(), app_key)
    except InvalidAppKeyFormat as error:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="400").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return _admin_users_error(
            "BAD_REQUEST", str(error), 400, retryable=False
        )
    except SQLAlchemyError as error:
        DB_ERRORS.labels(
            surface="admin-get-user", code=error.__class__.__name__
        ).inc()
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="503").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return _admin_users_error(
            "ADMIN_GET_USER_DB_UNAVAILABLE",
            "User lookup database is temporarily unavailable.",
            503,
            retryable=True,
        )

    if user is None:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="404").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return _admin_users_error(
            "USER_NOT_FOUND",
            f"app_key {app_key!r} not found.",
            404,
            retryable=False,
        )

    REQUEST_COUNT.labels(endpoint=endpoint_label, status="200").inc()
    REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
    return jsonify({"success": True, "data": _serialize_admin_user(user)}), 200


@app.route("/admin/users/<string:app_key>", methods=["PATCH"])
def admin_update_user_route(app_key: str):
    start = time.time()
    endpoint_label = "admin-update-user"

    auth_error = require_pricing_bff_auth()
    if auth_error is not None:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status=str(auth_error[1])).inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return auth_error

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="400").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return _admin_users_error(
            "BAD_REQUEST",
            "Request body must be a JSON object.",
            400,
            retryable=False,
        )

    # Build the kwargs for `update_user`. Missing keys are skipped; the
    # sentinel inside `update_user` keeps those columns untouched. We
    # intentionally distinguish "field absent" (leave column) from "field
    # is null" (clear column) — needed so operators can blank out a
    # nickname/mobile/email without having to re-enter every other field.
    kwargs: dict[str, Any] = {}

    if "balance" in body:
        raw_balance = body["balance"]
        if raw_balance is None:
            REQUEST_COUNT.labels(endpoint=endpoint_label, status="400").inc()
            REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
            return _admin_users_error(
                "BAD_REQUEST", "balance cannot be null.", 400, retryable=False
            )
        try:
            parsed_balance = Decimal(str(raw_balance))
        except Exception:
            REQUEST_COUNT.labels(endpoint=endpoint_label, status="400").inc()
            REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
            return _admin_users_error(
                "BAD_REQUEST",
                "balance must be a numeric value.",
                400,
                retryable=False,
            )
        if not parsed_balance.is_finite():
            # Reject NaN / Infinity at the boundary so they never reach
            # the `< 0` comparison in admin.update_user (which would
            # raise InvalidOperation → 500). security hardening.
            REQUEST_COUNT.labels(endpoint=endpoint_label, status="400").inc()
            REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
            return _admin_users_error(
                "BAD_REQUEST",
                "balance must be a finite numeric value.",
                400,
                retryable=False,
            )
        kwargs["balance"] = parsed_balance

    profile_fields = ("nickname", "mobile", "email", "company_name")
    for field in profile_fields:
        if field not in body:
            continue
        value = body[field]
        if value is None:
            kwargs[field] = None
            continue
        if not isinstance(value, str):
            REQUEST_COUNT.labels(endpoint=endpoint_label, status="400").inc()
            REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
            return _admin_users_error(
                "BAD_REQUEST",
                f"{field} must be a string.",
                400,
                retryable=False,
            )
        stripped = value.strip()
        kwargs[field] = stripped or None

    try:
        # Capture before-state so the audit log records what actually
        # changed. If the row already does not exist we short-circuit to
        # 404 here rather than letting `update_user` race with a
        # concurrent delete — admin operators are rare enough that the extra
        # SELECT is free.
        before = admin_get_user_by_app_key(get_db_engine(), app_key)
        if before is None:
            REQUEST_COUNT.labels(endpoint=endpoint_label, status="404").inc()
            REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
            return _admin_users_error(
                "USER_NOT_FOUND",
                f"app_key {app_key!r} not found.",
                404,
                retryable=False,
            )
        admin_update_user(get_db_engine(), app_key, **kwargs)
    except InvalidBalance as error:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="400").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return _admin_users_error(
            "BAD_REQUEST", str(error), 400, retryable=False
        )
    except InvalidAppKeyFormat as error:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="400").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return _admin_users_error(
            "BAD_REQUEST", str(error), 400, retryable=False
        )
    except UserNotFound as error:
        # Reached only if the row vanished between the before-lookup and
        # the UPDATE — extremely unlikely under admin traffic, but kept
        # so a concurrent delete still returns a clean 404 rather than
        # leaking a 500.
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="404").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return _admin_users_error(
            "USER_NOT_FOUND", str(error), 404, retryable=False
        )
    except SQLAlchemyError as error:
        DB_ERRORS.labels(
            surface="admin-update-user", code=error.__class__.__name__
        ).inc()
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="503").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return _admin_users_error(
            "ADMIN_UPDATE_USER_DB_UNAVAILABLE",
            "User update database is temporarily unavailable.",
            503,
            retryable=True,
        )

    # Refresh under the same SQLAlchemyError envelope as the UPDATE so
    # a transient driver failure here surfaces as 503 rather than an
    # unhandled 500. `refreshed is None` covers the (small) window
    # where the row is deleted between commit and re-select; return a
    # clean 404 instead of crashing in serialization. security hardening.
    try:
        refreshed = admin_get_user_by_app_key(get_db_engine(), app_key)
    except SQLAlchemyError as error:
        DB_ERRORS.labels(
            surface="admin-update-user", code=error.__class__.__name__
        ).inc()
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="503").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return _admin_users_error(
            "ADMIN_UPDATE_USER_DB_UNAVAILABLE",
            "User update database is temporarily unavailable.",
            503,
            retryable=True,
        )
    if refreshed is None:
        REQUEST_COUNT.labels(endpoint=endpoint_label, status="404").inc()
        REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
        return _admin_users_error(
            "USER_NOT_FOUND",
            f"app_key {app_key!r} was deleted during update.",
            404,
            retryable=False,
        )

    # Audit log: balance changes are recorded with before/after values
    # (financial signal, not PII), but profile fields are recorded as a
    # name list of what changed — never the raw values. See create-
    # route comment for the PII rationale. security hardening.
    balance_before: Optional[str] = None
    balance_after: Optional[str] = None
    if "balance" in kwargs:
        b = str(before["balance"])
        a = str(refreshed["balance"])
        if b != a:
            balance_before, balance_after = b, a
    profile_changed = [
        f
        for f in ("nickname", "mobile", "email", "company_name")
        if f in kwargs and before[f] != refreshed[f]
    ]
    app.logger.info(
        "admin.users.update",
        extra={
            "event": "admin.users.update",
            "actor_ip": _admin_actor_ip(),
            "app_key": app_key,
            "balance_before": balance_before,
            "balance_after": balance_after,
            "profile_changed": profile_changed,
        },
    )

    REQUEST_COUNT.labels(endpoint=endpoint_label, status="200").inc()
    REQUEST_LATENCY.labels(endpoint=endpoint_label).observe(time.time() - start)
    return (
        jsonify({"success": True, "data": _serialize_admin_user(refreshed)}),
        200,
    )


# ── Orchestrator ──────────────────────────────────────
# Imports at function scope so the apscheduler dependency is only loaded
# when ORCHESTRATOR_ENABLED=true (default false). bootstrap.start_orchestrator
# returns None when disabled, so this is just one env-var read at boot.
from orchestrator.bootstrap import start_orchestrator  # noqa: E402
app.orchestrator = start_orchestrator(get_db_engine())


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
