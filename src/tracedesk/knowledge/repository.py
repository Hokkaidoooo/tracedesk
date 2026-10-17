"""Tenant-scoped persistence; external parsers and models never enter these transactions."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import timedelta
from uuid import uuid4

from ..errors import DomainError
from ..evidence import Evidence, ParsedBlock
from ..store import Store, error_payload, json_text, utc_text
from .embeddings import Vector, validate_vectors
from .jsonio import dump_json, load_json

_VERSION_SELECT = """
    SELECT v.document_id, v.tenant_id, d.title, v.version, v.filename, v.media_type,
           v.source_sha256, v.status, v.parser, v.parse_id, v.parse_started_at,
           v.created_at, v.finished_at, v.error_json, v.lease_expires_at,
           (SELECT count(*) FROM knowledge_parse_attempts a WHERE a.document_id = v.document_id
            AND a.tenant_id = v.tenant_id AND a.version = v.version) AS attempt_count,
           (SELECT count(*) FROM knowledge_blocks b WHERE b.document_id = v.document_id
            AND b.tenant_id = v.tenant_id AND b.version = v.version) AS block_count
    FROM knowledge_versions v JOIN knowledge_documents d
      ON d.id = v.document_id AND d.tenant_id = v.tenant_id
"""

_EVIDENCE_SELECT = """
    SELECT b.*, d.title, v.source_sha256 FROM knowledge_snapshot_members m
    JOIN knowledge_blocks b ON b.source_id = m.source_id AND b.tenant_id = m.tenant_id
    JOIN knowledge_versions v ON v.document_id = b.document_id
      AND v.tenant_id = b.tenant_id AND v.version = b.version
    JOIN knowledge_documents d ON d.id = b.document_id AND d.tenant_id = b.tenant_id
    WHERE m.snapshot_id = ? AND m.tenant_id = ?
    ORDER BY d.id, b.version, b.ordinal
"""


def block_digest(block: ParsedBlock) -> str:
    body = dump_json({"text": block.text, "page": block.page, "kind": block.kind})
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def source_id(document_id: str, version: int, ordinal: int, digest: str) -> str:
    return f"{document_id}@{version}:{ordinal}:{digest[:16]}"


def _document(row: sqlite3.Row, *, idempotent: bool = False) -> dict:
    result = dict(row)
    error = result.pop("error_json")
    result["error"] = load_json(error) if error is not None else None
    result.pop("lease_expires_at", None)
    result["idempotent"] = idempotent
    return result


def _snapshot(row: sqlite3.Row) -> dict:
    result = dict(row)
    error = result.pop("error_json")
    result["error"] = load_json(error) if error is not None else None
    result["artifacts"] = load_json(result.pop("artifacts_json"))
    result["with_embeddings"] = bool(result["with_embeddings"])
    result.pop("lease_expires_at", None)
    return result


def failure_payload(error: BaseException, code: str) -> dict:
    if isinstance(error, DomainError):
        return error_payload(error)
    return error_payload(
        DomainError(
            code, "处理失败，未发布此次结果。", 500, {"exception_type": type(error).__name__}
        )
    )


class KnowledgeRepository:
    def __init__(self, store: Store) -> None:
        self.store = store

    def scope(self, session: dict) -> dict:
        with self.store.transaction(write=False) as connection:
            return dict(self.store.require_session(connection, session))

    def expire_work(self, session: dict) -> None:
        with self.store.transaction() as connection:
            scope = self.store.require_session(connection, session)
            self._expire_builds(connection, scope["tenant_id"])
            rows = connection.execute(
                _VERSION_SELECT + " WHERE v.tenant_id = ? AND v.status = 'parsing'",
                (scope["tenant_id"],),
            ).fetchall()
            for row in rows:
                self._expire_parse(connection, row)

    def begin_document(
        self,
        session: dict,
        *,
        filename: str,
        title: str,
        content: bytes,
        media_type: str,
        parser: str,
        timeout_seconds: float,
    ) -> dict:
        digest = hashlib.sha256(content).hexdigest()
        with self.store.transaction() as connection:
            scope = self.store.require_session(connection, session)
            tenant = scope["tenant_id"]
            existing = connection.execute(
                _VERSION_SELECT + " WHERE v.tenant_id = ? AND v.source_sha256 = ?",
                (tenant, digest),
            ).fetchone()
            if existing is not None:
                self._expire_parse(connection, existing)
                row = self._version(
                    connection, tenant, existing["document_id"], existing["version"]
                )
                return _document(row, idempotent=True)
            document = connection.execute(
                "SELECT id FROM knowledge_documents WHERE tenant_id = ? AND title = ?",
                (tenant, title),
            ).fetchone()
            document_id = document["id"] if document is not None else str(uuid4())
            now = self.store.now()
            if document is None:
                connection.execute(
                    "INSERT INTO knowledge_documents(id, tenant_id, title, created_at) VALUES (?, ?, ?, ?)",
                    (document_id, tenant, title, utc_text(now)),
                )
            version = connection.execute(
                """SELECT COALESCE(max(version), 0) + 1 FROM knowledge_versions
                   WHERE document_id = ? AND tenant_id = ?""",
                (document_id, tenant),
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO knowledge_versions(
                   document_id, tenant_id, version, filename, media_type, source_sha256,
                   original, parser, parse_id, parse_started_at, status, created_at, lease_expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'parsing', ?, ?)""",
                (
                    document_id,
                    tenant,
                    version,
                    filename,
                    media_type,
                    digest,
                    content,
                    parser,
                    str(uuid4()),
                    utc_text(now),
                    utc_text(now),
                    utc_text(now + timedelta(seconds=timeout_seconds)),
                ),
            )
            return _document(self._version(connection, tenant, document_id, version))

    def retry_document(
        self, session: dict, document_id: str, version: int, *, parser: str, timeout_seconds: float
    ) -> dict:
        self.expire_work(session)
        with self.store.transaction() as connection:
            scope = self.store.require_session(connection, session)
            tenant = scope["tenant_id"]
            row = self._version(connection, tenant, document_id, version)
            if row["status"] != "failed":
                raise DomainError("DOCUMENT_NOT_FAILED", "只能显式重试解析失败的文档版本。", 409)
            now = self.store.now()
            connection.execute(
                """UPDATE knowledge_versions SET status = 'parsing', parser = ?, parse_id = ?,
                   parse_started_at = ?, lease_expires_at = ?, finished_at = NULL, error_json = NULL
                   WHERE document_id = ? AND tenant_id = ? AND version = ? AND status = 'failed'""",
                (
                    parser,
                    str(uuid4()),
                    utc_text(now),
                    utc_text(now + timedelta(seconds=timeout_seconds)),
                    document_id,
                    tenant,
                    version,
                ),
            )
            return _document(self._version(connection, tenant, document_id, version))

    def _expire_parse(self, connection: sqlite3.Connection, row: sqlite3.Row) -> None:
        now = utc_text(self.store.now())
        if row["status"] == "parsing" and row["lease_expires_at"] <= now:
            error = DomainError("DOCUMENT_INTERRUPTED", "前次文档解析已超过租约，结果未完成。", 409)
            connection.execute(
                """UPDATE knowledge_versions SET status = 'failed', finished_at = ?, error_json = ?
                   WHERE document_id = ? AND tenant_id = ? AND version = ? AND status = 'parsing'""",
                (
                    now,
                    json_text(error_payload(error)),
                    row["document_id"],
                    row["tenant_id"],
                    row["version"],
                ),
            )

    @staticmethod
    def _version(
        connection: sqlite3.Connection, tenant: str, document_id: str, version: int
    ) -> sqlite3.Row:
        row = connection.execute(
            _VERSION_SELECT + " WHERE v.tenant_id = ? AND v.document_id = ? AND v.version = ?",
            (tenant, document_id, version),
        ).fetchone()
        if row is None:
            raise DomainError("DOCUMENT_NOT_FOUND", "未找到当前商店的文档版本。", 404)
        return row

    def complete_document(
        self, session: dict, document: dict, blocks: tuple[ParsedBlock, ...]
    ) -> dict:
        with self.store.transaction() as connection:
            scope = self.store.require_session(connection, session)
            row = self._version(
                connection, scope["tenant_id"], document["document_id"], document["version"]
            )
            if (
                row["status"] != "parsing"
                or row["parse_id"] != document["parse_id"]
                or row["lease_expires_at"] <= utc_text(self.store.now())
            ):
                raise DomainError("DOCUMENT_NOT_ACTIVE", "文档解析已结束或租约已过期。", 409)
            for ordinal, block in enumerate(blocks):
                digest = block_digest(block)
                connection.execute(
                    """INSERT INTO knowledge_blocks(source_id, document_id, tenant_id, version,
                       ordinal, text, page, kind, block_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        source_id(row["document_id"], row["version"], ordinal, digest),
                        row["document_id"],
                        row["tenant_id"],
                        row["version"],
                        ordinal,
                        block.text,
                        block.page,
                        block.kind,
                        digest,
                    ),
                )
            connection.execute(
                """UPDATE knowledge_versions SET status = 'ready', finished_at = ?
                   WHERE document_id = ? AND tenant_id = ? AND version = ?""",
                (utc_text(self.store.now()), row["document_id"], row["tenant_id"], row["version"]),
            )
            return _document(
                self._version(connection, row["tenant_id"], row["document_id"], row["version"])
            )

    def fail_document(self, session: dict, document: dict, error: BaseException) -> None:
        serialized = json_text(failure_payload(error, "DOCUMENT_PARSE_FAILED"))
        with self.store.transaction() as connection:
            scope = self.store.require_session(connection, session)
            connection.execute(
                """UPDATE knowledge_versions SET status = 'failed', finished_at = ?, error_json = ?
                   WHERE document_id = ? AND tenant_id = ? AND version = ? AND parse_id = ? AND status = 'parsing'""",
                (
                    utc_text(self.store.now()),
                    serialized,
                    document["document_id"],
                    scope["tenant_id"],
                    document["version"],
                    document["parse_id"],
                ),
            )

    def list_documents(self, session: dict) -> list[dict]:
        self.expire_work(session)
        with self.store.transaction(write=False) as connection:
            scope = self.store.require_session(connection, session)
            rows = connection.execute(
                _VERSION_SELECT + " WHERE v.tenant_id = ? ORDER BY d.title, v.version DESC",
                (scope["tenant_id"],),
            ).fetchall()
            return [_document(row) for row in rows]

    def original(self, session: dict, document_id: str, version: int) -> dict:
        with self.store.transaction(write=False) as connection:
            scope = self.store.require_session(connection, session)
            row = connection.execute(
                """SELECT original, filename, media_type, source_sha256 FROM knowledge_versions
                   WHERE document_id = ? AND tenant_id = ? AND version = ?""",
                (document_id, scope["tenant_id"], version),
            ).fetchone()
            if row is None:
                raise DomainError("DOCUMENT_NOT_FOUND", "未找到当前商店的文档版本。", 404)
            self._verify_original(row["original"], row["source_sha256"])
            return {
                "content": row["original"],
                "filename": row["filename"],
                "media_type": row["media_type"],
                "sha256": row["source_sha256"],
            }

    @staticmethod
    def _verify_original(content: bytes, digest: str) -> None:
        if hashlib.sha256(content).hexdigest() != digest:
            raise DomainError("SOURCE_INTEGRITY", "原件内容与保存的 SHA-256 不一致。", 500)

    def list_snapshots(self, session: dict) -> list[dict]:
        self.expire_work(session)
        with self.store.transaction(write=False) as connection:
            scope = self.store.require_session(connection, session)
            rows = connection.execute(
                "SELECT * FROM knowledge_snapshots WHERE tenant_id = ? ORDER BY created_at DESC, id",
                (scope["tenant_id"],),
            ).fetchall()
            return [_snapshot(row) for row in rows]

    def current_snapshot(self, session: dict) -> dict | None:
        with self.store.transaction(write=False) as connection:
            scope = self.store.require_session(connection, session)
            row = self._published(connection, scope["tenant_id"], None)
            return _snapshot(row) if row is not None else None

    @staticmethod
    def _published(
        connection: sqlite3.Connection, tenant: str, snapshot_id: str | None
    ) -> sqlite3.Row | None:
        if snapshot_id is None:
            return connection.execute(
                """SELECT s.* FROM knowledge_publications p JOIN knowledge_snapshots s
                   ON s.id = p.snapshot_id AND s.tenant_id = p.tenant_id
                   WHERE p.tenant_id = ? AND s.status = 'published'""",
                (tenant,),
            ).fetchone()
        row = connection.execute(
            "SELECT * FROM knowledge_snapshots WHERE id = ? AND tenant_id = ? AND status = 'published'",
            (snapshot_id, tenant),
        ).fetchone()
        if row is None:
            raise DomainError("SNAPSHOT_NOT_FOUND", "未找到当前商店的已发布快照。", 404)
        return row

    def read_snapshot(
        self, session: dict, snapshot_id: str | None = None
    ) -> tuple[dict | None, tuple[Evidence, ...]]:
        with self.store.transaction(write=False) as connection:
            scope = self.store.require_session(connection, session)
            row = self._published(connection, scope["tenant_id"], snapshot_id)
            if row is None:
                return None, ()
            evidence = self._evidence(connection, scope["tenant_id"], row["id"])
            if len(evidence) != row["source_count"]:
                raise DomainError("SOURCE_INTEGRITY", "快照来源数量与构建清单不一致。", 500)
            return _snapshot(row), evidence

    def _evidence(
        self, connection: sqlite3.Connection, tenant: str, snapshot_id: str
    ) -> tuple[Evidence, ...]:
        originals = connection.execute(
            """SELECT v.original, v.source_sha256 FROM knowledge_versions v
               WHERE v.tenant_id = ? AND EXISTS (
                 SELECT 1 FROM knowledge_blocks b JOIN knowledge_snapshot_members m
                   ON m.source_id = b.source_id AND m.tenant_id = b.tenant_id
                 WHERE b.document_id = v.document_id AND b.tenant_id = v.tenant_id
                   AND b.version = v.version AND m.snapshot_id = ?)""",
            (tenant, snapshot_id),
        )
        for original in originals:
            self._verify_original(original["original"], original["source_sha256"])
        return tuple(
            self._row_evidence(row, snapshot_id)
            for row in connection.execute(_EVIDENCE_SELECT, (snapshot_id, tenant))
        )

    @staticmethod
    def _row_evidence(row: sqlite3.Row, snapshot_id: str) -> Evidence:
        block = ParsedBlock(row["text"], page=row["page"], kind=row["kind"])
        digest = block_digest(block)
        identity = source_id(row["document_id"], row["version"], row["ordinal"], digest)
        if digest != row["block_sha256"] or identity != row["source_id"]:
            raise DomainError("SOURCE_INTEGRITY", "证据块内容与稳定来源 ID 不一致。", 500)
        return Evidence(
            source_id=row["source_id"],
            tenant_id=row["tenant_id"],
            snapshot_id=snapshot_id,
            document_id=row["document_id"],
            document_version=row["version"],
            title=row["title"],
            text=row["text"],
            source_sha256=row["source_sha256"],
            page=row["page"],
            kind=row["kind"],
        )

    def begin_snapshot(
        self, session: dict, *, fingerprint: str | None, timeout_seconds: float
    ) -> tuple[dict, tuple[Evidence, ...]]:
        self.expire_work(session)
        with self.store.transaction() as connection:
            scope = self.store.require_session(connection, session)
            tenant = scope["tenant_id"]
            if connection.execute(
                "SELECT 1 FROM knowledge_snapshots WHERE tenant_id = ? AND status = 'building'",
                (tenant,),
            ).fetchone():
                raise DomainError("SNAPSHOT_BUSY", "当前商店已有正在构建的知识快照。", 409)
            versions = self._latest_versions(connection, tenant)
            sources = self._latest_sources(connection, tenant, versions)
            snapshot_id = str(uuid4())
            now = self.store.now()
            connection.execute(
                """INSERT INTO knowledge_snapshots(id, tenant_id, status, created_at,
                   lease_expires_at, with_embeddings, embedding_fingerprint, source_count)
                   VALUES (?, ?, 'building', ?, ?, ?, ?, ?)""",
                (
                    snapshot_id,
                    tenant,
                    utc_text(now),
                    utc_text(now + timedelta(seconds=timeout_seconds)),
                    int(fingerprint is not None),
                    fingerprint,
                    len(sources),
                ),
            )
            connection.executemany(
                "INSERT INTO knowledge_snapshot_members(snapshot_id, tenant_id, source_id) VALUES (?, ?, ?)",
                [(snapshot_id, tenant, source) for source in sources],
            )
            row = connection.execute(
                "SELECT * FROM knowledge_snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
            return _snapshot(row), self._evidence(connection, tenant, snapshot_id)

    def _expire_builds(self, connection: sqlite3.Connection, tenant: str) -> None:
        now = utc_text(self.store.now())
        error = DomainError("SNAPSHOT_INTERRUPTED", "前次构建超过租约，未切换已发布快照。", 409)
        connection.execute(
            """UPDATE knowledge_snapshots SET status = 'failed', finished_at = ?, error_json = ?
               WHERE tenant_id = ? AND status = 'building' AND lease_expires_at <= ?""",
            (now, json_text(error_payload(error)), tenant, now),
        )

    def _latest_versions(self, connection: sqlite3.Connection, tenant: str) -> list[sqlite3.Row]:
        rows = connection.execute(
            _VERSION_SELECT
            + """ WHERE v.tenant_id = ? AND v.version = (
                SELECT max(n.version) FROM knowledge_versions n
                WHERE n.document_id = v.document_id AND n.tenant_id = v.tenant_id)
                ORDER BY v.document_id""",
            (tenant,),
        ).fetchall()
        if not rows:
            raise DomainError("KNOWLEDGE_EMPTY", "请先上传至少一份可检索的文档。", 409)
        unfinished = [row["document_id"] for row in rows if row["status"] != "ready"]
        if unfinished:
            raise DomainError(
                "DOCUMENT_NOT_READY",
                "最新文档版本尚未解析成功，不能构建快照。",
                409,
                {"document_ids": unfinished},
            )
        return rows

    @staticmethod
    def _latest_sources(
        connection: sqlite3.Connection, tenant: str, versions: list[sqlite3.Row]
    ) -> list[str]:
        sources = []
        for version in versions:
            rows = connection.execute(
                """SELECT source_id FROM knowledge_blocks
                   WHERE tenant_id = ? AND document_id = ? AND version = ? ORDER BY ordinal""",
                (tenant, version["document_id"], version["version"]),
            ).fetchall()
            if not rows:
                raise DomainError("SOURCE_INTEGRITY", "已解析文档缺少来源块。", 500)
            sources.extend(row["source_id"] for row in rows)
        return sources

    def publish_snapshot(
        self,
        session: dict,
        snapshot_id: str,
        evidence: tuple[Evidence, ...],
        vectors: tuple[Vector, ...],
        artifacts_json: str,
    ) -> dict:
        with self.store.transaction() as connection:
            scope = self.store.require_session(connection, session)
            tenant = scope["tenant_id"]
            row = connection.execute(
                "SELECT * FROM knowledge_snapshots WHERE id = ? AND tenant_id = ?",
                (snapshot_id, tenant),
            ).fetchone()
            if (
                row is None
                or row["status"] != "building"
                or row["lease_expires_at"] <= utc_text(self.store.now())
            ):
                raise DomainError("SNAPSHOT_NOT_ACTIVE", "知识快照已结束或构建租约已过期。", 409)
            if row["with_embeddings"]:
                if len(vectors) != len(evidence):
                    raise DomainError("EMBEDDING_PROTOCOL", "向量数量与快照来源不一致。", 502)
                connection.executemany(
                    """INSERT INTO knowledge_vectors(snapshot_id, tenant_id, source_id, dimensions, vector_json)
                       VALUES (?, ?, ?, ?, ?)""",
                    [
                        (snapshot_id, tenant, item.source_id, len(vector), json_text(vector))
                        for item, vector in zip(evidence, vectors, strict=True)
                    ],
                )
            connection.execute(
                """UPDATE knowledge_snapshots SET status = 'published', finished_at = ?, artifacts_json = ?
                   WHERE id = ? AND tenant_id = ? AND status = 'building'""",
                (utc_text(self.store.now()), artifacts_json, snapshot_id, tenant),
            )
            connection.execute(
                """INSERT INTO knowledge_publications(tenant_id, snapshot_id) VALUES (?, ?)
                   ON CONFLICT(tenant_id) DO UPDATE SET snapshot_id = excluded.snapshot_id""",
                (tenant, snapshot_id),
            )
            return _snapshot(self._published(connection, tenant, snapshot_id))

    def fail_snapshot(self, session: dict, snapshot_id: str, error: BaseException) -> None:
        serialized = json_text(failure_payload(error, "SNAPSHOT_BUILD_FAILED"))
        with self.store.transaction() as connection:
            scope = self.store.require_session(connection, session)
            connection.execute(
                """UPDATE knowledge_snapshots SET status = 'failed', finished_at = ?, error_json = ?
                   WHERE id = ? AND tenant_id = ? AND status = 'building'""",
                (utc_text(self.store.now()), serialized, snapshot_id, scope["tenant_id"]),
            )

    def vectors(self, session: dict, snapshot_id: str, dimensions: int) -> dict[str, Vector]:
        with self.store.transaction(write=False) as connection:
            scope = self.store.require_session(connection, session)
            snapshot = self._published(connection, scope["tenant_id"], snapshot_id)
            rows = connection.execute(
                """SELECT source_id, dimensions, vector_json FROM knowledge_vectors
                   WHERE snapshot_id = ? AND tenant_id = ? ORDER BY source_id""",
                (snapshot_id, scope["tenant_id"]),
            ).fetchall()
            if len(rows) != snapshot["source_count"] or any(
                row["dimensions"] != dimensions for row in rows
            ):
                raise DomainError("SOURCE_INTEGRITY", "已发布向量索引不完整或维度不一致。", 500)
            vectors = validate_vectors(
                [load_json(row["vector_json"]) for row in rows], len(rows), dimensions
            )
            return {row["source_id"]: vector for row, vector in zip(rows, vectors, strict=True)}
