from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tracedesk.errors import DomainError
from tracedesk.evidence import ParsedBlock
from tracedesk.knowledge import KnowledgeConfig, KnowledgeService
from tracedesk.store import Store


class FixtureEmbedder:
    """A protocol fixture with inspectable calls; it is not a model-quality benchmark."""

    fingerprint = "fixture-model-v1"
    dimensions = 2

    def __init__(self) -> None:
        self.calls = []
        self.query_started = None
        self.query_release = None

    async def embed(self, texts, *, kind="document"):
        self.calls.append((texts, kind))
        if kind == "query" and self.query_started is not None:
            self.query_started.set()
            await self.query_release.wait()
        return tuple(
            (1.0, 0.0) if "退款" in text or "money" in text else (0.0, 1.0) for text in texts
        )


@pytest.fixture
def environment(tmp_path: Path):
    moment = [datetime(2026, 10, 17, 0, tzinfo=timezone.utc)]
    store = Store(tmp_path / "knowledge.db", clock=lambda: moment[0])
    store.initialize(
        [
            {"id": "alice", "tenant_id": "shop", "name": "A"},
            {"id": "bob", "tenant_id": "shop", "name": "B"},
            {"id": "carol", "tenant_id": "other", "name": "C"},
        ],
        [],
    )
    sessions = {
        name: store.authenticate(store.create_session(name)["token"])
        for name in ("alice", "bob", "carol")
    }
    embedder = FixtureEmbedder()
    service = KnowledgeService(store, KnowledgeConfig(), embedder=embedder)
    return service, store, sessions, embedder, moment


def ingest(service, session, text="七天内支持原路退款。", title="售后政策", filename="policy.md"):
    return asyncio.run(
        service.ingest(
            session,
            filename=filename,
            title=title,
            content=text.encode("utf-8"),
            media_type="text/markdown",
        )
    )


def error(code, callback):
    with pytest.raises(DomainError) as caught:
        callback()
    assert caught.value.code == code
    return caught.value


def test_text_table_ingest_and_published_sources_are_stable(environment) -> None:
    service, _, sessions, _, _ = environment
    session = sessions["alice"]
    assert service.current_snapshot(session) is None
    assert service.evidence(session) == ()
    content = "# 售后\n\n七天内支持原路退款。\n\n|项目|期限|\n|---|---|\n|发票|三天|"
    document = ingest(service, session, content)
    assert document["status"] == "ready"
    assert document["block_count"] == 3
    assert document["version"] == 1
    assert "error_json" not in document
    snapshot = asyncio.run(service.build_snapshot(session))
    evidence = service.evidence(session)
    assert len(evidence) == 3
    assert {item.snapshot_id for item in evidence} == {snapshot["id"]}
    assert all(item.page is None for item in evidence)
    assert {item.kind for item in evidence} == {"text", "table"}
    assert {item.source_sha256 for item in evidence} == {
        hashlib.sha256(content.encode()).hexdigest()
    }
    assert service.original(session, document["document_id"], 1)["content"] == content.encode()
    next_snapshot = asyncio.run(service.build_snapshot(session))
    assert next_snapshot["id"] != snapshot["id"]
    assert [item.source_id for item in service.evidence(session)] == [
        item.source_id for item in evidence
    ]
    hits = asyncio.run(service.search(session, "发票"))
    assert hits[0].kind == "table"
    assert hits[0].method == "lexical"
    assert service.list_documents(sessions["bob"])[0]["document_id"] == document["document_id"]


def test_duplicate_original_is_idempotent_and_new_title_content_advances_version(
    environment,
) -> None:
    service, _, sessions, _, _ = environment
    session = sessions["alice"]
    first = ingest(service, session)
    older = asyncio.run(service.build_snapshot(session))
    duplicate = ingest(service, session, title="另一个标题")
    assert duplicate["document_id"] == first["document_id"]
    assert duplicate["title"] == "售后政策"
    assert duplicate["idempotent"] is True
    second = ingest(service, session, "签收十四天内可以申请售后。")
    assert second["document_id"] == first["document_id"]
    assert second["version"] == 2
    assert service.evidence(session)[0].document_version == 1
    newer = asyncio.run(service.build_snapshot(session))
    assert service.current_snapshot(session)["id"] == newer["id"]
    assert service.evidence(session)[0].document_version == 2
    assert service.evidence(session, older["id"])[0].document_version == 1
    assert [item["version"] for item in service.list_documents(session)] == [2, 1]


def test_tenant_scope_applies_to_history_originals_and_forged_session(environment) -> None:
    service, _, sessions, _, _ = environment
    alice, carol = sessions["alice"], sessions["carol"]
    document = ingest(service, alice)
    snapshot = asyncio.run(service.build_snapshot(alice))
    other_document = ingest(service, carol)
    assert other_document["document_id"] != document["document_id"]
    error("SNAPSHOT_NOT_FOUND", lambda: service.evidence(carol, snapshot["id"]))
    error("DOCUMENT_NOT_FOUND", lambda: service.original(carol, document["document_id"], 1))
    error(
        "SNAPSHOT_NOT_FOUND",
        lambda: asyncio.run(service.search(carol, "退款", snapshot_id=snapshot["id"])),
    )
    forged = {**alice, "tenant_id": carol["tenant_id"]}
    for callback in (
        lambda: service.list_documents(forged),
        lambda: service.list_snapshots(forged),
        lambda: service.current_snapshot(forged),
        lambda: service.evidence(forged),
        lambda: service.original(forged, document["document_id"], 1),
        lambda: asyncio.run(service.build_snapshot(forged, with_embeddings=True)),
        lambda: asyncio.run(service.search(forged, "退款")),
        lambda: ingest(service, forged),
    ):
        error("UNAUTHORIZED", callback)
    assert service.list_snapshots(carol) == []


def test_missing_pdf_parser_persists_failure_and_original(environment) -> None:
    service, _, sessions, _, _ = environment
    session = sessions["alice"]
    raw = b"%PDF-1.7\nfixture content"

    def upload():
        return asyncio.run(
            service.ingest(
                session,
                filename="guide.pdf",
                title="指南",
                content=raw,
                media_type="application/pdf",
            )
        )

    error("PARSER_NOT_CONFIGURED", upload)
    document = service.list_documents(session)[0]
    assert document["status"] == "failed"
    assert document["error"]["code"] == "PARSER_NOT_CONFIGURED"
    assert document["block_count"] == 0
    assert service.original(session, document["document_id"], 1)["content"] == raw
    assert upload()["idempotent"] is True
    error("DOCUMENT_NOT_READY", lambda: asyncio.run(service.build_snapshot(session)))
    assert service.current_snapshot(session) is None


def test_latest_failed_version_cannot_silently_reuse_older_source(environment) -> None:
    service, _, sessions, _, _ = environment
    session = sessions["alice"]
    ingest(service, session)
    published = asyncio.run(service.build_snapshot(session))
    with pytest.raises(DomainError, match="UTF-8"):
        asyncio.run(
            service.ingest(
                session,
                filename="policy.md",
                title="售后政策",
                content=b"\xff",
                media_type="text/markdown",
            )
        )
    error("DOCUMENT_NOT_READY", lambda: asyncio.run(service.build_snapshot(session)))
    assert service.current_snapshot(session)["id"] == published["id"]
    assert service.evidence(session)[0].document_version == 1


def test_explicit_retry_keeps_original_version_and_preserves_failed_attempt(environment) -> None:
    service, store, sessions, _, _ = environment
    session = sessions["alice"]
    raw = b"%PDF-1.7\nprotocol fixture"
    error(
        "PARSER_NOT_CONFIGURED",
        lambda: asyncio.run(
            service.ingest(
                session,
                filename="guide.pdf",
                title="指南",
                content=raw,
                media_type="application/pdf",
            )
        ),
    )
    failed = service.list_documents(session)[0]

    class Parser:
        name = "configured-fixture"

        async def parse(self, content, *, filename, media_type):
            assert content == raw
            return (ParsedBlock("解析成功后的正文。", page=2),)

    configured = KnowledgeService(store, KnowledgeConfig(), parser=Parser())
    ready = asyncio.run(
        configured.retry_document(session, failed["document_id"], failed["version"])
    )
    assert ready["version"] == failed["version"]
    assert ready["document_id"] == failed["document_id"]
    assert ready["source_sha256"] == failed["source_sha256"]
    assert ready["parse_id"] != failed["parse_id"]
    assert ready["attempt_count"] == 2
    assert ready["status"] == "ready"
    with store.transaction(write=False) as connection:
        attempts = {
            row["id"]: dict(row)
            for row in connection.execute("SELECT * FROM knowledge_parse_attempts")
        }
    assert attempts[failed["parse_id"]]["status"] == "failed"
    assert attempts[ready["parse_id"]]["status"] == "ready"
    assert attempts[failed["parse_id"]]["error_json"]
    error(
        "DOCUMENT_NOT_FAILED",
        lambda: asyncio.run(configured.retry_document(session, ready["document_id"], 1)),
    )
    error(
        "DOCUMENT_NOT_FOUND",
        lambda: asyncio.run(configured.retry_document(sessions["carol"], ready["document_id"], 1)),
    )


def test_late_parse_attempt_cannot_complete_or_fail_an_explicit_retry(environment) -> None:
    service, _, sessions, _, moment = environment
    session = sessions["alice"]
    repository = service.repository
    first = repository.begin_document(
        session,
        filename="guide.txt",
        title="指南",
        content=b"original",
        media_type="text/plain",
        parser="first",
        timeout_seconds=1,
    )
    moment[0] += timedelta(seconds=1)
    assert service.list_documents(session)[0]["status"] == "failed"
    second = repository.retry_document(
        session, first["document_id"], 1, parser="second", timeout_seconds=5
    )
    error(
        "DOCUMENT_NOT_ACTIVE",
        lambda: repository.complete_document(session, first, (ParsedBlock("旧结果"),)),
    )
    repository.fail_document(session, first, RuntimeError("late failure"))
    assert service.list_documents(session)[0]["status"] == "parsing"
    repository.complete_document(session, second, (ParsedBlock("新结果"),))
    asyncio.run(service.build_snapshot(session))
    assert service.evidence(session)[0].text == "新结果"


def test_parse_and_build_timeouts_persist_explicit_failed_status(environment) -> None:
    _, store, sessions, _, _ = environment
    session = sessions["alice"]

    class Parser:
        name = "timeout-fixture"

        async def parse(self, content, *, filename, media_type):
            await asyncio.Event().wait()

    failing = KnowledgeService(store, KnowledgeConfig(parse_timeout_seconds=0.01), parser=Parser())
    error("PARSER_TIMEOUT", lambda: ingest(failing, session))
    assert failing.list_documents(session)[0]["error"]["code"] == "PARSER_TIMEOUT"

    service = KnowledgeService(store, KnowledgeConfig(build_timeout_seconds=0.01))
    failed = service.list_documents(session)[0]
    asyncio.run(service.retry_document(session, failed["document_id"], 1))

    async def enrichment(evidence):
        await asyncio.Event().wait()

    error(
        "SNAPSHOT_TIMEOUT",
        lambda: asyncio.run(service.build_snapshot(session, enricher=enrichment)),
    )
    assert service.list_snapshots(session)[0]["error"]["code"] == "SNAPSHOT_TIMEOUT"
    assert service.current_snapshot(session) is None


def test_parser_and_enricher_run_outside_write_transactions(environment) -> None:
    _, store, sessions, _, _ = environment
    session = sessions["alice"]

    class Parser:
        name = "fixture-parser"

        async def parse(self, content, *, filename, media_type):
            with store.transaction() as connection:
                connection.execute("INSERT INTO metadata VALUES ('parser-write', 'ok')")
            return (ParsedBlock("发票可在三个工作日内开具。", page=3),)

    service = KnowledgeService(store, KnowledgeConfig(), parser=Parser())
    document = ingest(service, session)

    async def enrich(evidence):
        assert service.current_snapshot(session) is None
        assert len({item.snapshot_id for item in evidence}) == 1
        error("SNAPSHOT_NOT_FOUND", lambda: service.evidence(session, evidence[0].snapshot_id))
        with store.transaction() as connection:
            connection.execute("INSERT INTO metadata VALUES ('graph-write', 'ok')")
        return {"graph": {"source_ids": [item.source_id for item in evidence]}}

    snapshot = asyncio.run(service.build_snapshot(session, enricher=enrich))
    assert snapshot["artifacts"]["graph"]["source_ids"] == [service.evidence(session)[0].source_id]
    assert service.evidence(session)[0].page == 3
    assert document["parser"] == "fixture-parser"


def test_failed_enrichment_preserves_previous_snapshot_and_no_partial_vectors(environment) -> None:
    service, store, sessions, _, _ = environment
    session = sessions["alice"]
    ingest(service, session)
    published = asyncio.run(service.build_snapshot(session))

    async def broken(evidence):
        assert evidence[0].snapshot_id != published["id"]
        raise RuntimeError("graph projection failed")

    with pytest.raises(RuntimeError, match="graph projection failed"):
        asyncio.run(service.build_snapshot(session, with_embeddings=True, enricher=broken))
    snapshots = service.list_snapshots(session)
    failed = next(item for item in snapshots if item["status"] == "failed")
    assert failed["error"]["details"]["exception_type"] == "RuntimeError"
    assert service.current_snapshot(session)["id"] == published["id"]
    error("SNAPSHOT_NOT_FOUND", lambda: service.evidence(session, failed["id"]))
    with store.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM knowledge_vectors").fetchone()[0] == 0


@pytest.mark.parametrize(
    "artifact", [{"bad": float("nan")}, {1: "bad"}, {"bad": (1, 2)}, ["not-object"]]
)
def test_non_json_artifacts_fail_without_publishing(environment, artifact) -> None:
    service, _, sessions, _, _ = environment
    session = sessions["alice"]
    ingest(service, session)

    async def enrich(evidence):
        return artifact

    with pytest.raises((ValueError, DomainError)):
        asyncio.run(service.build_snapshot(session, enricher=enrich))
    assert service.current_snapshot(session) is None
    assert service.list_snapshots(session)[0]["status"] == "failed"


@pytest.mark.parametrize(
    "vectors", [[], [(1.0,)], [(float("nan"), 0.0)], [(0.0, 0.0)], [(True, 0.0)]]
)
def test_bad_embedding_fails_build_and_preserves_state(environment, vectors) -> None:
    _, store, sessions, _, _ = environment
    session = sessions["alice"]

    class Broken(FixtureEmbedder):
        async def embed(self, texts, *, kind="document"):
            return vectors

    service = KnowledgeService(store, KnowledgeConfig(), embedder=Broken())
    ingest(service, session)
    with pytest.raises(DomainError):
        asyncio.run(service.build_snapshot(session, with_embeddings=True))
    assert service.current_snapshot(session) is None
    assert service.list_snapshots(session)[0]["status"] == "failed"


def test_semantic_and_rrf_require_vectors_and_keep_same_scope(environment) -> None:
    service, _, sessions, embedder, _ = environment
    session = sessions["alice"]
    ingest(service, session)
    ingest(service, session, "电子发票三个工作日出具。", title="发票")
    lexical_snapshot = asyncio.run(service.build_snapshot(session))
    for method in ("semantic", "hybrid"):
        error(
            "SEMANTIC_NOT_AVAILABLE",
            lambda method=method: asyncio.run(service.search(session, "money back", method=method)),
        )
    assert embedder.calls == []
    semantic_snapshot = asyncio.run(service.build_snapshot(session, with_embeddings=True))
    assert asyncio.run(service.search(session, "money back", method="lexical")) == ()
    for method in ("semantic", "hybrid"):
        hits = asyncio.run(service.search(session, "money back", method=method, limit=1))
        assert len(hits) == 1
        assert "退款" in hits[0].text
        assert hits[0].snapshot_id == semantic_snapshot["id"]
        assert hits[0].tenant_id == session["tenant_id"]
        assert hits[0].method == method
    error(
        "SEMANTIC_NOT_AVAILABLE",
        lambda: asyncio.run(
            service.search(session, "money", method="semantic", snapshot_id=lexical_snapshot["id"])
        ),
    )


def test_embedding_model_change_requires_rebuild(environment) -> None:
    service, store, sessions, _, _ = environment
    session = sessions["alice"]
    ingest(service, session)
    asyncio.run(service.build_snapshot(session, with_embeddings=True))
    other = FixtureEmbedder()
    other.fingerprint = "fixture-model-v2"
    restarted = KnowledgeService(store, KnowledgeConfig(), embedder=other)
    error(
        "EMBEDDING_MODEL_MISMATCH",
        lambda: asyncio.run(restarted.search(session, "退款", method="semantic")),
    )
    assert other.calls == []
    assert asyncio.run(restarted.search(session, "退款", method="lexical"))


def test_query_stays_on_captured_snapshot_during_new_publication(environment) -> None:
    service, _, sessions, embedder, _ = environment
    session = sessions["alice"]
    ingest(service, session)
    first = asyncio.run(service.build_snapshot(session, with_embeddings=True))

    async def scenario():
        embedder.query_started = asyncio.Event()
        embedder.query_release = asyncio.Event()
        query = asyncio.create_task(service.search(session, "money back", method="hybrid"))
        await embedder.query_started.wait()
        await service.ingest(
            session,
            filename="policy.md",
            title="售后政策",
            content="十四天退款窗口。".encode(),
            media_type="text/markdown",
        )
        latest = await service.build_snapshot(session, with_embeddings=True)
        embedder.query_release.set()
        hits = await query
        return latest, hits

    latest, hits = asyncio.run(scenario())
    assert latest["id"] != first["id"]
    assert {item.snapshot_id for item in hits} == {first["id"]}
    assert {item.document_version for item in hits} == {1}
    assert service.evidence(session)[0].document_version == 2


def test_duplicate_in_flight_upload_has_single_parser_owner(environment) -> None:
    _, store, sessions, _, _ = environment
    session = sessions["alice"]

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        class Parser:
            name = "fixture-slow"
            calls = 0

            async def parse(self, content, *, filename, media_type):
                self.calls += 1
                started.set()
                await release.wait()
                return (ParsedBlock("同一原件"),)

        parser = Parser()
        service = KnowledgeService(store, KnowledgeConfig(), parser=parser)
        args = {
            "filename": "doc.txt",
            "title": "说明",
            "content": b"same",
            "media_type": "text/plain",
        }
        task = asyncio.create_task(service.ingest(session, **args))
        await started.wait()
        duplicate = await service.ingest(session, **args)
        assert duplicate["idempotent"] is True
        assert duplicate["status"] == "parsing"
        release.set()
        first = await task
        assert first["document_id"] == duplicate["document_id"]
        assert parser.calls == 1
        assert len(service.list_documents(session)) == 1

    asyncio.run(scenario())


def test_concurrent_build_and_cancel_leave_explicit_failure(environment) -> None:
    service, _, sessions, _, _ = environment
    session = sessions["alice"]
    ingest(service, session)

    async def scenario():
        started = asyncio.Event()

        async def enrich(evidence):
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(service.build_snapshot(session, enricher=enrich))
        await started.wait()
        with pytest.raises(DomainError) as caught:
            await service.build_snapshot(session)
        assert caught.value.code == "SNAPSHOT_BUSY"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert service.list_snapshots(session)[0]["status"] == "failed"
    assert service.current_snapshot(session) is None


def test_restart_recovers_expired_build_and_keeps_published_sources(environment) -> None:
    service, store, sessions, embedder, moment = environment
    session = sessions["alice"]
    document = ingest(service, session)
    published = asyncio.run(service.build_snapshot(session))
    orphan, _ = service.repository.begin_snapshot(session, fingerprint=None, timeout_seconds=10)
    moment[0] += timedelta(seconds=10)
    store.initialize([], [])
    restarted = KnowledgeService(store, KnowledgeConfig(), embedder=embedder)
    assert restarted.current_snapshot(session)["id"] == published["id"]
    assert restarted.original(session, document["document_id"], 1)["content"]
    assert (
        next(item for item in restarted.list_snapshots(session) if item["id"] == orphan["id"])[
            "error"
        ]["code"]
        == "SNAPSHOT_INTERRUPTED"
    )
    assert asyncio.run(restarted.build_snapshot(session))["status"] == "published"


def test_original_corruption_is_detected_in_download_and_evidence(environment) -> None:
    service, store, sessions, _, _ = environment
    session = sessions["alice"]
    document = ingest(service, session)
    asyncio.run(service.build_snapshot(session))
    with store.transaction() as connection:
        connection.execute("DROP TRIGGER knowledge_version_transition")
        connection.execute("UPDATE knowledge_versions SET original = ?", (b"tampered",))
    error("SOURCE_INTEGRITY", lambda: service.original(session, document["document_id"], 1))
    error("SOURCE_INTEGRITY", lambda: service.evidence(session))


def test_database_rejects_cross_tenant_projection_and_terminal_mutation(environment) -> None:
    service, store, sessions, _, _ = environment
    alice, carol = sessions["alice"], sessions["carol"]
    ingest(service, alice)
    ingest(service, carol, "另一个商店的私有文档。")
    other_snapshot = asyncio.run(service.build_snapshot(carol))
    foreign_source = service.evidence(carol)[0].source_id
    building, _ = service.repository.begin_snapshot(alice, fingerprint=None, timeout_seconds=10)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        with store.transaction() as connection:
            connection.execute(
                "INSERT INTO knowledge_snapshot_members VALUES (?, ?, ?)",
                (building["id"], alice["tenant_id"], foreign_source),
            )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_snapshots SET artifacts_json = '{}' WHERE id = ?",
                (other_snapshot["id"],),
            )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store.transaction() as connection:
            connection.execute("DELETE FROM knowledge_blocks")


@pytest.mark.parametrize(
    "arguments,code",
    [
        ({"filename": "../secret.md"}, "DOCUMENT_FILENAME"),
        ({"filename": r"..\secret.md"}, "DOCUMENT_FILENAME"),
        ({"filename": "secret.exe"}, "DOCUMENT_MEDIA_TYPE"),
        ({"media_type": "application/pdf"}, "DOCUMENT_MEDIA_TYPE"),
        ({"title": " "}, "DOCUMENT_TITLE"),
        ({"content": b""}, "DOCUMENT_EMPTY"),
    ],
)
def test_upload_boundary_rejects_invalid_inputs(environment, arguments, code) -> None:
    service, _, sessions, _, _ = environment
    args = {
        "filename": "guide.md",
        "title": "指南",
        "content": b"text",
        "media_type": "text/markdown",
        **arguments,
    }
    error(code, lambda: asyncio.run(service.ingest(sessions["alice"], **args)))
    assert service.list_documents(sessions["alice"]) == []
