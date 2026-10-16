from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from types import MappingProxyType

import pytest

from tracedesk.errors import DomainError
from tracedesk.refunds import RefundService
from tracedesk.retrieval import KnowledgeBase
from tracedesk.store import Store, utc_text

NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value


@dataclass(frozen=True)
class Policy:
    source_id: str
    rules: MappingProxyType


class PolicyCatalog:
    """A mutable catalog makes policy activation changes controllable in unit tests."""

    def __init__(self, *, window_days: int = 7) -> None:
        self.document = Policy(
            "returns@2",
            MappingProxyType(
                {
                    "window_days": window_days,
                    "allowed_statuses": ("delivered",),
                    "excluded_categories": ("digital",),
                }
            ),
        )
        self.calls = []

    def get(self, document_id: str, *, tenant_id: str, as_of):
        self.calls.append((document_id, tenant_id, as_of))
        return self.document if tenant_id == "shop" else None


@pytest.fixture
def environment(tmp_path: Path):
    clock = Clock()
    store = Store(tmp_path / "refunds.db", clock=clock)
    customers = [
        {"id": "alice", "name": "林女士", "tenant_id": "shop"},
        {"id": "bob", "name": "陈先生", "tenant_id": "shop"},
    ]
    orders = [
        {
            "id": "order-" + customer["id"],
            "customer_id": customer["id"],
            "tenant_id": "shop",
            "product": "键盘",
            "category": "physical",
            "amount_minor": 12900,
            "currency": "CNY",
            "status": "delivered",
            "delivered_at": utc_text(NOW - timedelta(days=2)),
            "version": 1,
        }
        for customer in customers
    ]
    orders.append({**orders[0], "id": "order-alice-extra", "product": "支架", "amount_minor": 8900})
    store.initialize(customers, orders)
    created = store.create_session("alice")
    session = store.authenticate(created["token"])
    run = store.start_run(session, "申请退款", "request-1", "extractive")
    catalog = PolicyCatalog()
    refunds = RefundService(store, catalog)
    return store, clock, session, run, catalog, refunds


def assert_error(code: str, callback, status: int | None = None) -> DomainError:
    with pytest.raises(DomainError) as caught:
        callback()
    assert caught.value.code == code
    if status is not None:
        assert caught.value.status_code == status
    return caught.value


def prepare(environment, reason: str = "尺寸不合适") -> dict:
    _, _, session, run, _, refunds = environment
    return refunds.prepare(session, "order-alice", reason, run["id"])


def confirm(environment, action: dict, request_id: str = "confirm-1") -> dict:
    return environment[-1].confirm(
        environment[2], action["id"], action["confirmation_token"], request_id
    )


def second_run(environment, action: dict):
    store, clock, session, original_run, catalog, refunds = environment
    store.finish_run(
        session,
        original_run["id"],
        {
            "id": "turn-" + original_run["id"],
            "run_id": original_run["id"],
            "request_id": "request-1",
            "message": "申请退款",
            "runtime": "extractive",
            "answer": "请确认。",
            "status": "needs_confirmation",
            "action": action,
            "trace": [],
            "created_at": utc_text(store.now()),
        },
    )
    run = store.start_run(session, "申请退款", "request-2", "extractive")
    return store, clock, session, run, catalog, refunds


def change_order(store: Store, **fields) -> None:
    assignment = ", ".join(f"{field} = ?" for field in fields)
    with store.transaction() as connection:
        connection.execute(
            f"UPDATE orders SET {assignment}, version = version + 1 WHERE id = 'order-alice'",
            tuple(fields.values()),
        )


def counts(store: Store) -> dict:
    with store.transaction(write=False) as connection:
        return {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("actions", "receipts", "refund_requests", "confirmation_requests")
        }


def test_prepare_uses_policy_and_stored_money_without_changing_order(environment) -> None:
    store, clock, session, _, catalog, refunds = environment
    action = prepare(environment)
    assert action["state"] == "pending"
    assert action["amount_minor"] == 12900
    assert action["currency"] == "CNY"
    assert action["policy_source_id"] == "returns@2"
    assert catalog.calls == [("returns", "shop", clock.value.date())]
    assert store.get_order(session, "order-alice")["version"] == 1
    assert counts(store)["refund_requests"] == 0
    with store.transaction(write=False) as connection:
        row = dict(
            connection.execute("SELECT * FROM actions WHERE id = ?", (action["id"],)).fetchone()
        )
    assert action["confirmation_token"] not in repr(row)
    assert len(row["confirmation_token_hash"]) == 64
    assert "confirmation_token" not in refunds.current_action(session, action["id"])


def test_confirm_is_atomic_and_replays_one_immutable_receipt(environment) -> None:
    store, clock, session, _, catalog, refunds = environment
    action = prepare(environment)
    first = confirm(environment, action)
    assert first["state"] == "submitted" and first["replayed"] is False
    assert first["amount_minor"] == 12900
    assert confirm(environment, action) == {**first, "replayed": True}
    clock.value += timedelta(days=1)
    catalog.document = None
    assert confirm(environment, action, "confirm-after-expiry") == {**first, "replayed": True}
    order = store.get_order(session, "order-alice")
    assert order["status"] == "refund_requested"
    assert order["version"] == 2
    assert counts(store) == {
        "actions": 1,
        "receipts": 1,
        "refund_requests": 1,
        "confirmation_requests": 2,
    }
    assert refunds.current_action(session, action["id"])["receipt_id"] == first["receipt_id"]
    for table in ("receipts", "refund_requests"):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            with store.transaction() as connection:
                connection.execute(f"DELETE FROM {table}")


def test_receipt_and_pending_action_survive_reopening(environment) -> None:
    store, clock, session, _, catalog, _ = environment
    action = prepare(environment)
    reopened = Store(store.database_path, clock=clock)
    reopened.initialize([], [])
    refunds = RefundService(reopened, catalog)
    first = refunds.confirm(session, action["id"], action["confirmation_token"], "confirm-1")
    another = Store(store.database_path, clock=clock)
    another.initialize([], [])
    assert RefundService(another, catalog).confirm(
        session, action["id"], action["confirmation_token"], "confirm-1"
    ) == {**first, "replayed": True}


@pytest.mark.parametrize("other_customer", ["alice", "bob"])
def test_action_access_requires_the_preparing_session(environment, other_customer) -> None:
    store, _, _, _, _, refunds = environment
    action = prepare(environment)
    other = store.authenticate(store.create_session(other_customer)["token"])
    for callback in (
        lambda: refunds.current_action(other, action["id"]),
        lambda: refunds.cancel(other, action["id"]),
        lambda: refunds.confirm(other, action["id"], action["confirmation_token"], "other-confirm"),
    ):
        assert_error("ACTION_NOT_FOUND", callback, 404)
    assert counts(store)["receipts"] == 0


def test_prepare_checks_order_and_run_ownership(environment) -> None:
    store, _, session, run, _, refunds = environment
    assert_error(
        "ORDER_NOT_FOUND", lambda: refunds.prepare(session, "order-bob", "退款", run["id"]), 404
    )
    other = store.authenticate(store.create_session("alice")["token"])
    assert_error(
        "RUN_NOT_FOUND", lambda: refunds.prepare(other, "order-alice", "退款", run["id"]), 404
    )
    assert counts(store)["actions"] == 0


def test_prepare_rejects_inactive_or_expired_runs(environment) -> None:
    store, clock, session, run, _, refunds = environment
    clock.value += timedelta(seconds=300)
    assert_error("RUN_NOT_ACTIVE", lambda: prepare(environment), 409)
    next_run = store.start_run(session, "重新申请", "request-2", "extractive")
    store.fail_run(session, next_run["id"], DomainError("MODEL_ERROR", "模型失败。", 502), [])
    assert_error(
        "RUN_NOT_ACTIVE",
        lambda: refunds.prepare(session, "order-alice", "退款", next_run["id"]),
        409,
    )


@pytest.mark.parametrize(
    ("order_id", "reason"),
    [("order-alice", "尺寸不合适"), ("order-alice", "另一个原因"), ("order-alice-extra", "退货")],
)
def test_each_run_can_prepare_at_most_one_action(environment, order_id, reason) -> None:
    store, _, session, run, _, refunds = environment
    first = prepare(environment)
    assert_error(
        "ACTION_ALREADY_PREPARED",
        lambda: refunds.prepare(session, order_id, reason, run["id"]),
        409,
    )
    assert counts(store)["actions"] == 1
    assert refunds.current_action(session, first["id"])["state"] == "pending"
    assert store.get_order(session, "order-alice")["status"] == "delivered"
    assert store.get_order(session, "order-alice-extra")["status"] == "delivered"


def test_database_rejects_a_second_prepared_action_for_the_same_run(environment) -> None:
    store = environment[0]
    first = prepare(environment)
    with store.transaction(write=False) as connection:
        clone = dict(
            connection.execute("SELECT * FROM actions WHERE id = ?", (first["id"],)).fetchone()
        )
    clone["id"] = "second-action"
    columns = ", ".join(clone)
    placeholders = ", ".join("?" for _ in clone)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed: actions.run_id"):
        with store.transaction() as connection:
            connection.execute(
                f"INSERT INTO actions ({columns}) VALUES ({placeholders})", tuple(clone.values())
            )
    assert counts(store)["actions"] == 1


def test_concurrent_prepare_attempts_leave_one_action_per_run(environment) -> None:
    store, _, session, run, _, refunds = environment
    barrier = Barrier(2)

    def prepare_order(order_id):
        barrier.wait(timeout=5)
        try:
            return refunds.prepare(session, order_id, "退货", run["id"])
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(prepare_order, ["order-alice", "order-alice-extra"]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("ACTION_ALREADY_PREPARED") == 1
    assert counts(store)["actions"] == 1


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"category": "digital"}, "excluded_category"),
        ({"status": "shipped"}, "status"),
        ({"delivered_at": None}, "not_delivered"),
        ({"delivered_at": utc_text(NOW + timedelta(seconds=1))}, "future_delivery"),
        ({"delivered_at": utc_text(NOW - timedelta(days=7, microseconds=1))}, "window"),
    ],
)
def test_ineligible_order_rules_are_explicit(environment, changes, reason) -> None:
    change_order(environment[0], **changes)
    error = assert_error("ORDER_NOT_ELIGIBLE", lambda: prepare(environment), 409)
    assert error.details == {"reason": reason}
    assert counts(environment[0])["actions"] == 0


def test_window_is_exact_inclusive_and_comes_from_policy(environment) -> None:
    store, _, _, _, catalog, _ = environment
    change_order(store, delivered_at=utc_text(NOW - timedelta(days=7)))
    first = prepare(environment)
    assert first["state"] == "pending"
    catalog.document = Policy(
        "returns@3",
        MappingProxyType(
            {"window_days": 14, "allowed_statuses": ("delivered",), "excluded_categories": ()}
        ),
    )
    change_order(store, delivered_at=utc_text(NOW - timedelta(days=10)))
    assert prepare(second_run(environment, first))["policy_source_id"] == "returns@3"


def test_prepare_has_no_policy_fallback(environment) -> None:
    environment[4].document = None
    assert_error("POLICY_UNAVAILABLE", lambda: prepare(environment), 503)
    assert counts(environment[0])["actions"] == 0


@pytest.mark.parametrize(
    "rules",
    [
        {"allowed_statuses": ("delivered",), "excluded_categories": ()},
        {"window_days": True, "allowed_statuses": ("delivered",), "excluded_categories": ()},
        {"window_days": 7, "allowed_statuses": "delivered", "excluded_categories": ()},
        {"window_days": 7, "allowed_statuses": (), "excluded_categories": ()},
    ],
)
def test_invalid_policy_is_an_error(environment, rules) -> None:
    environment[4].document = Policy("returns@2", MappingProxyType(rules))
    assert_error("POLICY_INVALID", lambda: prepare(environment), 500)


def test_cancel_is_idempotent_and_cannot_undo_submission(environment) -> None:
    _, _, session, _, _, refunds = environment
    cancelled = prepare(environment)
    assert refunds.cancel(session, cancelled["id"]) == {"id": cancelled["id"], "state": "cancelled"}
    assert refunds.cancel(session, cancelled["id"])["state"] == "cancelled"
    assert_error("ACTION_CANCELLED", lambda: confirm(environment, cancelled), 409)
    submitted = prepare(second_run(environment, cancelled))
    receipt = confirm(environment, submitted, "confirm-2")
    assert_error("ACTION_ALREADY_SUBMITTED", lambda: refunds.cancel(session, submitted["id"]), 409)
    assert refunds.current_action(session, submitted["id"])["receipt_id"] == receipt["receipt_id"]


@pytest.mark.parametrize("operation", ["confirm", "cancel", "read"])
def test_expiration_persists_at_exact_ttl_boundary(environment, operation) -> None:
    store, clock, session, _, _, refunds = environment
    action = prepare(environment)
    clock.value += timedelta(seconds=600)
    if operation == "confirm":
        assert_error("ACTION_EXPIRED", lambda: confirm(environment, action), 410)
    elif operation == "cancel":
        assert_error("ACTION_EXPIRED", lambda: refunds.cancel(session, action["id"]), 410)
    else:
        assert refunds.current_action(session, action["id"])["state"] == "expired"
    with store.transaction(write=False) as connection:
        assert (
            connection.execute(
                "SELECT state FROM actions WHERE id = ?", (action["id"],)
            ).fetchone()[0]
            == "expired"
        )
    assert counts(store)["receipts"] == 0


def test_confirm_rechecks_window_elapsed_since_prepare(environment) -> None:
    store, clock, _, _, _, _ = environment
    change_order(store, delivered_at=utc_text(NOW - timedelta(days=7) + timedelta(seconds=30)))
    action = prepare(environment)
    clock.value += timedelta(seconds=31)
    error = assert_error("ORDER_NOT_ELIGIBLE", lambda: confirm(environment, action), 409)
    assert error.details["reason"] == "window"
    assert counts(store)["receipts"] == 0


def test_confirm_rechecks_current_policy_and_replays_original_failure(environment) -> None:
    store, _, _, _, catalog, _ = environment
    action = prepare(environment)
    original = catalog.document
    catalog.document = Policy("returns@3", original.rules)
    assert_error("POLICY_CHANGED", lambda: confirm(environment, action), 409)
    catalog.document = original
    assert_error("POLICY_CHANGED", lambda: confirm(environment, action), 409)
    assert counts(store)["receipts"] == 0
    assert confirm(environment, action, "explicit-new-confirm")["state"] == "submitted"


@pytest.mark.parametrize(
    "changes",
    [
        {"amount_minor": 15000},
        {"status": "cancelled"},
        {"category": "digital"},
        {"currency": "USD"},
    ],
)
def test_changed_order_version_invalidates_prepared_action(environment, changes) -> None:
    action = prepare(environment)
    change_order(environment[0], **changes)
    assert_error("ORDER_CHANGED", lambda: confirm(environment, action), 409)
    assert counts(environment[0])["receipts"] == 0


@pytest.mark.parametrize("invalid_token", ["incorrect", ""])
def test_confirmation_requires_token_and_payload_bound_request_id(
    environment, invalid_token
) -> None:
    store, _, session, _, _, refunds = environment
    action = prepare(environment)
    assert_error(
        "INVALID_CONFIRMATION_TOKEN",
        lambda: refunds.confirm(session, action["id"], invalid_token, "confirm-1"),
        403,
    )
    assert_error("REQUEST_CONFLICT", lambda: confirm(environment, action, "confirm-1"), 409)
    receipt = confirm(environment, action, "confirm-2")
    another = "another-action"
    assert_error(
        "REQUEST_CONFLICT",
        lambda: refunds.confirm(session, another, action["confirmation_token"], "confirm-2"),
        409,
    )
    assert refunds.current_action(session, action["id"])["receipt_id"] == receipt["receipt_id"]
    assert counts(store)["refund_requests"] == 1


def test_another_prepared_action_cannot_refund_the_same_order(environment) -> None:
    first = prepare(environment)
    second = prepare(second_run(environment, first), "另一个原因")
    receipt = confirm(environment, first)
    assert_error("ORDER_ALREADY_REQUESTED", lambda: confirm(environment, second, "confirm-2"), 409)
    assert confirm(environment, first, "confirm-3")["receipt_id"] == receipt["receipt_id"]
    assert counts(environment[0])["refund_requests"] == 1


def test_prepared_snapshot_and_terminal_state_cannot_be_rewritten(environment) -> None:
    store, _, session, _, _, refunds = environment
    action = prepare(environment)
    with pytest.raises(sqlite3.IntegrityError, match="snapshot is immutable"):
        with store.transaction() as connection:
            connection.execute("UPDATE actions SET amount_minor = 1 WHERE id = ?", (action["id"],))
    refunds.cancel(session, action["id"])
    with pytest.raises(sqlite3.IntegrityError, match="terminal action state"):
        with store.transaction() as connection:
            connection.execute("UPDATE actions SET state = 'pending' WHERE id = ?", (action["id"],))


@pytest.mark.parametrize("shared_request", [False, True])
def test_concurrent_confirmations_return_one_receipt_across_store_instances(
    environment, shared_request
) -> None:
    store, clock, session, _, catalog, _ = environment
    action = prepare(environment)
    clients = [RefundService(Store(store.database_path, clock=clock), catalog) for _ in range(6)]
    barrier = Barrier(len(clients))

    def submit(index: int) -> dict:
        barrier.wait(timeout=10)
        request_id = "shared-request" if shared_request else f"request-{index}"
        return clients[index].confirm(
            session, action["id"], action["confirmation_token"], request_id
        )

    with ThreadPoolExecutor(max_workers=len(clients)) as executor:
        receipts = list(executor.map(submit, range(len(clients))))
    assert len({receipt["receipt_id"] for receipt in receipts}) == 1
    assert sum(not receipt["replayed"] for receipt in receipts) == 1
    assert counts(store)["refund_requests"] == 1
    assert counts(store)["receipts"] == 1
    assert counts(store)["confirmation_requests"] == (1 if shared_request else len(clients))
    assert store.get_order(session, "order-alice")["version"] == 2


def test_different_sessions_cannot_submit_two_refunds_concurrently(environment) -> None:
    store, _, session, _, _, refunds = environment
    first = prepare(environment)
    other = store.authenticate(store.create_session("alice")["token"])
    run = store.start_run(other, "申请退款", "other-run", "extractive")
    second = refunds.prepare(other, "order-alice", "另一会话申请", run["id"])
    barrier = Barrier(2)

    def submit(pair):
        owner, action = pair
        barrier.wait(timeout=5)
        try:
            return refunds.confirm(owner, action["id"], action["confirmation_token"], "confirm")
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, [(session, first), (other, second)]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("ORDER_ALREADY_REQUESTED") == 1
    assert counts(store)["receipts"] == counts(store)["refund_requests"] == 1


def test_receipt_write_failure_rolls_back_every_business_change(environment) -> None:
    store, _, session, _, _, refunds = environment
    action = prepare(environment)
    with store.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_receipt BEFORE INSERT ON receipts BEGIN SELECT RAISE(ABORT, 'receipt write failed'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="receipt write failed"):
        confirm(environment, action)
    order = store.get_order(session, "order-alice")
    assert order["status"] == "delivered" and order["version"] == 1
    assert refunds.current_action(session, action["id"])["state"] == "pending"
    assert counts(store) == {
        "actions": 1,
        "receipts": 0,
        "refund_requests": 0,
        "confirmation_requests": 0,
    }
    with store.transaction() as connection:
        connection.execute("DROP TRIGGER fail_receipt")
    assert confirm(environment, action)["replayed"] is False


def test_historical_turn_preserves_confirmation_snapshot_while_current_action_changes(
    environment,
) -> None:
    store, _, session, run, _, refunds = environment
    action = prepare(environment)
    payload = {
        "id": "turn-1",
        "run_id": run["id"],
        "request_id": "request-1",
        "message": "申请退款",
        "runtime": "extractive",
        "answer": "请确认。",
        "status": "needs_confirmation",
        "action": action,
        "trace": [],
        "created_at": utc_text(store.now()),
    }
    store.finish_run(session, run["id"], payload)
    confirm(environment, action)
    historical = store.list_turns(session)[0]
    assert historical["action"]["state"] == "pending"
    assert historical["action"]["confirmation_token"] == action["confirmation_token"]
    assert refunds.current_action(session, action["id"])["state"] == "submitted"
    assert "confirmation_token" not in refunds.current_action(session, action["id"])


def test_packaged_knowledge_and_demo_orders_complete_a_real_transaction(tmp_path: Path) -> None:
    data_path = Path(__file__).resolve().parents[1] / "src" / "tracedesk" / "data"
    seed = json.loads((data_path / "orders.json").read_text(encoding="utf-8"))
    orders = []
    for record in seed["orders"]:
        order = dict(record)
        days = order.pop("delivered_days_ago")
        order["delivered_at"] = utc_text(NOW - timedelta(days=days)) if days is not None else None
        orders.append(order)
    store = Store(tmp_path / "packaged.db", clock=lambda: NOW)
    store.initialize(seed["customers"], orders)
    knowledge = KnowledgeBase.from_path(data_path / "knowledge.json")
    refunds = RefundService(store, knowledge)
    session = store.authenticate(store.create_session("customer-a")["token"])
    run = store.start_run(session, "退货", "packaged-request", "extractive")
    for order_id, code in (
        ("TD-1002", "ORDER_NOT_ELIGIBLE"),
        ("TD-1003", "ORDER_NOT_ELIGIBLE"),
        ("TD-1004", "ORDER_NOT_ELIGIBLE"),
        ("TD-2001", "ORDER_NOT_FOUND"),
    ):
        assert_error(
            code, lambda target=order_id: refunds.prepare(session, target, "退货", run["id"])
        )
    action = refunds.prepare(session, "TD-1001", "退货", run["id"])
    assert action["policy_source_id"] == "returns@2"
    receipt = refunds.confirm(
        session, action["id"], action["confirmation_token"], "packaged-confirm"
    )
    assert receipt["state"] == "submitted" and receipt["amount_minor"] == 12900
    assert store.get_order(session, "TD-1001")["version"] == 2
