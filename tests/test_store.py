from __future__ import annotations

import copy
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest

from tracedesk.errors import DomainError
from tracedesk.store import SCHEMA_VERSION, Store, utc_text

NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
CUSTOMERS = [
    {"id": "alice", "name": "林女士", "tenant_id": "shop"},
    {"id": "bob", "name": "陈先生", "tenant_id": "shop"},
    {"id": "carol", "name": "周女士", "tenant_id": "other-shop"},
]


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value


def orders() -> list[dict]:
    return [
        {
            "id": f"order-{customer['id']}",
            "customer_id": customer["id"],
            "tenant_id": customer["tenant_id"],
            "product": "键盘",
            "category": "physical",
            "amount_minor": 12900,
            "currency": "CNY",
            "status": "delivered",
            "delivered_at": utc_text(NOW - timedelta(days=2)),
            "version": 1,
        }
        for customer in CUSTOMERS
    ]


@pytest.fixture
def environment(tmp_path: Path) -> tuple[Store, Clock, dict, dict]:
    clock = Clock()
    store = Store(tmp_path / "state.db", clock=clock)
    store.initialize(CUSTOMERS, orders())
    created = store.create_session("alice")
    return store, clock, store.authenticate(created["token"]), created


def turn(
    store: Store, run: dict, *, message: str = "查订单", request_id: str = "request-1"
) -> dict:
    return {
        "id": "turn-" + run["id"],
        "run_id": run["id"],
        "request_id": request_id,
        "message": message,
        "answer": "订单已签收。",
        "status": "answered",
        "runtime": "extractive",
        "citations": [],
        "order": None,
        "action": None,
        "trace": [],
        "created_at": utc_text(store.now()),
    }


def assert_error(code: str, callback, status: int | None = None) -> DomainError:
    with pytest.raises(DomainError) as caught:
        callback()
    assert caught.value.code == code
    if status is not None:
        assert caught.value.status_code == status
    return caught.value


def test_schema_and_hashed_bearer(environment) -> None:
    store, _, session, created = environment
    with store.transaction(write=False) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        row = dict(
            connection.execute("SELECT * FROM sessions WHERE id = ?", (session["id"],)).fetchone()
        )
    assert "token" not in row
    assert len(row["token_hash"]) == 64
    assert created["token"] not in repr(row)
    assert session["customer_name"] == "林女士"
    assert store.customer_list()[0] == {
        "id": "alice",
        "name": "林女士",
        "order_ids": ["order-alice"],
    }
    assert_error("UNAUTHORIZED", lambda: store.authenticate("incorrect"), 401)
    assert_error(
        "UNAUTHORIZED", lambda: store.authenticate(created["token"], "another-session"), 401
    )


def test_reopen_keeps_order_state_session_and_context(environment) -> None:
    store, clock, session, created = environment
    original = store.get_order(session, "order-alice")
    with store.transaction() as connection:
        connection.execute(
            "UPDATE orders SET amount_minor = 5000, version = version + 1 WHERE id = 'order-alice'"
        )
    clock.value += timedelta(days=1)
    reopened = Store(store.database_path, clock=clock)
    newer_seed = orders()
    newer_seed[0]["delivered_at"] = utc_text(clock.value)
    reopened.initialize(CUSTOMERS, newer_seed)
    restored = reopened.authenticate(created["token"], session["id"])
    order = reopened.get_order(restored, "order-alice")
    assert order["amount_minor"] == 5000
    assert order["version"] == 2
    assert order["delivered_at"] == original["delivered_at"]
    assert reopened.session_context(restored) == "order-alice"


@pytest.mark.parametrize("target", ["order-bob", "order-carol", "nonexistent"])
def test_foreign_and_missing_orders_are_indistinguishable(environment, target) -> None:
    store, _, session, _ = environment
    assert_error("ORDER_NOT_FOUND", lambda: store.get_order(session, target), 404)
    assert store.session_context(session) is None


def test_session_context_and_runs_are_session_scoped(environment) -> None:
    store, _, session, _ = environment
    created = store.create_session("alice")
    another_session = store.authenticate(created["token"])
    run = store.start_run(session, "查订单", "request-1", "extractive")
    store.get_order(session, "order-alice")
    assert store.session_context(session) == "order-alice"
    assert store.session_context(another_session) is None
    assert_error("RUN_NOT_FOUND", lambda: store.run_details(another_session, run["id"]), 404)
    store.start_run(another_session, "查订单", "request-1", "extractive")


def test_success_is_an_immutable_replayable_snapshot(environment) -> None:
    store, _, session, _ = environment
    run = store.start_run(session, "查订单", "request-1", "extractive")
    payload = turn(store, run)
    expected = copy.deepcopy(payload)
    store.finish_run(session, run["id"], payload)
    payload["answer"] = "caller changed its object"
    payload["trace"].append({"unexpected": True})
    replay = store.start_run(session, "查订单", "request-1", "llm")
    assert replay == {"id": run["id"], "replayed": True, "turn": expected}
    assert store.list_turns(session) == [expected]
    assert store.run_details(session, run["id"])["status"] == "completed"
    assert_error(
        "REQUEST_CONFLICT",
        lambda: store.start_run(session, "修改后的消息", "request-1", "extractive"),
        409,
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store.transaction() as connection:
            connection.execute(
                "UPDATE turns SET payload_json = '{}' WHERE run_id = ?", (run["id"],)
            )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store.transaction() as connection:
            connection.execute("DELETE FROM turns WHERE run_id = ?", (run["id"],))


def test_busy_conflict_and_failed_replay_release_session(environment) -> None:
    store, _, session, _ = environment
    run = store.start_run(session, "查订单", "request-1", "extractive")
    assert_error(
        "RUN_IN_PROGRESS",
        lambda: store.start_run(session, "查订单", "request-1", "extractive"),
        409,
    )
    assert_error(
        "REQUEST_CONFLICT",
        lambda: store.start_run(session, "另一条", "request-1", "extractive"),
        409,
    )
    assert_error(
        "SESSION_BUSY", lambda: store.start_run(session, "另一条", "request-2", "extractive"), 409
    )
    error = DomainError("PROVIDER_TIMEOUT", "模型调用超时。", 504, {"attempts": 1})
    trace = [{"seq": 1, "stage": "model", "status": "error", "detail": "timeout"}]
    store.fail_run(session, run["id"], error, trace)
    replay = store.start_run(session, "查订单", "request-1", "extractive")
    assert replay["replayed"] is True
    assert "turn" not in replay
    assert replay["error"] == {
        "code": "PROVIDER_TIMEOUT",
        "message": "模型调用超时。",
        "status_code": 504,
        "details": {"attempts": 1},
    }
    assert store.run_details(session, run["id"])["trace"] == trace
    assert store.list_turns(session) == []
    assert store.start_run(session, "另一条", "request-2", "extractive")["replayed"] is False


def test_run_lease_at_boundary_persists_interruption_and_allows_new_request(environment) -> None:
    store, clock, session, _ = environment
    run = store.start_run(session, "查订单", "request-1", "extractive")
    clock.value += timedelta(seconds=299)
    assert_error(
        "RUN_IN_PROGRESS", lambda: store.start_run(session, "查订单", "request-1", "extractive")
    )
    clock.value += timedelta(seconds=1)
    replay = store.start_run(session, "查订单", "request-1", "extractive")
    assert replay["error"]["code"] == "INTERRUPTED"
    assert "turn" not in replay
    detail = store.run_details(session, run["id"])
    assert detail["status"] == "interrupted"
    assert detail["trace"][-1]["label"] == "运行中断"
    new = store.start_run(session, "继续", "request-2", "extractive")
    assert new["id"] != run["id"]
    assert_error(
        "RUN_NOT_ACTIVE", lambda: store.finish_run(session, run["id"], turn(store, run)), 409
    )
    assert store.list_turns(session) == []


def test_expiration_survives_a_request_conflict_and_a_late_finish(environment) -> None:
    store, clock, session, _ = environment
    run = store.start_run(session, "查订单", "request-1", "extractive")
    clock.value += timedelta(seconds=301)
    assert_error(
        "REQUEST_CONFLICT", lambda: store.start_run(session, "不同内容", "request-1", "extractive")
    )
    with store.transaction(write=False) as connection:
        assert (
            connection.execute("SELECT status FROM runs WHERE id = ?", (run["id"],)).fetchone()[0]
            == "interrupted"
        )
    new = store.start_run(session, "继续", "request-2", "extractive")
    clock.value += timedelta(seconds=300)
    assert_error(
        "RUN_NOT_ACTIVE",
        lambda: store.finish_run(
            session, new["id"], turn(store, new, message="继续", request_id="request-2")
        ),
    )
    assert (
        store.start_run(session, "继续", "request-2", "extractive")["error"]["code"]
        == "INTERRUPTED"
    )


def test_reopen_recovers_failed_and_orphaned_runs(environment) -> None:
    store, clock, session, created = environment
    run = store.start_run(session, "查订单", "request-1", "extractive")
    clock.value += timedelta(seconds=301)
    reopened = Store(store.database_path, clock=clock)
    reopened.initialize([], [])
    restored = reopened.authenticate(created["token"])
    assert reopened.run_details(restored, run["id"])["status"] == "interrupted"
    next_run = reopened.start_run(restored, "继续", "request-2", "extractive")
    reopened.fail_run(restored, next_run["id"], DomainError("TOOL_FAILED", "工具失败。", 500), [])
    again = Store(store.database_path, clock=clock)
    again.initialize([], [])
    assert (
        again.start_run(restored, "继续", "request-2", "extractive")["error"]["code"]
        == "TOOL_FAILED"
    )


def test_turn_identity_mismatch_rolls_back_without_silent_completion(environment) -> None:
    store, _, session, _ = environment
    run = store.start_run(session, "查订单", "request-1", "extractive")
    payload = turn(store, run)
    payload["request_id"] = "wrong-request"
    with pytest.raises(ValueError, match="request_id"):
        store.finish_run(session, run["id"], payload)
    assert store.list_turns(session) == []
    assert store.run_details(session, run["id"])["status"] == "running"
    store.fail_run(session, run["id"], DomainError("INTERNAL_ERROR", "回答持久化失败。", 500), [])
    store.start_run(session, "恢复", "request-2", "extractive")


def test_database_transaction_and_foreign_key_invariants(environment) -> None:
    store, _, session, _ = environment
    with pytest.raises(RuntimeError, match="abort"):
        with store.transaction() as connection:
            connection.execute(
                "UPDATE orders SET amount_minor = 1, version = version + 1 WHERE id = 'order-alice'"
            )
            raise RuntimeError("abort")
    assert store.get_order(session, "order-alice")["amount_minor"] == 12900
    with pytest.raises(sqlite3.IntegrityError, match="advance version"):
        with store.transaction() as connection:
            connection.execute("UPDATE orders SET amount_minor = 1 WHERE id = 'order-alice'")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        with store.transaction() as connection:
            connection.execute(
                "UPDATE sessions SET last_order_id = 'order-bob' WHERE id = ?", (session["id"],)
            )


def test_concurrent_distinct_runs_have_one_session_owner(environment) -> None:
    store, _, session, _ = environment
    barrier = Barrier(2)

    def start(index: int):
        barrier.wait(timeout=5)
        try:
            return store.start_run(session, f"message-{index}", f"request-{index}", "extractive")
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(start, [1, 2]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("SESSION_BUSY") == 1


def test_unknown_schema_is_rejected_without_overwriting(tmp_path: Path) -> None:
    path = tmp_path / "foreign.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE valuable_data (value TEXT)")
        connection.execute("INSERT INTO valuable_data VALUES ('keep')")
    assert_error("SCHEMA_INCOMPATIBLE", lambda: Store(path).initialize([], []), 500)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM valuable_data").fetchone()[0] == "keep"


def test_future_schema_version_is_explicitly_rejected(environment) -> None:
    store, _, _, _ = environment
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("PRAGMA user_version = 99")
    assert_error("SCHEMA_INCOMPATIBLE", lambda: Store(store.database_path).initialize([], []), 500)


def test_bad_seed_rolls_back_schema_and_can_be_corrected(tmp_path: Path) -> None:
    store = Store(tmp_path / "bad-seed.db")
    invalid = orders()
    invalid[0]["tenant_id"] = "other-shop"
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        store.initialize(CUSTOMERS, invalid)
    store.initialize(CUSTOMERS, orders())
    assert len(store.customer_list()) == 3


def test_clock_requires_awareness_and_normalizes_utc(tmp_path: Path) -> None:
    naive = Store(tmp_path / "naive.db", clock=lambda: datetime(2026, 10, 16))
    with pytest.raises(ValueError, match="timezone-aware"):
        naive.now()
    offset = timezone(timedelta(hours=8))
    aware = Store(tmp_path / "aware.db", clock=lambda: datetime(2026, 10, 16, 20, tzinfo=offset))
    assert aware.now() == NOW
    assert aware.now().tzinfo is timezone.utc


def legacy_store(tmp_path: Path) -> Store:
    from tracedesk.store import SCHEMA

    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        for statement in SCHEMA:
            connection.execute(statement)
        Store._seed(connection, CUSTOMERS, orders())
        connection.execute("INSERT INTO metadata VALUES ('seeded', '1')")
        connection.execute("PRAGMA user_version = 1")
    return Store(path, clock=lambda: NOW)


def test_schema_one_migration_preserves_real_refund_receipt_and_replay(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from tracedesk.refunds import RefundService

    class Catalog:
        def get(self, document_id, *, tenant_id, as_of):
            return SimpleNamespace(
                source_id="returns@1",
                rules={
                    "window_days": 7,
                    "allowed_statuses": ("delivered",),
                    "excluded_categories": (),
                },
            )

    store = legacy_store(tmp_path)
    created = store.create_session("alice")
    session = store.authenticate(created["token"])
    # Seed the released v1 request shape before any v2 application migration runs.
    run = {"id": "legacy-refund-run"}
    with store.transaction() as connection:
        connection.execute(
            """INSERT INTO runs(id,session_id,request_id,message,runtime,status,created_at,lease_expires_at)
               VALUES (?,?,'legacy-run','退款','extractive','running',?,?)""",
            (run["id"], session["id"], utc_text(NOW), utc_text(NOW + timedelta(minutes=5))),
        )
    refunds = RefundService(store, Catalog())
    action = refunds.prepare(session, "order-alice", "尺寸不合适", run["id"])
    receipt = refunds.confirm(session, action["id"], action["confirmation_token"], "legacy-confirm")
    with store.transaction(write=False) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        columns = {
            table: ",".join(row[1] for row in connection.execute(f"PRAGMA table_info({table})"))
            for table in tables
        }
        before = {
            table: [
                tuple(row) for row in connection.execute(f"SELECT {columns[table]} FROM {table}")
            ]
            for table in tables
        }
    reopened = Store(store.database_path, clock=lambda: NOW)
    reopened.initialize([], [])
    with reopened.transaction(write=False) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        after = {
            table: [
                tuple(row) for row in connection.execute(f"SELECT {columns[table]} FROM {table}")
            ]
            for table in tables
        }
        assert connection.execute("SELECT request_context FROM runs").fetchone()[0] == "{}"
    assert before == after
    restored = reopened.authenticate(created["token"])
    replay = RefundService(reopened, Catalog()).confirm(
        restored, action["id"], action["confirmation_token"], "legacy-confirm"
    )
    assert replay == {**receipt, "replayed": True}
    reopened.initialize([], [])


@pytest.mark.parametrize(
    "corruption", ["missing_trigger", "foreign_key", "missing_seed", "extra_table"]
)
def test_schema_one_migration_rejects_damage_without_partial_upgrade(
    tmp_path: Path, corruption: str
) -> None:
    store = legacy_store(tmp_path)
    created = store.create_session("alice")
    with sqlite3.connect(store.database_path) as connection:
        if corruption == "missing_trigger":
            connection.execute("DROP TRIGGER order_updates_advance_version")
        elif corruption == "foreign_key":
            connection.execute("UPDATE sessions SET last_order_id = 'missing-order'")
        elif corruption == "missing_seed":
            connection.execute("DELETE FROM metadata WHERE key = 'seeded'")
        else:
            connection.execute("CREATE TABLE private_extension(value TEXT)")
            connection.execute("INSERT INTO private_extension VALUES ('keep')")
    expected = (
        "SCHEMA_INCOMPATIBLE"
        if corruption in {"missing_trigger", "extra_table"}
        else "DATABASE_INTEGRITY"
    )
    assert_error(expected, lambda: store.initialize([], []), 500)
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE name LIKE 'knowledge_%'"
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
    assert store.authenticate(created["token"])["customer_id"] == "alice"


def test_schema_migration_ddl_failure_rolls_back_every_added_object(
    tmp_path: Path, monkeypatch
) -> None:
    import tracedesk.knowledge.schema as knowledge_schema

    store = legacy_store(tmp_path)
    monkeypatch.setattr(
        knowledge_schema,
        "KNOWLEDGE_SCHEMA",
        (*knowledge_schema.KNOWLEDGE_SCHEMA, "INVALID SQL FOR ATOMIC MIGRATION TEST"),
    )
    with pytest.raises(sqlite3.OperationalError):
        store.initialize([], [])
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM orders").fetchone()[0] == 3
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE name LIKE 'knowledge_%'"
            ).fetchone()[0]
            == 0
        )
