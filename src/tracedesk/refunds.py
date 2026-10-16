"""Prepare and confirm a policy-bound refund request in one SQLite transaction."""

from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from .errors import DomainError
from .store import Store, error_payload, json_text, parse_utc, restore_error, secret_hash, utc_text

if TYPE_CHECKING:
    from .retrieval import Document, KnowledgeBase


class RefundService:
    def __init__(
        self, store: Store, knowledge: KnowledgeBase, *, action_ttl_seconds: int = 600
    ) -> None:
        if type(action_ttl_seconds) is not int or action_ttl_seconds <= 0:
            raise ValueError("action_ttl_seconds must be a positive integer.")
        self.store = store
        self.knowledge = knowledge
        self.action_ttl_seconds = action_ttl_seconds

    def _policy(self, session: dict, now: datetime) -> Document:
        document = self.knowledge.get("returns", tenant_id=session["tenant_id"], as_of=now.date())
        if document is None:
            raise DomainError(
                "POLICY_UNAVAILABLE", "当前没有生效的退货政策，无法准备或确认申请。", 503
            )
        rules = document.rules
        if (
            not isinstance(rules, Mapping)
            or type(rules.get("window_days")) is not int
            or rules["window_days"] < 0
        ):
            raise DomainError("POLICY_INVALID", "退货政策缺少有效的时间窗口。", 500)
        for field in ("allowed_statuses", "excluded_categories"):
            value = rules.get(field)
            if (
                not isinstance(value, Sequence)
                or isinstance(value, (str, bytes))
                or not all(isinstance(item, str) and item for item in value)
            ):
                raise DomainError("POLICY_INVALID", "退货政策规则结构无效。", 500)
        if (
            not rules["allowed_statuses"]
            or not isinstance(document.source_id, str)
            or not document.source_id
        ):
            raise DomainError("POLICY_INVALID", "退货政策缺少可用的状态规则或版本标识。", 500)
        return document

    @staticmethod
    def _check_eligibility(order: dict, policy: Document, now: datetime) -> None:
        rules = policy.rules
        if order["status"] not in rules["allowed_statuses"]:
            raise DomainError(
                "ORDER_NOT_ELIGIBLE", "当前订单状态不支持退货申请。", 409, {"reason": "status"}
            )
        if order["category"] in rules["excluded_categories"]:
            raise DomainError(
                "ORDER_NOT_ELIGIBLE",
                "该商品分类不在当前退货政策的适用范围内。",
                409,
                {"reason": "excluded_category"},
            )
        if order["delivered_at"] is None:
            raise DomainError(
                "ORDER_NOT_ELIGIBLE", "订单尚无有效的签收时间。", 409, {"reason": "not_delivered"}
            )
        elapsed = (now - parse_utc(order["delivered_at"])).total_seconds()
        if elapsed < 0:
            raise DomainError(
                "ORDER_NOT_ELIGIBLE",
                "订单签收时间在未来，请先核实订单数据。",
                409,
                {"reason": "future_delivery"},
            )
        if elapsed > timedelta(days=rules["window_days"]).total_seconds():
            raise DomainError(
                "ORDER_NOT_ELIGIBLE",
                "订单已超过当前政策规定的退货期限。",
                409,
                {"reason": "window"},
            )

    def prepare(self, session: dict, order_id: str, reason: str, run_id: str) -> dict:
        if not isinstance(reason, str) or not reason.strip():
            raise DomainError("INVALID_REASON", "请提供退款申请原因。", 422)
        with self.store.transaction() as connection:
            self.store.require_session(connection, session)
            now = self.store.now()
            run = self.store.require_run(connection, session, run_id)
            if run["status"] != "running" or parse_utc(run["lease_expires_at"]) <= now:
                raise DomainError("RUN_NOT_ACTIVE", "只能在有效的运行中准备申请。", 409)
            if (
                connection.execute(
                    "SELECT 1 FROM actions WHERE run_id = ? AND session_id = ?",
                    (run_id, session["id"]),
                ).fetchone()
                is not None
            ):
                raise DomainError(
                    "ACTION_ALREADY_PREPARED", "本次运行已经准备过申请，请在新的运行中操作。", 409
                )
            order = self.store.order_in_transaction(connection, session, order_id)
            policy = self._policy(session, now)
            self._check_eligibility(order, policy, now)
            token = secrets.token_urlsafe(32)
            action = {
                "id": str(uuid4()),
                "kind": "refund_request",
                "state": "pending",
                "order_id": order_id,
                "amount_minor": order["amount_minor"],
                "currency": order["currency"],
                "reason": reason.strip(),
                "created_at": utc_text(now),
                "expires_at": utc_text(now + timedelta(seconds=self.action_ttl_seconds)),
                "policy_source_id": policy.source_id,
            }
            connection.execute(
                """INSERT INTO actions(id, session_id, run_id, customer_id, tenant_id, order_id,
                   kind, state, reason, amount_minor, currency, order_version, policy_source_id,
                   confirmation_token_hash, created_at, expires_at, state_changed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    action["id"],
                    session["id"],
                    run_id,
                    session["customer_id"],
                    session["tenant_id"],
                    order_id,
                    action["kind"],
                    action["state"],
                    action["reason"],
                    action["amount_minor"],
                    action["currency"],
                    order["version"],
                    policy.source_id,
                    secret_hash(token),
                    action["created_at"],
                    action["expires_at"],
                    action["created_at"],
                ),
            )
        return {**action, "confirmation_token": token}

    @staticmethod
    def _action(connection: sqlite3.Connection, session: dict, action_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM actions WHERE id = ? AND session_id = ? AND customer_id = ? AND tenant_id = ?",
            (action_id, session["id"], session["customer_id"], session["tenant_id"]),
        ).fetchone()
        if row is None:
            raise DomainError("ACTION_NOT_FOUND", "未找到当前会话的申请。", 404)
        return row

    @staticmethod
    def _public_action(row: sqlite3.Row) -> dict:
        return {
            key: row[key]
            for key in (
                "id",
                "kind",
                "state",
                "order_id",
                "amount_minor",
                "currency",
                "reason",
                "created_at",
                "expires_at",
                "policy_source_id",
            )
        }

    @staticmethod
    def _receipt(
        connection: sqlite3.Connection,
        session_id: str,
        *,
        action_id: str | None = None,
        receipt_id: str | None = None,
        replayed: bool,
    ) -> dict:
        row = connection.execute(
            "SELECT * FROM receipts WHERE session_id = ? AND (action_id = ? OR receipt_id = ?)",
            (session_id, action_id, receipt_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("Submitted action has no matching immutable receipt.")
        return {
            "id": row["action_id"],
            "state": "submitted",
            "order_id": row["order_id"],
            "amount_minor": row["amount_minor"],
            "currency": row["currency"],
            "receipt_id": row["receipt_id"],
            "submitted_at": row["submitted_at"],
            "replayed": replayed,
        }

    @staticmethod
    def _expire(connection: sqlite3.Connection, action: sqlite3.Row, now: datetime) -> bool:
        if action["state"] != "pending" or parse_utc(action["expires_at"]) > now:
            return False
        connection.execute(
            "UPDATE actions SET state = 'expired', state_changed_at = ? WHERE id = ? AND state = 'pending'",
            (utc_text(now), action["id"]),
        )
        return True

    def current_action(self, session: dict, action_id: str) -> dict:
        with self.store.transaction() as connection:
            self.store.require_session(connection, session)
            action = self._action(connection, session, action_id)
            if self._expire(connection, action, self.store.now()):
                action = self._action(connection, session, action_id)
            result = self._public_action(action)
            if action["state"] == "submitted":
                result.update(
                    self._receipt(connection, session["id"], action_id=action_id, replayed=True)
                )
        return result

    def cancel(self, session: dict, action_id: str) -> dict:
        failure: DomainError | None = None
        with self.store.transaction() as connection:
            self.store.require_session(connection, session)
            action = self._action(connection, session, action_id)
            now = self.store.now()
            expired = self._expire(connection, action, now)
            if expired or action["state"] == "expired":
                failure = DomainError("ACTION_EXPIRED", "申请已过期，请重新准备。", 410)
            elif action["state"] == "submitted":
                failure = DomainError("ACTION_ALREADY_SUBMITTED", "申请已经提交，不能取消。", 409)
            elif action["state"] == "pending":
                connection.execute(
                    "UPDATE actions SET state = 'cancelled', state_changed_at = ? WHERE id = ? AND state = 'pending'",
                    (utc_text(now), action_id),
                )
        if failure is not None:
            raise failure
        return {"id": action_id, "state": "cancelled"}

    def confirm(
        self, session: dict, action_id: str, confirmation_token: str, request_id: str
    ) -> dict:
        if not isinstance(request_id, str) or not request_id.strip():
            raise DomainError("INVALID_REQUEST_ID", "确认申请必须提供请求 ID。", 422)
        if not isinstance(confirmation_token, str):
            raise DomainError("INVALID_CONFIRMATION_TOKEN", "确认凭据无效。", 403)
        fingerprint = secret_hash(json_text([action_id, confirmation_token]))
        failure: DomainError | None = None
        result: dict = {}
        with self.store.transaction() as connection:
            self.store.require_session(connection, session)
            previous = connection.execute(
                "SELECT * FROM confirmation_requests WHERE session_id = ? AND request_id = ?",
                (session["id"], request_id),
            ).fetchone()
            if previous is not None:
                result, failure = self._replay_confirmation(
                    connection, session, previous, fingerprint
                )
            else:
                now = self.store.now()
                try:
                    result = self._confirm_action(
                        connection, session, action_id, confirmation_token, now
                    )
                except DomainError as error:
                    # Expected validation failures are replayable; SQL/programming failures roll back.
                    failure = error
                connection.execute(
                    """INSERT INTO confirmation_requests(session_id, request_id, action_id, payload_hash,
                       receipt_id, error_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        session["id"],
                        request_id,
                        action_id,
                        fingerprint,
                        result.get("receipt_id"),
                        json_text(error_payload(failure)) if failure is not None else None,
                        utc_text(now),
                    ),
                )
        if failure is not None:
            raise failure
        return result

    def _replay_confirmation(
        self, connection: sqlite3.Connection, session: dict, previous: sqlite3.Row, fingerprint: str
    ) -> tuple[dict, DomainError | None]:
        if not hmac.compare_digest(previous["payload_hash"], fingerprint):
            return {}, DomainError("REQUEST_CONFLICT", "该请求 ID 已用于不同的确认内容。", 409)
        if previous["error_json"] is not None:
            return {}, restore_error(json.loads(previous["error_json"]))
        return self._receipt(
            connection, session["id"], receipt_id=previous["receipt_id"], replayed=True
        ), None

    def _confirm_action(
        self,
        connection: sqlite3.Connection,
        session: dict,
        action_id: str,
        token: str,
        now: datetime,
    ) -> dict:
        action = self._action(connection, session, action_id)
        if not hmac.compare_digest(action["confirmation_token_hash"], secret_hash(token)):
            raise DomainError("INVALID_CONFIRMATION_TOKEN", "确认凭据无效。", 403)
        if action["state"] == "submitted":
            return self._receipt(connection, session["id"], action_id=action_id, replayed=True)
        if action["state"] == "cancelled":
            raise DomainError("ACTION_CANCELLED", "申请已取消，请重新准备。", 409)
        if self._expire(connection, action, now) or action["state"] == "expired":
            raise DomainError("ACTION_EXPIRED", "申请已过期，请重新准备。", 410)
        order = self.store.order_in_transaction(connection, session, action["order_id"])
        if (
            connection.execute(
                "SELECT 1 FROM refund_requests WHERE order_id = ?", (order["id"],)
            ).fetchone()
            is not None
        ):
            raise DomainError("ORDER_ALREADY_REQUESTED", "该订单已经提交过退款申请。", 409)
        if (
            order["version"] != action["order_version"]
            or order["amount_minor"] != action["amount_minor"]
            or order["currency"] != action["currency"]
        ):
            raise DomainError("ORDER_CHANGED", "订单内容已变化，请重新查询并准备申请。", 409)
        policy = self._policy(session, now)
        if policy.source_id != action["policy_source_id"]:
            raise DomainError("POLICY_CHANGED", "生效政策已更新，请重新准备申请。", 409)
        self._check_eligibility(order, policy, now)
        return self._submit(connection, session, action, order, now)

    def _submit(
        self,
        connection: sqlite3.Connection,
        session: dict,
        action: sqlite3.Row,
        order: dict,
        now: datetime,
    ) -> dict:
        timestamp = utc_text(now)
        receipt_id = str(uuid4())
        connection.execute(
            "INSERT INTO refund_requests(order_id, action_id, submitted_at) VALUES (?, ?, ?)",
            (order["id"], action["id"], timestamp),
        )
        updated = connection.execute(
            """UPDATE orders SET status = 'refund_requested', version = version + 1
               WHERE id = ? AND customer_id = ? AND tenant_id = ? AND version = ? AND status = ?""",
            (
                order["id"],
                session["customer_id"],
                session["tenant_id"],
                action["order_version"],
                order["status"],
            ),
        ).rowcount
        if updated != 1:
            raise RuntimeError(
                "Order version invariant failed during an exclusive refund transaction."
            )
        connection.execute(
            """INSERT INTO receipts(receipt_id, action_id, session_id, order_id, amount_minor, currency, submitted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                receipt_id,
                action["id"],
                session["id"],
                order["id"],
                order["amount_minor"],
                order["currency"],
                timestamp,
            ),
        )
        updated = connection.execute(
            "UPDATE actions SET state = 'submitted', state_changed_at = ? WHERE id = ? AND session_id = ? AND state = 'pending'",
            (timestamp, action["id"], session["id"]),
        ).rowcount
        if updated != 1:
            raise RuntimeError(
                "Action state invariant failed during an exclusive refund transaction."
            )
        return self._receipt(connection, session["id"], receipt_id=receipt_id, replayed=False)
