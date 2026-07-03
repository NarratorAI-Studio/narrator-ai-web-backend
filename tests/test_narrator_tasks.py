"""Integration tests for /narrator/tasks .

Strategy: same in-memory sqlite + monkeypatched DB engine pattern as
test_account_me_endpoint.py. The sqlite schema mirrors the production
20260527_0001 DDL (JSONB → TEXT, TIMESTAMPTZ → TEXT, IDENTITY → plain
INTEGER UNIQUE; FK to users(id) parsed but not enforced — sqlite default).
SQLAlchemy's `with_for_update()` is a no-op on sqlite, so the tenant
isolation tests exercise the WHERE-clause filtering rather than the
row-lock, which is the right test for this layer anyway.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text


sqlite3.register_adapter(Decimal, str)


SQLITE_USERS_SCHEMA = """
CREATE TABLE users (
    app_key TEXT PRIMARY KEY,
    id INTEGER NOT NULL UNIQUE,
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

# Two users — KEY_A is id=1, KEY_B is id=2. The format matches
# `users.schema.is_valid_app_key` so require_web_user_auth's regex check
# passes.
KEY_A = "grid_AbCdEfGhIjKlMnOpQrStUv"
KEY_B = "grid_ZyXwVuTsRqPoNmLkJiHgFe"


@pytest.fixture()
def sqlite_engine():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    with engine.begin() as conn:
        conn.execute(text(SQLITE_USERS_SCHEMA))
        conn.execute(text(SQLITE_NARRATOR_TASKS_SCHEMA))
        conn.execute(
            text("INSERT INTO users (app_key, id) VALUES (:k, 1)"), {"k": KEY_A}
        )
        conn.execute(
            text("INSERT INTO users (app_key, id) VALUES (:k, 2)"), {"k": KEY_B}
        )
    yield engine
    engine.dispose()


@pytest.fixture()
def client(sqlite_engine, monkeypatch):
    import server

    monkeypatch.setattr(server, "get_db_engine", lambda: sqlite_engine)
    monkeypatch.setattr(
        server, "get_db_core_connection", lambda: sqlite_engine.connect()
    )
    return server.app.test_client()


# ---------- POST /narrator/tasks (create) ----------


def test_create_assigns_id_and_timestamps(client):
    res = client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json={"narrator_type": "playlet", "status": "pending"},
    )
    assert res.status_code == 200, res.get_json()
    body = res.get_json()
    assert body["success"] is True
    data = body["data"]
    assert data["narrator_task_id"]
    # The generated ID looks like `<ms>_<6 base36 chars>`.
    assert "_" in data["narrator_task_id"]
    assert data["created_at"] == data["updated_at"]
    assert data["app_key"] == KEY_A
    assert data["status"] == "pending"
    assert data["narrator_type"] == "playlet"


def test_create_defaults_status_when_omitted(client):
    res = client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json={"narrator_type": "playlet"},
    )
    assert res.status_code == 200
    assert res.get_json()["data"]["status"] == "pending"


def test_create_rejects_non_json_body(client):
    res = client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        data="not-json",
    )
    assert res.status_code == 400
    body = res.get_json()
    assert body["success"] is False
    assert body["error"]["code"] == "BAD_REQUEST"


def test_create_requires_app_key(client):
    res = client.post(
        "/narrator/tasks",
        headers={"Content-Type": "application/json"},
        json={"narrator_type": "playlet"},
    )
    assert res.status_code == 401
    assert res.get_json()["error"]["code"] == "WEB_APP_KEY_MISSING"


def test_create_rejects_unknown_app_key(client):
    res = client.post(
        "/narrator/tasks",
        headers={
            "X-Web-App-Key": "grid_AAAAAAAAAAAAAAAAAAAAAA",
            "Content-Type": "application/json",
        },
        json={"narrator_type": "playlet"},
    )
    assert res.status_code == 401
    assert res.get_json()["error"]["code"] == "WEB_APP_KEY_UNKNOWN"


# ---------- POST /narrator/tasks (upsert with client-supplied ID) ----------


def test_upsert_same_tenant_preserves_id_and_created_at(client):
    first = client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json={"narrator_task_id": "task_111", "status": "pending"},
    )
    assert first.status_code == 200
    first_data = first.get_json()["data"]
    assert first_data["narrator_task_id"] == "task_111"
    original_created = first_data["created_at"]

    second = client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json={
            "narrator_task_id": "task_111",
            "status": "running",
            "current_step": "clip_data",
        },
    )
    assert second.status_code == 200
    second_data = second.get_json()["data"]
    assert second_data["narrator_task_id"] == "task_111"
    assert second_data["status"] == "running"
    assert second_data["current_step"] == "clip_data"
    assert second_data["created_at"] == original_created
    assert second_data["updated_at"] >= original_created


def test_upsert_cross_tenant_403(client):
    # KEY_A inserts.
    client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json={"narrator_task_id": "task_222"},
    )
    # KEY_B tries to overwrite.
    res = client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": KEY_B, "Content-Type": "application/json"},
        json={"narrator_task_id": "task_222", "status": "hijacked"},
    )
    assert res.status_code == 403
    assert res.get_json()["error"]["code"] == "FORBIDDEN"

    # KEY_A's row is untouched.
    fetched = client.get(
        "/narrator/tasks/task_222", headers={"X-Web-App-Key": KEY_A}
    )
    assert fetched.status_code == 200
    assert fetched.get_json()["data"]["status"] != "hijacked"


# ---------- GET /narrator/tasks (list) ----------


def test_list_scopes_to_caller_with_pagination(client):
    for i, status in enumerate(["pending", "running", "completed"]):
        client.post(
            "/narrator/tasks",
            headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
            json={"status": status, "seq": i},
        )
    client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": KEY_B, "Content-Type": "application/json"},
        json={"status": "pending"},
    )

    res_a = client.get("/narrator/tasks", headers={"X-Web-App-Key": KEY_A})
    assert res_a.status_code == 200
    data_a = res_a.get_json()["data"]
    assert data_a["total"] == 3
    assert len(data_a["items"]) == 3

    res_b = client.get("/narrator/tasks", headers={"X-Web-App-Key": KEY_B})
    assert res_b.status_code == 200
    data_b = res_b.get_json()["data"]
    assert data_b["total"] == 1


def test_list_status_filter(client):
    for status in ["pending", "running", "completed"]:
        client.post(
            "/narrator/tasks",
            headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
            json={"status": status},
        )
    res = client.get(
        "/narrator/tasks?status=running", headers={"X-Web-App-Key": KEY_A}
    )
    assert res.status_code == 200
    data = res.get_json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["status"] == "running"


def test_list_validates_pagination(client):
    for bad in ["?page=0", "?page=-1", "?limit=0", "?limit=99999", "?page=abc"]:
        res = client.get(
            f"/narrator/tasks{bad}", headers={"X-Web-App-Key": KEY_A}
        )
        assert res.status_code == 400, f"expected 400 for {bad}, got {res.status_code}"


# ---------- GET /narrator/tasks/<id> ----------


def test_get_returns_owned_task(client):
    created = client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json={"status": "pending"},
    ).get_json()["data"]
    task_id = created["narrator_task_id"]

    res = client.get(f"/narrator/tasks/{task_id}", headers={"X-Web-App-Key": KEY_A})
    assert res.status_code == 200
    assert res.get_json()["data"]["narrator_task_id"] == task_id


def test_get_cross_tenant_returns_404(client):
    created = client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json={"status": "pending"},
    ).get_json()["data"]
    task_id = created["narrator_task_id"]
    res = client.get(f"/narrator/tasks/{task_id}", headers={"X-Web-App-Key": KEY_B})
    assert res.status_code == 404
    assert res.get_json()["error"]["code"] == "NOT_FOUND"


def test_get_missing_returns_404(client):
    res = client.get(
        "/narrator/tasks/does_not_exist", headers={"X-Web-App-Key": KEY_A}
    )
    assert res.status_code == 404


# ---------- PUT /narrator/tasks/<id> (replace + CAS) ----------


def test_replace_happy_path(client):
    created = client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json={"status": "pending"},
    ).get_json()["data"]
    task_id = created["narrator_task_id"]

    res = client.put(
        f"/narrator/tasks/{task_id}",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json={
            "narrator_task_id": task_id,
            "status": "running",
            "current_step": "popular_learning",
        },
    )
    assert res.status_code == 200
    data = res.get_json()["data"]
    assert data["status"] == "running"
    assert data["current_step"] == "popular_learning"
    assert data["created_at"] == created["created_at"]


def test_replace_cas_status_match_200(client):
    created = client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json={"status": "pending"},
    ).get_json()["data"]
    task_id = created["narrator_task_id"]

    res = client.put(
        f"/narrator/tasks/{task_id}?expected_status=pending",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json={"status": "running"},
    )
    assert res.status_code == 200


def test_replace_cas_status_mismatch_409(client):
    created = client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json={"status": "pending"},
    ).get_json()["data"]
    task_id = created["narrator_task_id"]

    res = client.put(
        f"/narrator/tasks/{task_id}?expected_status=running,paused",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json={"status": "completed"},
    )
    assert res.status_code == 409
    assert res.get_json()["error"]["code"] == "CAS_MISMATCH"

    # The row should still be in its original 'pending' state since the
    # write was rejected.
    fetched = client.get(
        f"/narrator/tasks/{task_id}", headers={"X-Web-App-Key": KEY_A}
    )
    assert fetched.get_json()["data"]["status"] == "pending"


def test_replace_cas_step_mismatch_409(client):
    client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json={
            "narrator_task_id": "task_step",
            "status": "running",
            "current_step": "popular_learning",
        },
    )
    res = client.put(
        "/narrator/tasks/task_step?expected_status=running&expected_step=clip_data",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json={"status": "running", "current_step": "video_composing"},
    )
    assert res.status_code == 409


def test_replace_cas_status_and_step_match_200(client):
    client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json={
            "narrator_task_id": "task_both",
            "status": "running",
            "current_step": "popular_learning",
        },
    )
    res = client.put(
        "/narrator/tasks/task_both?expected_status=running&expected_step=popular_learning",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json={"status": "running", "current_step": "clip_data"},
    )
    assert res.status_code == 200
    assert res.get_json()["data"]["current_step"] == "clip_data"


def test_replace_cross_tenant_404(client):
    created = client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json={"status": "pending"},
    ).get_json()["data"]
    task_id = created["narrator_task_id"]
    res = client.put(
        f"/narrator/tasks/{task_id}",
        headers={"X-Web-App-Key": KEY_B, "Content-Type": "application/json"},
        json={"status": "hijacked"},
    )
    assert res.status_code == 404


def test_replace_missing_404(client):
    res = client.put(
        "/narrator/tasks/missing",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json={"status": "running"},
    )
    assert res.status_code == 404


def test_replace_rejects_id_mismatch(client):
    client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json={"narrator_task_id": "task_real", "status": "pending"},
    )
    res = client.put(
        "/narrator/tasks/task_real",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json={"narrator_task_id": "task_other", "status": "running"},
    )
    assert res.status_code == 400
    assert res.get_json()["error"]["code"] == "BAD_REQUEST"


# ---------- spoofed app_key in body must be overridden by the server
# ----------
#
# All three write surfaces must overwrite any client-supplied `app_key`
# field on the persisted JSON body with the authenticated `X-Web-App-Key`.
# Otherwise downstream consumers (GET / list, logs, debug) see attribution
# that doesn't match the row's true owner (review feedback).


def test_create_overrides_spoofed_app_key(client):
    res = client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json={"status": "pending", "app_key": KEY_B},
    )
    assert res.status_code == 200
    assert res.get_json()["data"]["app_key"] == KEY_A
    # And a subsequent GET also returns the authenticated key, not KEY_B.
    task_id = res.get_json()["data"]["narrator_task_id"]
    fetched = client.get(
        f"/narrator/tasks/{task_id}", headers={"X-Web-App-Key": KEY_A}
    )
    assert fetched.get_json()["data"]["app_key"] == KEY_A


def test_upsert_overrides_spoofed_app_key(client):
    res = client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json={"narrator_task_id": "task_spoof_a", "status": "pending", "app_key": KEY_B},
    )
    assert res.status_code == 200
    assert res.get_json()["data"]["app_key"] == KEY_A
    fetched = client.get(
        "/narrator/tasks/task_spoof_a", headers={"X-Web-App-Key": KEY_A}
    )
    assert fetched.get_json()["data"]["app_key"] == KEY_A


def test_replace_overrides_spoofed_app_key(client):
    client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json={"narrator_task_id": "task_spoof_r", "status": "pending"},
    )
    res = client.put(
        "/narrator/tasks/task_spoof_r",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json={"status": "running", "app_key": KEY_B},
    )
    assert res.status_code == 200
    assert res.get_json()["data"]["app_key"] == KEY_A
    fetched = client.get(
        "/narrator/tasks/task_spoof_r", headers={"X-Web-App-Key": KEY_A}
    )
    assert fetched.get_json()["data"]["app_key"] == KEY_A


# ---------- hot-column type validation ----------
#
# Bad client types on the columns we forward to the DB (narrator_task_id,
# status, current_step) must surface as 400 BAD_REQUEST, never as a 503
# from the route's catch-all exception handler — otherwise a client typo
# looks like a store outage (security hardening).


@pytest.mark.parametrize(
    "body",
    [
        {"narrator_task_id": 123, "status": "pending"},
        {"narrator_task_id": ""},
        {"narrator_task_id": ["a"]},
        {"status": ["pending"]},
        {"status": 42},
        {"current_step": {"x": 1}},
        {"current_step": 7},
    ],
)
def test_create_rejects_bad_hot_column_types(client, body):
    res = client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json=body,
    )
    assert res.status_code == 400, res.get_json()
    assert res.get_json()["error"]["code"] == "BAD_REQUEST"


@pytest.mark.parametrize(
    "body",
    [
        {"status": []},
        {"current_step": []},
        {"narrator_task_id": 5},
    ],
)
def test_replace_rejects_bad_hot_column_types(client, body):
    # Seed a row to target.
    client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json={"narrator_task_id": "task_typecheck", "status": "pending"},
    )
    res = client.put(
        "/narrator/tasks/task_typecheck",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json=body,
    )
    assert res.status_code == 400, res.get_json()
    assert res.get_json()["error"]["code"] == "BAD_REQUEST"


# ---------- concurrent-insert race recovery ----------


def test_upsert_recovers_from_concurrent_insert(client, sqlite_engine, monkeypatch):
    """`SELECT ... FOR UPDATE` does not lock a non-existent primary key
    in Postgres, so two concurrent same-tenant upserts with the same
    `narrator_task_id` can both observe `existing is None` and race to
    INSERT — one wins, the other gets a UNIQUE violation. Without the
    SAVEPOINT-and-retry fallback, the loser bubbles up as a 503, breaking
    the orchestrator's idempotent-resubmit semantics (review
    feedback).

    The race is simulated by sneaking a racing INSERT in via a separate
    connection from inside `_build_full_body`, which is called between
    upsert_task's SELECT (sees None) and its own INSERT (now races).
    """
    from narrator_tasks import store

    real_build = store._build_full_body
    raced = {"done": False}

    def racing_build(**kw):
        if not raced["done"] and kw["task_id"] == "task_race":
            raced["done"] = True
            with sqlite_engine.begin() as rconn:
                rconn.execute(
                    text(
                        "INSERT INTO narrator_tasks "
                        "(narrator_task_id, user_id, app_key, status, current_step, data, created_at, updated_at) "
                        "VALUES ('task_race', 1, :ak, 'pending', NULL, :data, "
                        "        '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
                    ),
                    {
                        "ak": KEY_A,
                        "data": (
                            '{"narrator_task_id":"task_race","app_key":"'
                            + KEY_A
                            + '","status":"pending","created_at":"2026-01-01T00:00:00+00:00"}'
                        ),
                    },
                )
        return real_build(**kw)

    monkeypatch.setattr(store, "_build_full_body", racing_build)

    res = client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json={
            "narrator_task_id": "task_race",
            "status": "running",
            "current_step": "clip_data",
        },
    )

    # Recovery → 200, not 503. The status from the second submission wins;
    # `created_at` is preserved from the racing insert (since that row was
    # the "first" in the recovery branch's eyes).
    assert res.status_code == 200, res.get_json()
    data = res.get_json()["data"]
    assert data["narrator_task_id"] == "task_race"
    assert data["status"] == "running"
    assert data["current_step"] == "clip_data"
    assert data["app_key"] == KEY_A
    assert data["created_at"] == "2026-01-01T00:00:00+00:00"


def test_upsert_recovers_with_cross_tenant_lands_403(
    client, sqlite_engine, monkeypatch
):
    """Race-recovery edge case: the row was inserted by a different
    tenant between our SELECT FOR UPDATE (saw None) and our INSERT (raises
    IntegrityError). The recovery branch's re-acquired SELECT FOR UPDATE
    must see the new row, observe its `user_id != self`, and return 403
    — not 200 (which would let cross-tenant data leak) and not 503
    (which would mask the real cause)."""
    from narrator_tasks import store

    real_build = store._build_full_body
    raced = {"done": False}

    def racing_build(**kw):
        if not raced["done"] and kw["task_id"] == "task_xtenant_race":
            raced["done"] = True
            # KEY_B (user_id=2) wins the race.
            with sqlite_engine.begin() as rconn:
                rconn.execute(
                    text(
                        "INSERT INTO narrator_tasks "
                        "(narrator_task_id, user_id, app_key, status, current_step, data, created_at, updated_at) "
                        "VALUES ('task_xtenant_race', 2, :ak, 'pending', NULL, "
                        "        '{\"narrator_task_id\":\"task_xtenant_race\",\"app_key\":\"' || :ak || '\",\"status\":\"pending\"}', "
                        "        '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
                    ),
                    {"ak": KEY_B},
                )
        return real_build(**kw)

    monkeypatch.setattr(store, "_build_full_body", racing_build)

    # KEY_A tries to upsert; the racing winner is KEY_B → should land 403.
    res = client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json={"narrator_task_id": "task_xtenant_race", "status": "hijacked"},
    )
    assert res.status_code == 403
    assert res.get_json()["error"]["code"] == "FORBIDDEN"


def test_upsert_race_recovery_avoids_time_inversion(
    client, sqlite_engine, monkeypatch
):
    """Race recovery must capture a fresh `now` for the eventual UPDATE.
    If the recovery branch reuses the pre-race `now` (captured before
    the IntegrityError), the resulting `updated_at` can land earlier
    than the racing winner's `created_at`, inverting the timeline and
    breaking ORDER BY updated_at DESC list ordering (review
    review).

    Sequence with the patched clock:
      1. upsert_task starts → `now = t1` (early).
      2. racing_build sneaks an INSERT with created_at = t_winner
         (between t1 and t2).
      3. Our INSERT raises IntegrityError → savepoint rollback.
      4. Recovery branch must call _utcnow() again → t2 (later than
         t_winner). _apply_update writes updated_at = t2.

    Without the fix, _apply_update would receive the stale t1 and
    write updated_at = t1.isoformat() — earlier than the racing
    winner's created_at = t_winner. The assertion at the bottom would
    fail with that bug live.
    """
    from datetime import datetime, timezone

    from narrator_tasks import store

    t1 = datetime(2026, 5, 27, 0, 0, 0, tzinfo=timezone.utc)
    t_winner = "2026-05-27T00:02:00+00:00"
    t2 = datetime(2026, 5, 27, 0, 5, 0, tzinfo=timezone.utc)

    clock = iter([t1, t2, t2, t2, t2])  # generous tail in case of refactor
    monkeypatch.setattr(store, "_utcnow", lambda: next(clock))

    real_build = store._build_full_body
    raced = {"done": False}

    def racing_build(**kw):
        if not raced["done"] and kw["task_id"] == "task_time_race":
            raced["done"] = True
            with sqlite_engine.begin() as rconn:
                rconn.execute(
                    text(
                        "INSERT INTO narrator_tasks "
                        "(narrator_task_id, user_id, app_key, status, current_step, data, created_at, updated_at) "
                        "VALUES ('task_time_race', 1, :ak, 'pending', NULL, :data, "
                        "        '2026-05-27T00:02:00', '2026-05-27T00:02:00')"
                    ),
                    {
                        "ak": KEY_A,
                        "data": (
                            '{"narrator_task_id":"task_time_race","app_key":"'
                            + KEY_A
                            + '","status":"pending","created_at":"'
                            + t_winner
                            + '"}'
                        ),
                    },
                )
        return real_build(**kw)

    monkeypatch.setattr(store, "_build_full_body", racing_build)

    res = client.post(
        "/narrator/tasks",
        headers={"X-Web-App-Key": KEY_A, "Content-Type": "application/json"},
        json={
            "narrator_task_id": "task_time_race",
            "status": "running",
            "current_step": "clip_data",
        },
    )
    assert res.status_code == 200, res.get_json()
    data = res.get_json()["data"]
    assert data["created_at"] == t_winner
    assert data["updated_at"] >= data["created_at"], (
        f"time inversion: updated_at={data['updated_at']} < created_at={data['created_at']}"
    )
    # With the fix the recovery branch's _utcnow() returns t2; without
    # the fix updated_at would be t1.isoformat() and the assertion above
    # would fail first.
    assert data["updated_at"] == t2.isoformat()
