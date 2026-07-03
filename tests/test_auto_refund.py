"""Tests for the fail-fast auto-refund hook (regression coverage / Web API contract).

Two layers:

1. `detect_fail_fast_no_work` — pure function over a PUT body.
   Exhaustive boolean coverage: 3 ways the body can satisfy condition 2
   (no-output), 5+ ways any one of the three conditions can fail.
2. `apply_auto_refund_if_eligible` — integration with sqlite, covering
   feature-flag gate, no-snapshot v1 row, CAS idempotency, zero-amount
   snapshot, and the happy path that credits the wallet + flips
   refund_status.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text

from pricing_quote_v2.auto_refund import (
    apply_auto_refund_if_eligible,
    detect_fail_fast_no_work,
)
from pricing_quote_v2.schema import (
    PRICING_QUOTES_V2_SCHEMA_SQL,
    PRICING_SNAPSHOTS_V2_SCHEMA_SQL,
)


sqlite3.register_adapter(Decimal, str)


NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
WEB_APP_KEY = "grid_TestAutoRefundKey0000"
USER_ID = 1
INITIAL_BALANCE = 1000


SQLITE_USERS_SCHEMA = """
CREATE TABLE users (
    app_key TEXT PRIMARY KEY,
    id INTEGER UNIQUE,
    balance_points NUMERIC NOT NULL DEFAULT 1000 CHECK (balance_points >= 0),
    nickname TEXT,
    mobile TEXT,
    email TEXT,
    company_name TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

SQLITE_NARRATOR_TASKS_SCHEMA = """
CREATE TABLE narrator_tasks (
    narrator_task_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    app_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    current_step TEXT,
    data TEXT NOT NULL,
    snapshot_id TEXT,
    run_auto SMALLINT NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _exec_each(conn, sql_block):
    for statement in sql_block.split(";"):
        s = statement.strip()
        if s:
            conn.execute(text(s))


# ─── detection layer ─────────────────────────────────────────────────────────


def _body(
    *,
    status="failed",
    current_step="generate_writing",
    steps=None,
):
    """Master-task body in the FLAT shape the PUT /narrator/tasks/<id>
    route and the orchestrator path actually hand to the auto-refund
    hook — top-level `steps`, no `data` wrapper.

    Prior revision wrapped under `data.steps`; that matched neither
    the route nor the store.py persisted shape and caused
    detect_fail_fast_no_work to always return pattern_mismatch
    against real traffic . The hook never fired between
    its 2026-06-10 deploy and 2026-06-12 discovery — visible only
    because no integration test exercised the Flask route.
    """
    return {
        "status": status,
        "current_step": current_step,
        "steps": steps if steps is not None else {
            "generate_writing": {
                "status": "failed",
                "started_at": "2026-06-09T12:00:00Z",
                "error": "upstream rejected",
            }
        },
    }


def test_detect_matches_canonical_fail_fast_shape():
    """The catalyst case from Web API contract: status=failed,
    one subflow attempted, no task_id, no output, current_step still
    points at it."""
    assert detect_fail_fast_no_work(_body()) == "generate_writing"


def test_detect_matches_when_current_step_is_missing():
    body = _body()
    del body["current_step"]
    assert detect_fail_fast_no_work(body) == "generate_writing"


def test_detect_matches_when_current_step_is_empty_string():
    body = _body(current_step="")
    assert detect_fail_fast_no_work(body) == "generate_writing"


def test_detect_matches_fast_generate_writing_first_step():
    """First step key may be `fast_generate_writing` for original-writing
    flows; the detector must not hardcode either name."""
    body = _body(
        current_step="fast_generate_writing",
        steps={
            "fast_generate_writing": {
                "status": "failed",
                "started_at": "2026-06-09T12:00:00Z",
                "error": "upstream rejected",
            }
        },
    )
    assert detect_fail_fast_no_work(body) == "fast_generate_writing"


def test_detect_rejects_non_failed_status():
    assert detect_fail_fast_no_work(_body(status="running")) is None
    assert detect_fail_fast_no_work(_body(status="completed")) is None


def test_detect_rejects_when_first_step_has_task_id():
    body = _body(steps={
        "generate_writing": {
            "status": "failed",
            "task_id": "upstream_task_999",
            "started_at": "2026-06-09T12:00:00Z",
        }
    })
    assert detect_fail_fast_no_work(body) is None


def test_detect_rejects_when_task_id_is_unexpected_type():
    """task_id is contractually a string. A numeric / non-empty value
    of any type means upstream definitely accepted something — stay
    on the manual path rather than auto-refunding speculatively."""
    for stray in (0, 1, "0", {"id": "x"}, ["x"]):
        body = _body(steps={
            "generate_writing": {"status": "failed", "task_id": stray},
        })
        assert detect_fail_fast_no_work(body) is None, (
            f"task_id={stray!r} should disqualify the auto-refund pattern"
        )


def test_detect_rejects_when_step_status_not_failed():
    body = _body(steps={
        "generate_writing": {
            "status": "running",
            "started_at": "2026-06-09T12:00:00Z",
        }
    })
    assert detect_fail_fast_no_work(body) is None


def test_detect_rejects_when_step_has_output_file_id_and_current_step_advanced():
    """If any step has produced output AND current_step has moved past
    the first step, both OR branches of condition 2 fail and refund is
    blocked. Sets current_step to an unrelated value to force the
    branch that the catalyst case implicitly takes."""
    body = _body(
        current_step="clip_data",
        steps={
            "generate_writing": {
                "status": "failed",
                "output_file_id": "f_abc",
            }
        },
    )
    assert detect_fail_fast_no_work(body) is None


def test_detect_rejects_when_step_has_output_url_and_current_step_advanced():
    body = _body(
        current_step="clip_data",
        steps={
            "generate_writing": {
                "status": "failed",
                "output_url": "https://example.com/x.mp4",
            }
        },
    )
    assert detect_fail_fast_no_work(body) is None


def test_detect_rejects_single_step_with_output_even_when_current_step_at_first():
    """AND semantics: a single-step body that carries output_file_id
    while current_step still references it is internally inconsistent
    (output existed somewhere, so something ran). Manual path."""
    body = _body(
        current_step="generate_writing",
        steps={
            "generate_writing": {
                "status": "failed",
                "output_file_id": "f_lingering",
            }
        },
    )
    assert detect_fail_fast_no_work(body) is None


def test_detect_rejects_multi_step_failure():
    """Case 5 from § 方案 D — later subflow failed AND the earlier
    one produced output. Stays manual."""
    body = _body(
        current_step="clip_data",
        steps={
            "generate_writing": {
                "status": "completed",
                "task_id": "upstream_t_1",
                "output_file_id": "f_writing_out",
            },
            "clip_data": {"status": "failed"},
        },
    )
    assert detect_fail_fast_no_work(body) is None


def test_detect_matches_multi_step_when_all_have_no_output_and_current_at_first():
    """regression coverage: a body with placeholder later step
    entries that are still output-free must still auto-refund,
    provided current_step also still references the first step that
    failed. This is the realistic data shape — the frontend
    pre-allocates step rows before they run; current_step doesn't
    advance until the previous step completes."""
    body = _body(
        current_step="generate_writing",
        steps={
            "generate_writing": {
                "status": "failed",
                "started_at": "2026-06-09T12:00:00Z",
                "error": "upstream rejected",
            },
            "clip_data": {"status": "pending"},
            "video_composing": {"status": "pending"},
        },
    )
    assert detect_fail_fast_no_work(body) == "generate_writing"


def test_detect_rejects_multi_step_when_all_empty_but_current_step_advanced():
    """AND semantics: even if no step produced output, if current_step
    has moved past the first step the shape is internally inconsistent
    (how did it advance without anything completing?). Bail to manual."""
    body = _body(
        current_step="clip_data",
        steps={
            "generate_writing": {"status": "failed"},
            "clip_data": {"status": "pending"},
        },
    )
    assert detect_fail_fast_no_work(body) is None


def test_detect_rejects_multi_step_when_a_later_step_has_output():
    """If any step (first or later) carries output_file_id /
    output_url, the all-no-output branch fails and refund is blocked.
    Documents that AND semantics catch this even when current_step
    still points at first_step."""
    body = _body(
        current_step="generate_writing",
        steps={
            "generate_writing": {"status": "failed"},
            "clip_data": {"output_file_id": "f_lingering"},
        },
    )
    assert detect_fail_fast_no_work(body) is None


def test_detect_rejects_when_current_step_moved_past_first_step():
    body = _body(
        current_step="clip_data",
        steps={
            "generate_writing": {"status": "failed"},
        },
    )
    assert detect_fail_fast_no_work(body) is None


def test_detect_rejects_empty_or_missing_steps():
    assert detect_fail_fast_no_work({"status": "failed"}) is None
    assert detect_fail_fast_no_work({"status": "failed", "data": {}}) is None
    assert detect_fail_fast_no_work(
        {"status": "failed", "data": {"steps": {}}}
    ) is None


def test_detect_rejects_non_dict_body():
    assert detect_fail_fast_no_work(None) is None
    assert detect_fail_fast_no_work("not a dict") is None
    assert detect_fail_fast_no_work([]) is None


# ─── apply layer (integration) ───────────────────────────────────────────────


@pytest.fixture()
def engine(monkeypatch):
    # Default the feature flag ON for apply tests; individual tests
    # flip it off when they want to assert the disabled path.
    monkeypatch.setenv("AUTO_REFUND_FAIL_FAST_ENABLED", "true")
    eng = create_engine("sqlite:///:memory:")
    with eng.begin() as conn:
        _exec_each(conn, SQLITE_USERS_SCHEMA)
        conn.execute(
            text(
                "INSERT INTO users (app_key, id, balance_points) "
                "VALUES (:k, :id, :b)"
            ),
            {"k": WEB_APP_KEY, "id": USER_ID, "b": INITIAL_BALANCE},
        )
        _exec_each(conn, SQLITE_NARRATOR_TASKS_SCHEMA)
        _exec_each(conn, PRICING_QUOTES_V2_SCHEMA_SQL)
        _exec_each(conn, PRICING_SNAPSHOTS_V2_SCHEMA_SQL)
    return eng


def _seed_quote_and_snapshot(
    engine,
    *,
    quote_id="Q-test-001",
    snapshot_id="S-test-001",
    quote_price=None,
    snapshot_price=81,
    refund_status="none",
    user_id=USER_ID,
):
    """Seed a quote + snapshot pair. `quote_price` defaults to a
    distinct sentinel (`snapshot_price + 7`) so tests inadvertently
    asserting against the quote rather than the snapshot fail loudly
    (regression coverage: drift between the two prices
    must surface as a test failure)."""
    if quote_price is None:
        quote_price = snapshot_price + 7
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO pricing_quotes_v2 "
                "(quote_id, pricing_rule_version, price_source, template_id, "
                " combo_key, pro_upgrade, final_charge_price, flash_total, "
                " pro_total, pro_upgrade_delta, pricing_minutes, "
                " system_reference_price, breakdown, currency_unit, "
                " expires_at, web_user_id, created_at) "
                "VALUES (:qid, 'v2.0', 'manual_catalog_price', 'tpl_1', "
                "        'secondary_creation', 0, :qprice, :qprice, :qprice, "
                "        0, 10, :qprice, '[]', 'web_point', :now, :uid, :now)"
            ),
            {
                "qid": quote_id,
                "qprice": quote_price,
                "now": NOW,
                "uid": user_id,
            },
        )
        conn.execute(
            text(
                "INSERT INTO pricing_snapshots_v2 "
                "(snapshot_id, quote_id, pricing_rule_version, combo_key, "
                " price_source, template_id, pricing_minutes, "
                " system_reference_price, final_charge_price, breakdown, "
                " currency_unit, committed_at, refund_policy, refund_status, "
                " subflow_status, web_user_id) "
                "VALUES (:sid, :qid, 'v2.0', 'secondary_creation', "
                "        'manual_catalog_price', 'tpl_1', 10, :sprice, "
                "        :sprice, '[]', 'web_point', :now, 'manual', :rs, "
                "        '[]', :uid)"
            ),
            {
                "sid": snapshot_id,
                "qid": quote_id,
                "sprice": snapshot_price,
                "now": NOW,
                "rs": refund_status,
                "uid": user_id,
            },
        )


def _seed_task(
    engine,
    *,
    narrator_task_id="T-test-001",
    snapshot_id="S-test-001",
):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO narrator_tasks "
                "(narrator_task_id, user_id, app_key, status, current_step, "
                " data, snapshot_id, created_at, updated_at) "
                "VALUES (:tid, :uid, :k, 'failed', 'generate_writing', "
                "        '{}', :sid, :now, :now)"
            ),
            {
                "tid": narrator_task_id,
                "uid": USER_ID,
                "k": WEB_APP_KEY,
                "sid": snapshot_id,
                "now": NOW,
            },
        )


def _balance(engine):
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT balance_points FROM users WHERE id = :uid"),
            {"uid": USER_ID},
        ).first()
    return int(Decimal(str(row[0])))


def _refund_status(engine, snapshot_id="S-test-001"):
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT refund_status FROM pricing_snapshots_v2 "
                "WHERE snapshot_id = :sid"
            ),
            {"sid": snapshot_id},
        ).first()
    return row[0] if row else None


def test_apply_feature_flag_off_short_circuits(engine, monkeypatch):
    monkeypatch.setenv("AUTO_REFUND_FAIL_FAST_ENABLED", "false")
    _seed_quote_and_snapshot(engine)
    _seed_task(engine)
    with engine.begin() as conn:
        outcome = apply_auto_refund_if_eligible(
            conn,
            narrator_task_id="T-test-001",
            user_id=USER_ID,
            body=_body(),
        )
    assert outcome == {"applied": False, "reason": "feature_disabled"}
    assert _balance(engine) == INITIAL_BALANCE
    assert _refund_status(engine) == "none"


def test_apply_pattern_miss_does_nothing(engine):
    _seed_quote_and_snapshot(engine)
    _seed_task(engine)
    body = _body(status="running")
    with engine.begin() as conn:
        outcome = apply_auto_refund_if_eligible(
            conn,
            narrator_task_id="T-test-001",
            user_id=USER_ID,
            body=body,
        )
    assert outcome == {"applied": False, "reason": "pattern_mismatch"}
    assert _balance(engine) == INITIAL_BALANCE
    assert _refund_status(engine) == "none"


def test_apply_v1_row_no_snapshot_id_short_circuits(engine):
    # Seed the task without seeding a snapshot — snapshot_id is NULL.
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO narrator_tasks "
                "(narrator_task_id, user_id, app_key, status, current_step, "
                " data, snapshot_id, created_at, updated_at) "
                "VALUES ('T-v1-001', :uid, :k, 'failed', 'generate_writing', "
                "        '{}', NULL, :now, :now)"
            ),
            {"uid": USER_ID, "k": WEB_APP_KEY, "now": NOW},
        )
    with engine.begin() as conn:
        outcome = apply_auto_refund_if_eligible(
            conn,
            narrator_task_id="T-v1-001",
            user_id=USER_ID,
            body=_body(),
        )
    assert outcome == {"applied": False, "reason": "no_snapshot"}
    assert _balance(engine) == INITIAL_BALANCE


def test_apply_happy_path_credits_wallet_and_marks_snapshot(engine):
    # snapshot_price and quote_price are intentionally distinct (the
    # helper seeds quote_price = snapshot_price + 7) so any
    # implementation that credited from the quote rather than the
    # snapshot would surface here as a balance != 81 mismatch.
    _seed_quote_and_snapshot(engine, snapshot_price=81)
    _seed_task(engine)
    with engine.begin() as conn:
        outcome = apply_auto_refund_if_eligible(
            conn,
            narrator_task_id="T-test-001",
            user_id=USER_ID,
            body=_body(),
        )
    assert outcome["applied"] is True
    assert outcome["amount"] == 81  # snapshot.final_charge_price, NOT quote's 88
    assert outcome["first_step"] == "generate_writing"
    assert outcome["snapshot_id"] == "S-test-001"
    assert _balance(engine) == INITIAL_BALANCE + 81
    assert _refund_status(engine) == "auto_refunded_fail_fast"


def test_apply_credits_snapshot_price_when_quote_diverges(engine):
    """regression coverage — make the source-of-truth
    explicit. Seed snapshot at 50 with the quote helper's default
    sentinel offset (quote at 57), and assert the credit matches the
    snapshot regardless of the quote value."""
    _seed_quote_and_snapshot(engine, snapshot_price=50)  # quote_price -> 57
    _seed_task(engine)
    with engine.begin() as conn:
        outcome = apply_auto_refund_if_eligible(
            conn,
            narrator_task_id="T-test-001",
            user_id=USER_ID,
            body=_body(),
        )
    assert outcome["applied"] is True
    assert outcome["amount"] == 50
    assert _balance(engine) == INITIAL_BALANCE + 50


def test_apply_is_idempotent(engine):
    """Second call after a successful refund is a no-op — CAS lost."""
    _seed_quote_and_snapshot(engine, snapshot_price=81)
    _seed_task(engine)
    with engine.begin() as conn:
        first = apply_auto_refund_if_eligible(
            conn,
            narrator_task_id="T-test-001",
            user_id=USER_ID,
            body=_body(),
        )
    assert first["applied"] is True
    assert _balance(engine) == INITIAL_BALANCE + 81

    with engine.begin() as conn:
        second = apply_auto_refund_if_eligible(
            conn,
            narrator_task_id="T-test-001",
            user_id=USER_ID,
            body=_body(),
        )
    assert second == {"applied": False, "reason": "already_refunded"}
    assert _balance(engine) == INITIAL_BALANCE + 81  # no double-credit


def test_apply_skips_when_snapshot_already_refunded(engine):
    _seed_quote_and_snapshot(
        engine, refund_status="manual_refunded"
    )
    _seed_task(engine)
    with engine.begin() as conn:
        outcome = apply_auto_refund_if_eligible(
            conn,
            narrator_task_id="T-test-001",
            user_id=USER_ID,
            body=_body(),
        )
    assert outcome == {"applied": False, "reason": "already_refunded"}
    assert _balance(engine) == INITIAL_BALANCE
    assert _refund_status(engine) == "manual_refunded"


def test_apply_zero_amount_snapshot_flips_status_no_balance_change(engine):
    """Free orders shouldn't exist on the v2 path (§4 invariant) but if
    one ever lands, flip refund_status without a no-op UPDATE that
    would still consume the CAS slot."""
    _seed_quote_and_snapshot(engine, snapshot_price=0)
    _seed_task(engine)
    with engine.begin() as conn:
        outcome = apply_auto_refund_if_eligible(
            conn,
            narrator_task_id="T-test-001",
            user_id=USER_ID,
            body=_body(),
        )
    assert outcome["applied"] is True
    assert outcome["amount"] == 0
    assert _balance(engine) == INITIAL_BALANCE
    assert _refund_status(engine) == "auto_refunded_fail_fast"


# ─── storage-error propagation (review · security hardening) ────────────
#
# A storage error during refund detection / CAS / credit must propagate
# so the route layer rolls back the task PUT. Returning a no-op outcome
# while the caller commits would leave the user charged with
# `status=failed` and no recovery.


def test_apply_rejects_cross_tenant_lookup(engine):
    """defense-in-depth — even though
    the route layer validates ownership via nt_replace_task before
    this hook runs, both the snapshot_id lookup and the snapshot read
    pin web_user_id. A caller passing a foreign narrator_task_id with
    their own user_id never reaches the CAS / credit step."""
    OTHER_USER_ID = 99
    OTHER_APP_KEY = "grid_OtherTenant000000000"
    # Seed quote + snapshot owned by OTHER_USER_ID.
    _seed_quote_and_snapshot(engine, user_id=OTHER_USER_ID)
    # Also need a users row for OTHER_USER_ID so the FK on narrator_tasks
    # is satisfied.
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (app_key, id, balance_points) "
                "VALUES (:k, :id, :b)"
            ),
            {"k": OTHER_APP_KEY, "id": OTHER_USER_ID, "b": INITIAL_BALANCE},
        )
        # Task seeded under OTHER_USER_ID.
        conn.execute(
            text(
                "INSERT INTO narrator_tasks "
                "(narrator_task_id, user_id, app_key, status, current_step, "
                " data, snapshot_id, created_at, updated_at) "
                "VALUES ('T-other-001', :uid, :k, 'failed', 'generate_writing', "
                "        '{}', :sid, :now, :now)"
            ),
            {
                "uid": OTHER_USER_ID,
                "k": OTHER_APP_KEY,
                "sid": "S-test-001",
                "now": NOW,
            },
        )
    # Call with our user_id but the other tenant's task id — must
    # short-circuit at the snapshot_id lookup.
    with engine.begin() as conn:
        outcome = apply_auto_refund_if_eligible(
            conn,
            narrator_task_id="T-other-001",
            user_id=USER_ID,
            body=_body(),
        )
    assert outcome == {"applied": False, "reason": "no_snapshot"}
    assert _balance(engine) == INITIAL_BALANCE  # our balance untouched


def _connection_that_raises_on_execute(monkeypatch):
    """Build a stand-in for an SQLAlchemy `Connection` whose `.execute`
    always raises `SQLAlchemyError`. We use this rather than monkey-
    patching the live engine so each test can target exactly one
    failure mode."""
    from sqlalchemy.exc import OperationalError

    class _BrokenConn:
        def __init__(self, exc):
            self._exc = exc

        def execute(self, *args, **kwargs):  # noqa: D401
            raise self._exc

    return _BrokenConn(
        OperationalError("SELECT 1", {}, Exception("simulated outage"))
    )


def test_apply_raises_when_snapshot_lookup_fails(engine, monkeypatch):
    """First DB call (narrator_tasks.snapshot_id select) raises ->
    propagate so the route returns 503 and rolls back."""
    from sqlalchemy.exc import SQLAlchemyError

    broken = _connection_that_raises_on_execute(monkeypatch)
    with pytest.raises(SQLAlchemyError):
        apply_auto_refund_if_eligible(
            broken,
            narrator_task_id="T-test-001",
            user_id=USER_ID,
            body=_body(),
        )


def test_apply_raises_when_cas_or_credit_fails(engine, monkeypatch):
    """Once detection has decided we should refund, ANY storage failure
    on the CAS or balance credit must propagate so the outer
    transaction rolls back the task PUT.

    We exercise this end-to-end: seed the data so detection picks the
    happy path, then force a failure mid-stream by monkeypatching
    `conn.execute` on a real connection right before the CAS."""
    from sqlalchemy.exc import OperationalError, SQLAlchemyError

    _seed_quote_and_snapshot(engine)
    _seed_task(engine)
    with engine.begin() as conn:
        # Wrap conn.execute so the third call (the CAS update) raises.
        # call 1: narrator_tasks lookup
        # call 2: snapshot read
        # call 3: CAS update ← raises
        real_execute = conn.execute
        calls = {"n": 0}

        def flaky_execute(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] >= 3:
                raise OperationalError(
                    "UPDATE pricing_snapshots_v2",
                    {},
                    Exception("simulated outage"),
                )
            return real_execute(*args, **kwargs)

        monkeypatch.setattr(conn, "execute", flaky_execute)
        with pytest.raises(SQLAlchemyError):
            apply_auto_refund_if_eligible(
                conn,
                narrator_task_id="T-test-001",
                user_id=USER_ID,
                body=_body(),
            )


# ─── route-layer integration  ──────────────────────────────────────────
# These exercise the full PUT /narrator/tasks/<id> Flask route with the
# real flat-shape body the web side ships, asserting the hook actually
# fires end-to-end. Without an integration test like this the body-shape
# mismatch that route-level coverage is meant to catch.


@pytest.fixture()
def route_client(engine, monkeypatch):
    """Flask test client wired to the same engine the apply fixtures
    use, so route-level tests share the seeded quote / snapshot / task
    rows. `AUTO_REFUND_FAIL_FAST_ENABLED` is already true on `engine`.

    The shared `WEB_APP_KEY` constant is 21 char body — fine for tests
    that bypass route auth, but the Flask route's `is_valid_app_key`
    requires `grid_` + exactly 22 base62. Patch the validator to
    accept any non-empty key for route tests rather than reseeding
    quote / snapshot / task under a new user_id."""
    import server
    import users.auth as users_auth

    monkeypatch.setattr(server, "get_db_engine", lambda: engine)
    monkeypatch.setattr(
        server, "get_db_core_connection", lambda: engine.connect()
    )
    monkeypatch.setattr(users_auth, "is_valid_app_key", lambda k: bool(k))
    return server.app.test_client()


def _put_failed_body():
    """The flat-shape PUT body web sends when sync/route.ts settles a
    fail-fast task. Shape mirrors a captured production example."""
    return {
        "narrator_task_id": "T-test-001",
        "status": "failed",
        "current_step": "generate_writing",
        "error_message": "解说文案任务创建失败",
        "steps": {
            "generate_writing": {
                "status": "failed",
                "started_at": "2026-06-12T01:19:41.917Z",
                "error": "解说文案任务创建失败",
            },
        },
        "run_auto": 1,
    }


def test_route_put_triggers_auto_refund_with_flat_body(route_client, engine):
    """Regression for regression coverage bug 1: detector previously read body.data.steps
    and so silently no-op'd on every real PUT. Asserts the route → hook
    path now credits balance and stamps refund_status when the env flag
    is on and the body matches case 2."""
    _seed_quote_and_snapshot(engine, snapshot_price=81)
    # Seed the task in `status=running` so the PUT-with-status=failed
    # represents a real transition rather than a no-op replace.
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO narrator_tasks "
                "(narrator_task_id, user_id, app_key, status, current_step, "
                " data, snapshot_id, created_at, updated_at) "
                "VALUES (:tid, :uid, :k, 'running', 'generate_writing', "
                "        '{}', :sid, :now, :now)"
            ),
            {
                "tid": "T-test-001",
                "uid": USER_ID,
                "k": WEB_APP_KEY,
                "sid": "S-test-001",
                "now": NOW,
            },
        )
    balance_before = _balance(engine)

    res = route_client.put(
        "/narrator/tasks/T-test-001",
        headers={"X-Web-App-Key": WEB_APP_KEY, "Content-Type": "application/json"},
        json=_put_failed_body(),
    )
    assert res.status_code == 200, res.get_json()

    assert _balance(engine) == balance_before + 81
    assert _refund_status(engine) == "auto_refunded_fail_fast"


def test_route_put_no_refund_when_step_has_task_id(route_client, engine):
    """Counter-test: when the first step DID receive a task_id (upstream
    accepted it then later failed — case 4 in the spec), the hook
    skips refund and the user stays on the manual path."""
    _seed_quote_and_snapshot(engine, snapshot_price=81)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO narrator_tasks "
                "(narrator_task_id, user_id, app_key, status, current_step, "
                " data, snapshot_id, created_at, updated_at) "
                "VALUES (:tid, :uid, :k, 'running', 'generate_writing', "
                "        '{}', :sid, :now, :now)"
            ),
            {
                "tid": "T-test-001",
                "uid": USER_ID,
                "k": WEB_APP_KEY,
                "sid": "S-test-001",
                "now": NOW,
            },
        )
    balance_before = _balance(engine)

    body = _put_failed_body()
    body["steps"]["generate_writing"]["task_id"] = "upstream-accepted-id"
    res = route_client.put(
        "/narrator/tasks/T-test-001",
        headers={"X-Web-App-Key": WEB_APP_KEY, "Content-Type": "application/json"},
        json=body,
    )
    assert res.status_code == 200

    # No balance change; refund_status stays at 'none'.
    assert _balance(engine) == balance_before
    assert _refund_status(engine) == "none"
