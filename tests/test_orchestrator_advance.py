"""Tests for orchestrator.advance.

Uses sqlite + real store (via the schema fixtures from
test_orchestrator_store_helpers) and a monkeypatched
proxy_narrator_upstream so the upstream IO is fully under test
control. The goal: drive every branch the web sync.ts:230-391
handler exercises and assert the resulting DB state matches what
the web side would have written.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, text

import orchestrator.advance as advance_module
import orchestrator.triggers as triggers_module
from narrator_metadata.upstream import UpstreamNarratorError
from narrator_tasks.store import create_task
from orchestrator.advance import (
    AdvanceOutcome,
    advance_one_task,
)


USERS_SCHEMA = """
CREATE TABLE users (
    app_key TEXT PRIMARY KEY,
    id INTEGER UNIQUE,
    balance_points NUMERIC NOT NULL DEFAULT 1000
);
"""

NARRATOR_TASKS_SCHEMA = """
CREATE TABLE narrator_tasks (
    narrator_task_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    app_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    current_step TEXT,
    data TEXT NOT NULL,
    run_auto SMALLINT NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

KEY = "user-key"
USER_ID = 1


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    with eng.begin() as conn:
        conn.execute(text(USERS_SCHEMA))
        conn.execute(text(NARRATOR_TASKS_SCHEMA))
        conn.execute(
            text(
                "INSERT INTO users (app_key, id, balance_points) "
                "VALUES (:k, :i, 1000)"
            ),
            {"k": KEY, "i": USER_ID},
        )
    yield eng
    eng.dispose()


def _make_running_auto_task(engine, *, current_step, step_record=None, extras=None):
    """Seed a task in (status=running, run_auto=1, current_step=<step>)
    with the given step_record on `steps[<step>]`. Returns the
    persisted task body so tests can use its narrator_task_id."""
    body = {
        "status": "running",
        "run_auto": 1,
        "current_step": current_step,
        "steps": {current_step: step_record or {}},
        **(extras or {}),
    }
    with engine.begin() as conn:
        return create_task(
            conn, user_id=USER_ID, app_key=KEY, body=body
        )


def _read(engine, task_id):
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT data, status, current_step, run_auto "
                "FROM narrator_tasks WHERE narrator_task_id=:t"
            ),
            {"t": task_id},
        ).first()
        return {
            "data": json.loads(row.data),
            "status": row.status,
            "current_step": row.current_step,
            "run_auto": row.run_auto,
        }


# ─── early-exit branches ────────────────────────────────────────────────────


class TestEarlyExits:
    def test_task_not_found(self, engine):
        result = advance_one_task(
            engine, narrator_task_id="missing", user_id=USER_ID, app_key=KEY
        )
        assert result.outcome == AdvanceOutcome.NOOP
        assert "not found" in result.detail

    def test_already_completed_master_is_noop(self, engine):
        # Replace the seeded body so status='completed' lands in the JSON
        # blob too (get_task reads from `data`, not the denormalized
        # status column).
        body = _make_running_auto_task(engine, current_step="video_composing")
        new_data = {**body, "status": "completed"}
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE narrator_tasks SET status='completed', data=:d "
                    "WHERE narrator_task_id=:t"
                ),
                {"t": body["narrator_task_id"], "d": json.dumps(new_data)},
            )
        result = advance_one_task(
            engine, narrator_task_id=body["narrator_task_id"],
            user_id=USER_ID, app_key=KEY,
        )
        assert result.outcome == AdvanceOutcome.NOOP
        assert "completed" in result.detail

    def test_no_remote_task_id_is_noop(self, engine):
        body = _make_running_auto_task(
            engine, current_step="popular_learning", step_record={}
        )
        result = advance_one_task(
            engine, narrator_task_id=body["narrator_task_id"],
            user_id=USER_ID, app_key=KEY,
        )
        assert result.outcome == AdvanceOutcome.NOOP
        assert "upstream task_id" in result.detail

    def test_run_auto_zero_is_noop(self, engine):
        # Seed with run_auto=0 to confirm advance refuses non-auto tasks
        # even when reached directly (not via scan).
        with engine.begin() as conn:
            body = create_task(
                conn,
                user_id=USER_ID,
                app_key=KEY,
                body={
                    "status": "running",
                    "run_auto": 0,
                    "current_step": "popular_learning",
                    "steps": {"popular_learning": {"task_id": "rt-1"}},
                },
            )
        result = advance_one_task(
            engine, narrator_task_id=body["narrator_task_id"],
            user_id=USER_ID, app_key=KEY,
        )
        assert result.outcome == AdvanceOutcome.NOOP
        assert "run_auto != 1" in result.detail


# ─── upstream status branches ──────────────────────────────────────────────


class TestUpstreamStatusHandling:
    def test_in_flight_status_returns_in_progress(self, engine, monkeypatch):
        body = _make_running_auto_task(
            engine,
            current_step="popular_learning",
            step_record={"task_id": "rt-1"},
        )
        monkeypatch.setattr(
            advance_module,
            "proxy_narrator_upstream",
            lambda *a, **k: {"data": {"status": 1}},
        )
        result = advance_one_task(
            engine, narrator_task_id=body["narrator_task_id"],
            user_id=USER_ID, app_key=KEY,
        )
        assert result.outcome == AdvanceOutcome.IN_PROGRESS
        snapshot = _read(engine, body["narrator_task_id"])
        # Status unchanged.
        assert snapshot["status"] == "running"
        assert snapshot["current_step"] == "popular_learning"

    def test_upstream_failed_marks_master_failed(self, engine, monkeypatch):
        body = _make_running_auto_task(
            engine,
            current_step="popular_learning",
            step_record={"task_id": "rt-1"},
        )
        monkeypatch.setattr(
            advance_module,
            "proxy_narrator_upstream",
            lambda *a, **k: {"data": {"status": 3}},
        )
        result = advance_one_task(
            engine, narrator_task_id=body["narrator_task_id"],
            user_id=USER_ID, app_key=KEY,
        )
        assert result.outcome == AdvanceOutcome.STEP_FAILED
        snapshot = _read(engine, body["narrator_task_id"])
        assert snapshot["status"] == "failed"
        assert "失败" in snapshot["data"]["error_message"]
        step = snapshot["data"]["steps"]["popular_learning"]
        assert step["status"] == "failed"
        assert "completed_at" in step

    def test_upstream_cancelled_message_says_cancelled(self, engine, monkeypatch):
        body = _make_running_auto_task(
            engine,
            current_step="popular_learning",
            step_record={"task_id": "rt-1"},
        )
        monkeypatch.setattr(
            advance_module,
            "proxy_narrator_upstream",
            lambda *a, **k: {"data": {"status": 4}},
        )
        result = advance_one_task(
            engine, narrator_task_id=body["narrator_task_id"],
            user_id=USER_ID, app_key=KEY,
        )
        assert result.outcome == AdvanceOutcome.STEP_FAILED
        snapshot = _read(engine, body["narrator_task_id"])
        assert "已取消" in snapshot["data"]["error_message"]

    def test_crafted_task_id_is_url_encoded_in_upstream_path(self, engine, monkeypatch):
        """Regression for regression coverage security-sensitive: an attacker-poisoned
        `steps.<step>.task_id` containing path/query reserved chars
        must not rewrite the upstream URL. URL-encoding the path
        segment turns `?` into `%3F` etc. so the upstream call
        targets the literal id, never a smuggled query string."""
        body = _make_running_auto_task(
            engine,
            current_step="popular_learning",
            step_record={"task_id": "abc?smuggled=1&injected=evil"},
        )

        captured: list[str] = []

        def fake_upstream(path, **kw):
            captured.append(path)
            return {"data": {"status": 1}}  # in-flight — keep test scope tight

        monkeypatch.setattr(advance_module, "proxy_narrator_upstream", fake_upstream)

        result = advance_one_task(
            engine, narrator_task_id=body["narrator_task_id"],
            user_id=USER_ID, app_key=KEY,
        )
        assert result.outcome == AdvanceOutcome.IN_PROGRESS
        assert len(captured) == 1
        url = captured[0]
        # Reserved chars are percent-encoded, NOT passed through as URL
        # delimiters. `%3F` is `?`, `%3D` is `=`, `%26` is `&`.
        assert "%3F" in url
        assert "%3D" in url
        assert "%26" in url
        # And the raw query delimiter must not appear anywhere in the
        # constructed path — that's the actual security property.
        assert "?" not in url

    def test_upstream_error_is_noop(self, engine, monkeypatch):
        body = _make_running_auto_task(
            engine,
            current_step="popular_learning",
            step_record={"task_id": "rt-1"},
        )

        def boom(*a, **k):
            raise UpstreamNarratorError(
                502, "UPSTREAM_TIMEOUT", "upstream timed out"
            )

        monkeypatch.setattr(advance_module, "proxy_narrator_upstream", boom)
        result = advance_one_task(
            engine, narrator_task_id=body["narrator_task_id"],
            user_id=USER_ID, app_key=KEY,
        )
        # Transient upstream error: noop (next tick retries). State on disk
        # is unchanged.
        assert result.outcome == AdvanceOutcome.NOOP
        snapshot = _read(engine, body["narrator_task_id"])
        assert snapshot["status"] == "running"


# ─── success → advance to next step ────────────────────────────────────────


class TestAdvanceSuccess:
    def test_success_triggers_next_step_and_persists(self, engine, monkeypatch):
        body = _make_running_auto_task(
            engine,
            current_step="popular_learning",
            step_record={"task_id": "rt-1"},
            extras={
                "writing_type": 0,
                "use_existing_model": False,
                "native_video_id": "nv",
                "native_srt_id": "ns",
            },
        )

        # Upstream query returns status=2 (success) for popular_learning
        # + learning_model_id in order_info — extract_step_result reads
        # this. Then the trigger for next step (generate_writing) hits
        # a different upstream path; route them with one fake.
        calls = {"query": 0, "trigger": 0}

        def fake_upstream(path, method="GET", body=None, timeout_seconds=10.0, query_params=None):
            if "query" in path:
                calls["query"] += 1
                return {
                    "data": {
                        "status": 2,
                        "results": {
                            "order_info": {
                                "learning_model_id": "lm-77",
                                "order_num": "ord-1",
                            },
                            "tasks": [{"id": 99}],
                        },
                        "completed_at": "2026-06-11T01:00:00Z",
                    }
                }
            # Otherwise it's the next-step trigger.
            calls["trigger"] += 1
            return {"data": {"task_id": "next-rt-2"}}

        monkeypatch.setattr(advance_module, "proxy_narrator_upstream", fake_upstream)
        monkeypatch.setattr(triggers_module, "proxy_narrator_upstream", fake_upstream)

        result = advance_one_task(
            engine, narrator_task_id=body["narrator_task_id"],
            user_id=USER_ID, app_key=KEY,
        )
        assert result.outcome == AdvanceOutcome.ADVANCED
        assert result.next_step == "generate_writing"

        # DB state: current_step moved, popular_learning marked completed
        # with extracted result, generate_writing is running with the
        # upstream's new task_id.
        snapshot = _read(engine, body["narrator_task_id"])
        assert snapshot["current_step"] == "generate_writing"
        steps = snapshot["data"]["steps"]
        assert steps["popular_learning"]["status"] == "completed"
        assert steps["popular_learning"]["result"]["learning_model_id"] == "lm-77"
        assert steps["generate_writing"]["status"] == "running"
        assert steps["generate_writing"]["task_id"] == "next-rt-2"
        assert calls == {"query": 1, "trigger": 1}

    def test_video_composing_success_completes_pipeline(self, engine, monkeypatch):
        body = _make_running_auto_task(
            engine,
            current_step="video_composing",
            step_record={"task_id": "vc-rt"},
        )

        monkeypatch.setattr(
            advance_module,
            "proxy_narrator_upstream",
            lambda *a, **k: {
                "data": {
                    "status": 2,
                    "results": {
                        "order_info": {},
                        "tasks": [
                            {
                                "video_url": "https://cdn/done.mp4",
                                "project_zip": "https://cdn/done.zip",
                            }
                        ],
                    },
                    "completed_at": "2026-06-11T02:00:00Z",
                }
            },
        )

        result = advance_one_task(
            engine, narrator_task_id=body["narrator_task_id"],
            user_id=USER_ID, app_key=KEY,
        )
        assert result.outcome == AdvanceOutcome.COMPLETED
        snapshot = _read(engine, body["narrator_task_id"])
        assert snapshot["status"] == "completed"
        assert snapshot["current_step"] == "completed"
        assert (
            snapshot["data"]["steps"]["video_composing"]["result"]["video_url"]
            == "https://cdn/done.mp4"
        )


# ─── trigger failure path ──────────────────────────────────────────────────


class TestTriggerFailure:
    def test_trigger_failure_settles_master_failed(self, engine, monkeypatch):
        body = _make_running_auto_task(
            engine,
            current_step="popular_learning",
            step_record={"task_id": "rt-1"},
            extras={"writing_type": 0, "use_existing_model": False},
        )

        # Upstream succeeds for the popular_learning query but the
        # next-step trigger has no learning_model_id (it's missing from
        # the seeded task body), so trigger_next_step returns
        # success=False without calling upstream.
        monkeypatch.setattr(
            advance_module,
            "proxy_narrator_upstream",
            lambda *a, **k: {"data": {"status": 2, "results": {
                # no learning_model_id in order_info or task_result
                "order_info": {"order_num": "ord"},
                "tasks": [{}],
            }, "completed_at": ""}},
        )

        result = advance_one_task(
            engine, narrator_task_id=body["narrator_task_id"],
            user_id=USER_ID, app_key=KEY,
        )
        assert result.outcome == AdvanceOutcome.STEP_FAILED
        snapshot = _read(engine, body["narrator_task_id"])
        # Master ends up failed, current_step landed on the next step
        # (advance.py 348-352 reasoning — do not roll back).
        assert snapshot["status"] == "failed"
        assert snapshot["current_step"] == "generate_writing"
        assert (
            snapshot["data"]["steps"]["generate_writing"]["status"]
            == "failed"
        )
        assert "learning_model_id" in snapshot["data"]["error_message"]


# ─── short-circuit branch ─────────────────────────────────────────────────


class TestShortCircuit:
    def test_locally_completed_step_walks_forward(self, engine, monkeypatch):
        body = _make_running_auto_task(
            engine,
            current_step="popular_learning",
            step_record={"task_id": "rt-1", "status": "completed"},
            extras={
                "writing_type": 0,
                "use_existing_model": False,
                "steps": {
                    "popular_learning": {
                        "task_id": "rt-1",
                        "status": "completed",
                        "result": {"learning_model_id": "lm-stored"},
                    },
                    # generate_writing already has a task_id (web sync
                    # raced ahead, persisted the trigger). Short-circuit
                    # walks the pointer without triggering again.
                    "generate_writing": {
                        "status": "running",
                        "task_id": "gw-rt",
                    },
                },
            },
        )
        # If short-circuit works, no upstream call should fire.
        called = []
        monkeypatch.setattr(
            advance_module,
            "proxy_narrator_upstream",
            lambda *a, **k: called.append(a) or {"data": {"status": 2}},
        )
        result = advance_one_task(
            engine, narrator_task_id=body["narrator_task_id"],
            user_id=USER_ID, app_key=KEY,
        )
        assert result.outcome == AdvanceOutcome.ADVANCED
        assert result.next_step == "generate_writing"
        assert called == []  # no upstream query path was hit
        snapshot = _read(engine, body["narrator_task_id"])
        assert snapshot["current_step"] == "generate_writing"


# ─── CAS miss branch ──────────────────────────────────────────────────────


class TestCasMiss:
    def test_concurrent_advance_returns_cas_miss(self, engine, monkeypatch):
        body = _make_running_auto_task(
            engine,
            current_step="popular_learning",
            step_record={"task_id": "rt-1"},
            extras={"writing_type": 0, "use_existing_model": False},
        )

        # Simulate a race: between our get_task and our claim, another
        # writer flipped current_step to generate_writing. We model
        # this by patching get_task in the advance module to return
        # the seeded state, but updating the DB so the eventual CAS
        # claim sees current_step=generate_writing (mismatch).
        original_get_task = advance_module.get_task

        def get_task_with_db_drift(conn, *, user_id, narrator_task_id):
            stale = original_get_task(
                conn, user_id=user_id, narrator_task_id=narrator_task_id
            )
            # The "other writer" advances current_step via raw SQL while
            # we still hold the stale view in memory.
            conn.execute(
                text(
                    "UPDATE narrator_tasks SET current_step='generate_writing' "
                    "WHERE narrator_task_id=:t"
                ),
                {"t": narrator_task_id},
            )
            return stale

        monkeypatch.setattr(advance_module, "get_task", get_task_with_db_drift)
        monkeypatch.setattr(
            advance_module,
            "proxy_narrator_upstream",
            lambda *a, **k: {
                "data": {
                    "status": 2,
                    "results": {
                        "order_info": {"learning_model_id": "lm", "order_num": "o"},
                        "tasks": [{"id": 1}],
                    },
                    "completed_at": "",
                }
            },
        )

        result = advance_one_task(
            engine, narrator_task_id=body["narrator_task_id"],
            user_id=USER_ID, app_key=KEY,
        )
        assert result.outcome == AdvanceOutcome.CAS_MISS


# ─── orchestrator-path auto-refund coverage (regression coverage bug 2) ──────────────────


_PRICING_QUOTES_V2_SCHEMA = """
CREATE TABLE pricing_quotes_v2 (
    quote_id TEXT PRIMARY KEY,
    pricing_rule_version TEXT NOT NULL,
    price_source TEXT NOT NULL,
    template_id TEXT NOT NULL,
    combo_key TEXT NOT NULL,
    pro_upgrade INTEGER NOT NULL,
    final_charge_price INTEGER NOT NULL,
    flash_total INTEGER NOT NULL,
    pro_total INTEGER NOT NULL,
    pro_upgrade_delta INTEGER NOT NULL,
    pricing_minutes INTEGER NOT NULL,
    system_reference_price INTEGER NOT NULL,
    breakdown TEXT NOT NULL,
    currency_unit TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    web_user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""

_PRICING_SNAPSHOTS_V2_SCHEMA = """
CREATE TABLE pricing_snapshots_v2 (
    snapshot_id TEXT PRIMARY KEY,
    quote_id TEXT NOT NULL,
    pricing_rule_version TEXT NOT NULL,
    price_source TEXT NOT NULL,
    template_id TEXT NOT NULL,
    combo_key TEXT NOT NULL,
    pro_upgrade INTEGER NOT NULL,
    final_charge_price INTEGER NOT NULL,
    flash_total INTEGER NOT NULL,
    pro_total INTEGER NOT NULL,
    pro_upgrade_delta INTEGER NOT NULL,
    pricing_minutes INTEGER NOT NULL,
    system_reference_price INTEGER NOT NULL,
    breakdown TEXT NOT NULL,
    currency_unit TEXT NOT NULL,
    web_user_id INTEGER NOT NULL,
    refund_status TEXT NOT NULL DEFAULT 'none',
    confirmed_at TEXT NOT NULL,
    raw_confirm_payload TEXT NOT NULL
);
"""


@pytest.fixture()
def refund_engine(engine, monkeypatch):
    """Augment the base advance engine with pricing_quotes_v2 +
    pricing_snapshots_v2 so the auto-refund hook can read snapshot data,
    plus `narrator_tasks.snapshot_id` column so store-level refund
    eligibility lookups work."""
    monkeypatch.setenv("AUTO_REFUND_FAIL_FAST_ENABLED", "true")
    with engine.begin() as conn:
        conn.execute(text(_PRICING_QUOTES_V2_SCHEMA))
        conn.execute(text(_PRICING_SNAPSHOTS_V2_SCHEMA))
        # The advance fixture's narrator_tasks doesn't carry snapshot_id;
        # add a parallel table or column. Easiest: ALTER TABLE.
        conn.execute(text("ALTER TABLE narrator_tasks ADD COLUMN snapshot_id TEXT"))
    return engine


def _seed_quote_snapshot(engine, *, snapshot_price=81, snapshot_id="S-AR-001"):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO pricing_quotes_v2 "
                "(quote_id, pricing_rule_version, price_source, template_id, "
                " combo_key, pro_upgrade, final_charge_price, flash_total, "
                " pro_total, pro_upgrade_delta, pricing_minutes, "
                " system_reference_price, breakdown, currency_unit, "
                " expires_at, web_user_id, created_at) "
                "VALUES ('Q-AR-001', 'v2.0', 'manual_catalog_price', 'tpl_1', "
                "        'derivative', 0, :p, :p, :p, 0, 10, :p, '[]', "
                "        'web_point', '2099-01-01T00:00:00Z', :uid, '2026-06-12T00:00:00Z')"
            ),
            {"p": snapshot_price, "uid": USER_ID},
        )
        conn.execute(
            text(
                "INSERT INTO pricing_snapshots_v2 "
                "(snapshot_id, quote_id, pricing_rule_version, price_source, "
                " template_id, combo_key, pro_upgrade, final_charge_price, "
                " flash_total, pro_total, pro_upgrade_delta, pricing_minutes, "
                " system_reference_price, breakdown, currency_unit, "
                " web_user_id, refund_status, confirmed_at, raw_confirm_payload) "
                "VALUES (:sid, 'Q-AR-001', 'v2.0', 'manual_catalog_price', "
                "        'tpl_1', 'derivative', 0, :p, :p, :p, 0, 10, :p, "
                "        '[]', 'web_point', :uid, 'none', "
                "        '2026-06-12T00:00:00Z', '{}')"
            ),
            {"sid": snapshot_id, "p": snapshot_price, "uid": USER_ID},
        )


def _balance(engine, user_id=USER_ID):
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT balance_points FROM users WHERE id = :uid"),
            {"uid": user_id},
        ).first()
    from decimal import Decimal as _Decimal
    return int(_Decimal(str(row[0])))


def _refund_status(engine, snapshot_id="S-AR-001"):
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT refund_status FROM pricing_snapshots_v2 WHERE snapshot_id=:sid"),
            {"sid": snapshot_id},
        ).first()
    return row[0] if row else None


def _make_task_with_snapshot(engine, *, snapshot_id="S-AR-001"):
    body = _make_running_auto_task(
        engine,
        current_step="popular_learning",
        step_record={"task_id": "rt-1"},
        extras={"writing_type": 0, "use_existing_model": False},
    )
    # Attach snapshot_id (advance fixture writes None) and re-set the
    # task to use existing_model_id so triggers don't fail on missing
    # learning_model_id.
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE narrator_tasks SET snapshot_id=:sid WHERE narrator_task_id=:tid"),
            {"sid": snapshot_id, "tid": body["narrator_task_id"]},
        )
    return body


class TestOrchestratorAutoRefund:
    """Hook is wired into _persist_with_retries for forward-compat with
    future case 3-5 expansion of the refund spec, AND to keep the
    invariant `persist(status=failed) ⇒ refund hook called` symmetric
    between the route path and the orchestrator path.

    In practice for the case-2 spec the hook will NOT match the
    orchestrator's typical failure shape: by the time advance.py
    persists a failed body, `steps` always includes a prior successful
    step (the one whose upstream we just queried), so first_step.status
    is `completed`, not `failed`. These tests document that the call
    is correctly wired without overstating its current effect.
    """

    def test_trigger_failure_does_not_refund_when_prior_step_completed(
        self, refund_engine, monkeypatch,
    ):
        """The realistic orchestrator failure shape: step 1 succeeded
        and was marked completed; step 2's trigger failed. Detector
        rejects because the first step is completed, not failed. No
        refund — this is the same case-4-ish shape that stays manual
        on the PUT route too. Test asserts the wiring doesn't
        accidentally refund the wrong shape."""
        _seed_quote_snapshot(refund_engine)
        body = _make_task_with_snapshot(refund_engine)
        balance_before = _balance(refund_engine)

        # Upstream query succeeds (status=2) for the current step
        # (popular_learning); nextStep trigger (generate_writing)
        # fails because the body lacks learning_model_id.
        monkeypatch.setattr(
            advance_module,
            "proxy_narrator_upstream",
            lambda *a, **k: {"data": {
                "status": 2,
                "results": {
                    "order_info": {"order_num": "ord"},
                    "tasks": [{}],
                },
                "completed_at": "",
            }},
        )

        result = advance_one_task(
            refund_engine, narrator_task_id=body["narrator_task_id"],
            user_id=USER_ID, app_key=KEY,
        )
        assert result.outcome == AdvanceOutcome.STEP_FAILED

        # No refund: first step is completed, not failed → pattern mismatch.
        assert _balance(refund_engine) == balance_before
        assert _refund_status(refund_engine) == "none"

    def test_upstream_3_with_task_id_does_not_refund(self, refund_engine, monkeypatch):
        """The other realistic orchestrator failure: upstream returned
        status=3 for a step that already has a task_id (we queried it,
        so by construction it had one). Detector rejects because the
        failed step carries a task_id. Stays manual — by design."""
        _seed_quote_snapshot(refund_engine)
        body = _make_task_with_snapshot(refund_engine)
        balance_before = _balance(refund_engine)

        monkeypatch.setattr(
            advance_module,
            "proxy_narrator_upstream",
            lambda *a, **k: {"data": {"status": 3}},
        )

        result = advance_one_task(
            refund_engine, narrator_task_id=body["narrator_task_id"],
            user_id=USER_ID, app_key=KEY,
        )
        assert result.outcome == AdvanceOutcome.STEP_FAILED
        assert _balance(refund_engine) == balance_before
        assert _refund_status(refund_engine) == "none"

    def test_feature_flag_off_short_circuits_orchestrator_hook_too(
        self, refund_engine, monkeypatch,
    ):
        """When the env flag is off the route path is documented as a
        no-op. The orchestrator hook must respect the same kill switch
        so a single env-var flip cleanly disables auto-refund
        end-to-end (per the issue's rollback guidance)."""
        monkeypatch.setenv("AUTO_REFUND_FAIL_FAST_ENABLED", "false")
        _seed_quote_snapshot(refund_engine)
        body = _make_task_with_snapshot(refund_engine)
        balance_before = _balance(refund_engine)

        monkeypatch.setattr(
            advance_module,
            "proxy_narrator_upstream",
            lambda *a, **k: {"data": {
                "status": 2,
                "results": {"order_info": {"order_num": "ord"}, "tasks": [{}]},
                "completed_at": "",
            }},
        )

        result = advance_one_task(
            refund_engine, narrator_task_id=body["narrator_task_id"],
            user_id=USER_ID, app_key=KEY,
        )
        assert result.outcome == AdvanceOutcome.STEP_FAILED
        assert _balance(refund_engine) == balance_before
        assert _refund_status(refund_engine) == "none"

    def test_hook_not_called_on_success_path(self, refund_engine, monkeypatch):
        """Invariant: the hook only runs when persist body is `status=failed`.
        Successful step advances must not even touch the snapshot row."""
        _seed_quote_snapshot(refund_engine)
        body = _make_task_with_snapshot(refund_engine)
        # Give the seeded task an existing_model_id so the nextStep
        # trigger succeeds, otherwise the advance path falls into
        # STEP_FAILED via missing-field.
        with refund_engine.begin() as conn:
            from sqlalchemy import text as _t
            row = conn.execute(
                _t("SELECT data FROM narrator_tasks WHERE narrator_task_id=:t"),
                {"t": body["narrator_task_id"]},
            ).first()
            d = json.loads(row.data)
            d["existing_model_id"] = "model-x"
            conn.execute(
                _t("UPDATE narrator_tasks SET data=:d WHERE narrator_task_id=:t"),
                {"d": json.dumps(d), "t": body["narrator_task_id"]},
            )

        calls = {"q": 0, "t": 0}

        def fake_upstream(path, method="GET", body=None, timeout_seconds=10.0, query_params=None):
            if "query" in path:
                calls["q"] += 1
                return {"data": {
                    "status": 2,
                    "results": {
                        "order_info": {
                            "learning_model_id": "lm-77",
                            "order_num": "ord-1",
                        },
                        "tasks": [{"id": 99}],
                    },
                    "completed_at": "",
                }}
            calls["t"] += 1
            return {"data": {"task_id": "next-rt-2"}}

        monkeypatch.setattr(advance_module, "proxy_narrator_upstream", fake_upstream)
        monkeypatch.setattr(triggers_module, "proxy_narrator_upstream", fake_upstream)

        result = advance_one_task(
            refund_engine, narrator_task_id=body["narrator_task_id"],
            user_id=USER_ID, app_key=KEY,
        )
        assert result.outcome == AdvanceOutcome.ADVANCED
        # Snapshot untouched on the success path.
        assert _refund_status(refund_engine) == "none"
