"""SQLite is the single source of truth for sessions, runs and refund state."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .errors import DomainError

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5_000

SCHEMA = (
    """CREATE TABLE metadata (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)""",
    """CREATE TABLE customers (
        id TEXT PRIMARY KEY NOT NULL, tenant_id TEXT NOT NULL, name TEXT NOT NULL,
        UNIQUE (id, tenant_id)
    )""",
    """CREATE TABLE orders (
        id TEXT PRIMARY KEY NOT NULL,
        customer_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
        product TEXT NOT NULL, category TEXT NOT NULL,
        amount_minor INTEGER NOT NULL
            CHECK (typeof(amount_minor) = 'integer' AND amount_minor > 0),
        currency TEXT NOT NULL CHECK (length(currency) = 3),
        status TEXT NOT NULL CHECK (length(status) > 0),
        delivered_at TEXT,
        version INTEGER NOT NULL CHECK (typeof(version) = 'integer' AND version > 0),
        UNIQUE (id, customer_id, tenant_id),
        FOREIGN KEY (customer_id, tenant_id) REFERENCES customers(id, tenant_id)
    )""",
    """CREATE TABLE sessions (
        id TEXT PRIMARY KEY NOT NULL, customer_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE CHECK (length(token_hash) = 64),
        created_at TEXT NOT NULL, last_order_id TEXT,
        UNIQUE (id, customer_id, tenant_id),
        FOREIGN KEY (customer_id, tenant_id) REFERENCES customers(id, tenant_id),
        FOREIGN KEY (last_order_id, customer_id, tenant_id)
            REFERENCES orders(id, customer_id, tenant_id)
    )""",
    """CREATE TABLE runs (
        id TEXT PRIMARY KEY NOT NULL, session_id TEXT NOT NULL REFERENCES sessions(id),
        request_id TEXT NOT NULL, message TEXT NOT NULL, runtime TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('running','completed','failed','interrupted')),
        created_at TEXT NOT NULL, lease_expires_at TEXT NOT NULL,
        finished_at TEXT, error_json TEXT, trace_json TEXT NOT NULL DEFAULT '[]',
        UNIQUE (session_id, request_id), UNIQUE (id, session_id),
        CHECK (
            (status = 'running' AND finished_at IS NULL AND error_json IS NULL) OR
            (status = 'completed' AND finished_at IS NOT NULL AND error_json IS NULL) OR
            (status IN ('failed','interrupted') AND finished_at IS NOT NULL AND error_json IS NOT NULL)
        )
    )""",
    """CREATE UNIQUE INDEX one_running_run_per_session
        ON runs(session_id) WHERE status = 'running'""",
    """CREATE TABLE turns (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
        run_id TEXT NOT NULL UNIQUE, session_id TEXT NOT NULL,
        payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
        FOREIGN KEY (run_id, session_id) REFERENCES runs(id, session_id)
    )""",
    """CREATE INDEX turns_by_session ON turns(session_id, sequence)""",
    """CREATE TABLE actions (
        id TEXT PRIMARY KEY NOT NULL, session_id TEXT NOT NULL, run_id TEXT NOT NULL,
        customer_id TEXT NOT NULL, tenant_id TEXT NOT NULL, order_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind = 'refund_request'),
        state TEXT NOT NULL CHECK (state IN ('pending','submitted','cancelled','expired')),
        reason TEXT NOT NULL CHECK (length(reason) > 0),
        amount_minor INTEGER NOT NULL CHECK (typeof(amount_minor) = 'integer' AND amount_minor > 0),
        currency TEXT NOT NULL, order_version INTEGER NOT NULL CHECK (order_version > 0),
        policy_source_id TEXT NOT NULL,
        confirmation_token_hash TEXT NOT NULL CHECK (length(confirmation_token_hash) = 64),
        created_at TEXT NOT NULL, expires_at TEXT NOT NULL, state_changed_at TEXT NOT NULL,
        UNIQUE (id, session_id, order_id), UNIQUE (id, order_id), UNIQUE (run_id),
        FOREIGN KEY (session_id, customer_id, tenant_id)
            REFERENCES sessions(id, customer_id, tenant_id),
        FOREIGN KEY (order_id, customer_id, tenant_id)
            REFERENCES orders(id, customer_id, tenant_id),
        FOREIGN KEY (run_id, session_id) REFERENCES runs(id, session_id)
    )""",
    """CREATE INDEX actions_by_session ON actions(session_id)""",
    """CREATE TABLE refund_requests (
        order_id TEXT PRIMARY KEY NOT NULL REFERENCES orders(id),
        action_id TEXT NOT NULL UNIQUE,
        submitted_at TEXT NOT NULL,
        FOREIGN KEY (action_id, order_id) REFERENCES actions(id, order_id)
    )""",
    """CREATE TABLE receipts (
        receipt_id TEXT PRIMARY KEY NOT NULL,
        action_id TEXT NOT NULL UNIQUE REFERENCES refund_requests(action_id),
        session_id TEXT NOT NULL, order_id TEXT NOT NULL UNIQUE,
        amount_minor INTEGER NOT NULL CHECK (typeof(amount_minor) = 'integer' AND amount_minor > 0),
        currency TEXT NOT NULL, submitted_at TEXT NOT NULL,
        FOREIGN KEY (action_id, session_id, order_id)
            REFERENCES actions(id, session_id, order_id)
    )""",
    """CREATE TABLE confirmation_requests (
        session_id TEXT NOT NULL REFERENCES sessions(id), request_id TEXT NOT NULL,
        action_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
        receipt_id TEXT REFERENCES receipts(receipt_id), error_json TEXT,
        created_at TEXT NOT NULL, PRIMARY KEY (session_id, request_id),
        CHECK ((receipt_id IS NOT NULL AND error_json IS NULL) OR
               (receipt_id IS NULL AND error_json IS NOT NULL))
    )""",
    """CREATE TRIGGER order_identity_is_immutable BEFORE UPDATE ON orders
        WHEN NEW.id != OLD.id OR NEW.customer_id != OLD.customer_id OR NEW.tenant_id != OLD.tenant_id
        BEGIN SELECT RAISE(ABORT, 'order identity is immutable'); END""",
    """CREATE TRIGGER order_updates_advance_version BEFORE UPDATE ON orders
        WHEN NEW.version != OLD.version + 1
        BEGIN SELECT RAISE(ABORT, 'order updates must advance version exactly once'); END""",
    """CREATE TRIGGER action_snapshot_is_immutable BEFORE UPDATE ON actions
        WHEN NEW.id != OLD.id OR NEW.session_id != OLD.session_id OR NEW.run_id != OLD.run_id
        OR NEW.customer_id != OLD.customer_id OR NEW.tenant_id != OLD.tenant_id
        OR NEW.order_id != OLD.order_id OR NEW.kind != OLD.kind OR NEW.reason != OLD.reason
        OR NEW.amount_minor != OLD.amount_minor OR NEW.currency != OLD.currency
        OR NEW.order_version != OLD.order_version OR NEW.policy_source_id != OLD.policy_source_id
        OR NEW.confirmation_token_hash != OLD.confirmation_token_hash
        OR NEW.created_at != OLD.created_at OR NEW.expires_at != OLD.expires_at
        BEGIN SELECT RAISE(ABORT, 'prepared action snapshot is immutable'); END""",
    """CREATE TRIGGER action_state_transition BEFORE UPDATE OF state ON actions
        WHEN NEW.state != OLD.state AND OLD.state != 'pending'
        BEGIN SELECT RAISE(ABORT, 'terminal action state is immutable'); END""",
    *(
        f"CREATE TRIGGER {table}_immutable_{operation.lower()} BEFORE {operation} ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{table} are immutable'); END"
        for table in ("turns", "receipts", "refund_requests")
        for operation in ("UPDATE", "DELETE")
    ),
)


def utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware datetime is required.")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Stored timestamps must include a timezone.")
    return parsed.astimezone(timezone.utc)


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def error_payload(error: DomainError) -> dict[str, Any]:
    return {
        "code": error.code,
        "message": error.message,
        "status_code": error.status_code,
        "details": error.details,
    }


def restore_error(payload: dict[str, Any]) -> DomainError:
    return DomainError(
        payload["code"], payload["message"], payload["status_code"], payload["details"]
    )


class Store:
    def __init__(
        self,
        database_path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        run_lease_seconds: int = 300,
    ) -> None:
        if type(run_lease_seconds) is not int or run_lease_seconds <= 0:
            raise ValueError("run_lease_seconds must be a positive integer.")
        if str(database_path) == ":memory:":
            raise ValueError(
                "Use a file-backed SQLite database; connections are transaction scoped."
            )
        self.database_path = Path(database_path)
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self.run_lease_seconds = run_lease_seconds

    def now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("The store clock must return a timezone-aware datetime.")
        return value.astimezone(timezone.utc)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path, timeout=BUSY_TIMEOUT_MS / 1_000, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        return connection

    @contextmanager
    def transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        finally:
            # Closing rolls back an uncommitted transaction, including unexpected failures.
            connection.close()

    def initialize(self, customers: list[dict], orders: list[dict]) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if journal_mode != "wal":
                raise DomainError("DATABASE_CONFIGURATION", "数据库无法启用 WAL 日志模式。", 500)
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise DomainError(
                    "SCHEMA_INCOMPATIBLE", "数据库版本不兼容，必须先执行明确的数据迁移。", 500
                )
            if version == 0:
                existing = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                ).fetchone()
                if existing is not None:
                    raise DomainError("SCHEMA_INCOMPATIBLE", "未识别的已有数据库，拒绝覆盖。", 500)
                for statement in SCHEMA:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            seeded = connection.execute(
                "SELECT value FROM metadata WHERE key = 'seeded'"
            ).fetchone()
            if seeded is None:
                self._seed(connection, customers, orders)
                connection.execute("INSERT INTO metadata(key, value) VALUES ('seeded', '1')")
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _seed(connection: sqlite3.Connection, customers: list[dict], orders: list[dict]) -> None:
        for customer in customers:
            for key in ("id", "tenant_id", "name"):
                if not isinstance(customer[key], str) or not customer[key].strip():
                    raise ValueError(f"Customer {key} must be a nonempty string.")
            connection.execute(
                "INSERT INTO customers(id, tenant_id, name) VALUES (?, ?, ?)",
                (customer["id"], customer["tenant_id"], customer["name"]),
            )
        for order in orders:
            for key in (
                "id",
                "customer_id",
                "tenant_id",
                "product",
                "category",
                "currency",
                "status",
            ):
                if not isinstance(order[key], str) or not order[key].strip():
                    raise ValueError(f"Order {key} must be a nonempty string.")
            for key in ("amount_minor", "version"):
                if type(order[key]) is not int or order[key] <= 0:
                    raise ValueError(f"Order {key} must be a positive integer.")
            if (
                len(order["currency"]) != 3
                or not order["currency"].isalpha()
                or not order["currency"].isupper()
            ):
                raise ValueError("Order currency must be a three-letter uppercase code.")
            delivered_at = order["delivered_at"]
            if delivered_at is not None:
                delivered_at = utc_text(parse_utc(delivered_at))
            connection.execute(
                """INSERT INTO orders(id, customer_id, tenant_id, product, category, amount_minor,
                   currency, status, delivered_at, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    order["id"],
                    order["customer_id"],
                    order["tenant_id"],
                    order["product"],
                    order["category"],
                    order["amount_minor"],
                    order["currency"],
                    order["status"],
                    delivered_at,
                    order["version"],
                ),
            )

    def customer_list(self) -> list[dict]:
        with self.transaction(write=False) as connection:
            rows = connection.execute(
                """SELECT c.id, c.name, o.id AS order_id FROM customers c
                   LEFT JOIN orders o ON o.customer_id = c.id AND o.tenant_id = c.tenant_id
                   ORDER BY c.id, o.id"""
            ).fetchall()
        customers: dict[str, dict] = {}
        for row in rows:
            customer = customers.setdefault(
                row["id"], {"id": row["id"], "name": row["name"], "order_ids": []}
            )
            if row["order_id"] is not None:
                customer["order_ids"].append(row["order_id"])
        return list(customers.values())

    def create_session(self, customer_id: str) -> dict:
        with self.transaction() as connection:
            customer = connection.execute(
                "SELECT * FROM customers WHERE id = ?", (customer_id,)
            ).fetchone()
            if customer is None:
                raise DomainError("CUSTOMER_NOT_FOUND", "未找到客户。", 404)
            token = secrets.token_urlsafe(32)
            session_id = str(uuid4())
            created_at = utc_text(self.now())
            connection.execute(
                """INSERT INTO sessions(id, customer_id, tenant_id, token_hash, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, customer_id, customer["tenant_id"], secret_hash(token), created_at),
            )
        return {
            "id": session_id,
            "token": token,
            "customer": {"id": customer_id, "name": customer["name"]},
            "created_at": created_at,
        }

    def authenticate(self, token: str, session_id: str | None = None) -> dict:
        if not isinstance(token, str) or not token:
            raise DomainError("UNAUTHORIZED", "会话凭据无效。", 401)
        with self.transaction(write=False) as connection:
            row = connection.execute(
                """SELECT s.id, s.customer_id, s.tenant_id, c.name AS customer_name,
                   s.created_at, s.last_order_id FROM sessions s
                   JOIN customers c ON c.id = s.customer_id AND c.tenant_id = s.tenant_id
                   WHERE s.token_hash = ?""",
                (secret_hash(token),),
            ).fetchone()
        if row is None or (session_id is not None and row["id"] != session_id):
            raise DomainError("UNAUTHORIZED", "会话凭据无效。", 401)
        return dict(row)

    @staticmethod
    def require_session(connection: sqlite3.Connection, session: dict) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM sessions WHERE id = ? AND customer_id = ? AND tenant_id = ?",
            (session["id"], session["customer_id"], session["tenant_id"]),
        ).fetchone()
        if row is None:
            raise DomainError("UNAUTHORIZED", "会话凭据无效。", 401)
        return row

    @staticmethod
    def order_in_transaction(connection: sqlite3.Connection, session: dict, order_id: str) -> dict:
        row = connection.execute(
            "SELECT * FROM orders WHERE id = ? AND customer_id = ? AND tenant_id = ?",
            (order_id, session["customer_id"], session["tenant_id"]),
        ).fetchone()
        if row is None:
            raise DomainError("ORDER_NOT_FOUND", "未找到当前客户的订单。", 404)
        return dict(row)

    def get_order(self, session: dict, order_id: str) -> dict:
        with self.transaction() as connection:
            self.require_session(connection, session)
            order = self.order_in_transaction(connection, session, order_id)
            connection.execute(
                "UPDATE sessions SET last_order_id = ? WHERE id = ?", (order_id, session["id"])
            )
        return order

    def session_context(self, session: dict) -> str | None:
        with self.transaction(write=False) as connection:
            row = self.require_session(connection, session)
        return row["last_order_id"]

    def list_turns(self, session: dict) -> list[dict]:
        with self.transaction(write=False) as connection:
            self.require_session(connection, session)
            rows = connection.execute(
                "SELECT payload_json FROM turns WHERE session_id = ? ORDER BY sequence",
                (session["id"],),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def interrupt_expired_runs(
        self, connection: sqlite3.Connection, session_id: str, now: datetime
    ) -> None:
        timestamp = utc_text(now)
        rows = connection.execute(
            "SELECT id, trace_json FROM runs WHERE session_id = ? AND status = 'running' AND lease_expires_at <= ?",
            (session_id, timestamp),
        ).fetchall()
        for row in rows:
            error = DomainError(
                "INTERRUPTED", "运行租约已过期，前次执行被中断。请使用新的请求 ID 继续。", 409
            )
            trace = json.loads(row["trace_json"])
            trace.append(
                {
                    "seq": len(trace) + 1,
                    "stage": "runtime",
                    "label": "运行中断",
                    "status": "error",
                    "detail": error.message,
                    "duration_ms": 0.0,
                }
            )
            connection.execute(
                """UPDATE runs SET status = 'interrupted', finished_at = ?, error_json = ?, trace_json = ?
                   WHERE id = ? AND status = 'running'""",
                (timestamp, json_text(error_payload(error)), json_text(trace), row["id"]),
            )

    def start_run(self, session: dict, message: str, request_id: str, runtime: str) -> dict:
        for name, value in (("message", message), ("request_id", request_id), ("runtime", runtime)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a nonempty string.")
        failure: DomainError | None = None
        result: dict = {}
        with self.transaction() as connection:
            self.require_session(connection, session)
            now = self.now()
            self.interrupt_expired_runs(connection, session["id"], now)
            existing = connection.execute(
                "SELECT * FROM runs WHERE session_id = ? AND request_id = ?",
                (session["id"], request_id),
            ).fetchone()
            if existing is not None:
                result, failure = self._replay_run(connection, existing, message)
            elif (
                connection.execute(
                    "SELECT 1 FROM runs WHERE session_id = ? AND status = 'running'",
                    (session["id"],),
                ).fetchone()
                is not None
            ):
                failure = DomainError(
                    "SESSION_BUSY", "当前会话仍有运行中的请求，请等待其结束。", 409
                )
            else:
                run_id = str(uuid4())
                connection.execute(
                    """INSERT INTO runs(id, session_id, request_id, message, runtime, status, created_at, lease_expires_at)
                       VALUES (?, ?, ?, ?, ?, 'running', ?, ?)""",
                    (
                        run_id,
                        session["id"],
                        request_id,
                        message,
                        runtime,
                        utc_text(now),
                        utc_text(now + timedelta(seconds=self.run_lease_seconds)),
                    ),
                )
                result = {"id": run_id, "replayed": False}
        if failure is not None:
            raise failure
        return result

    @staticmethod
    def _replay_run(
        connection: sqlite3.Connection, run: sqlite3.Row, message: str
    ) -> tuple[dict, DomainError | None]:
        if run["message"] != message:
            return {}, DomainError("REQUEST_CONFLICT", "该请求 ID 已用于不同的消息。", 409)
        if run["status"] == "running":
            return {}, DomainError("RUN_IN_PROGRESS", "该请求仍在执行中。", 409)
        result = {"id": run["id"], "replayed": True}
        if run["status"] != "completed":
            result["error"] = json.loads(run["error_json"])
            return result, None
        row = connection.execute(
            "SELECT payload_json FROM turns WHERE run_id = ?", (run["id"],)
        ).fetchone()
        if row is None:
            raise RuntimeError("Completed run has no immutable turn.")
        result["turn"] = json.loads(row["payload_json"])
        return result, None

    @staticmethod
    def require_run(connection: sqlite3.Connection, session: dict, run_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM runs WHERE id = ? AND session_id = ?", (run_id, session["id"])
        ).fetchone()
        if row is None:
            raise DomainError("RUN_NOT_FOUND", "未找到当前会话的运行。", 404)
        return row

    def finish_run(self, session: dict, run_id: str, turn: dict) -> None:
        payload = json_text(turn)
        failure: DomainError | None = None
        with self.transaction() as connection:
            self.require_session(connection, session)
            now = self.now()
            self.interrupt_expired_runs(connection, session["id"], now)
            run = self.require_run(connection, session, run_id)
            if run["status"] != "running":
                failure = DomainError("RUN_NOT_ACTIVE", "该运行已结束，不能再保存回答。", 409)
            else:
                self._validate_turn(run, turn)
                connection.execute(
                    "INSERT INTO turns(id, run_id, session_id, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                    (turn["id"], run_id, session["id"], payload, turn["created_at"]),
                )
                connection.execute(
                    """UPDATE runs SET status = 'completed', finished_at = ?, trace_json = ?
                       WHERE id = ? AND session_id = ? AND status = 'running'""",
                    (utc_text(now), json_text(turn["trace"]), run_id, session["id"]),
                )
        if failure is not None:
            raise failure

    @staticmethod
    def _validate_turn(run: sqlite3.Row, turn: dict) -> None:
        for field, expected in (
            ("run_id", run["id"]),
            ("request_id", run["request_id"]),
            ("message", run["message"]),
            ("runtime", run["runtime"]),
        ):
            if turn.get(field) != expected:
                raise ValueError(f"Turn {field} does not match its run.")
        if not isinstance(turn.get("id"), str) or not turn["id"]:
            raise ValueError("Turn id must be a nonempty string.")
        if not isinstance(turn.get("trace"), list):
            raise ValueError("Turn trace must be a list.")
        parse_utc(turn["created_at"])

    def fail_run(self, session: dict, run_id: str, error: DomainError, trace: list) -> None:
        serialized_error = json_text(error_payload(error))
        serialized_trace = json_text(trace)
        failure: DomainError | None = None
        with self.transaction() as connection:
            self.require_session(connection, session)
            now = self.now()
            self.interrupt_expired_runs(connection, session["id"], now)
            run = self.require_run(connection, session, run_id)
            if run["status"] != "running":
                failure = DomainError("RUN_NOT_ACTIVE", "该运行已结束，不能再改写错误。", 409)
            else:
                connection.execute(
                    """UPDATE runs SET status = 'failed', finished_at = ?, error_json = ?, trace_json = ?
                       WHERE id = ? AND session_id = ? AND status = 'running'""",
                    (utc_text(now), serialized_error, serialized_trace, run_id, session["id"]),
                )
        if failure is not None:
            raise failure

    def run_details(self, session: dict, run_id: str) -> dict:
        with self.transaction() as connection:
            self.require_session(connection, session)
            self.interrupt_expired_runs(connection, session["id"], self.now())
            row = self.require_run(connection, session, run_id)
            turn_row = connection.execute(
                "SELECT payload_json FROM turns WHERE run_id = ? AND session_id = ?",
                (run_id, session["id"]),
            ).fetchone()
        result = {
            "id": row["id"],
            "status": row["status"],
            "runtime": row["runtime"],
            "trace": json.loads(row["trace_json"]),
            "turn": json.loads(turn_row["payload_json"]) if turn_row is not None else None,
        }
        if row["error_json"] is not None:
            result["error"] = json.loads(row["error_json"])
        return result
