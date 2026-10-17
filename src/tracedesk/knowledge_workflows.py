"""Bind document and graph research to one authenticated, published snapshot."""

import json
from dataclasses import dataclass, replace
from importlib.resources import files
from typing import Any

from .errors import DomainError
from .evidence import Evidence, GraphFact
from .graphs import GraphService
from .knowledge import KnowledgeService
from .provider import ModelProvider
from .research import ResearchConfig, ResearchOrchestrator
from .store import Store
from .trace import Trace

DOCUMENT_METHODS = {"lexical", "semantic", "hybrid"}
MICROSOFT_METHODS = {"graphrag_local", "graphrag_global"}


@dataclass(frozen=True)
class ResearchRequest:
    snapshot_id: str
    mode: str = "multi"

    @property
    def runtime(self) -> str:
        # The persisted idempotency contract includes the selected mode and immutable snapshot.
        return f"research:{self.mode}:{self.snapshot_id}"


class KnowledgeWorkflows:
    def __init__(
        self,
        store: Store,
        documents: KnowledgeService,
        graphs: GraphService,
        provider: ModelProvider | None,
        research_config: ResearchConfig,
        *,
        history_limit: int = 6,
    ) -> None:
        self.store = store
        self.documents = documents
        self.graphs = graphs
        self.provider = provider
        self.research_config = research_config
        self.history_limit = history_limit

    def snapshot(self, session: dict, snapshot_id: str | None = None) -> dict:
        if snapshot_id is None:
            snapshot = self.documents.current_snapshot(session)
        else:
            snapshot = next(
                (
                    item
                    for item in self.documents.list_snapshots(session)
                    if item["id"] == snapshot_id
                ),
                None,
            )
        if snapshot is None:
            raise DomainError("SNAPSHOT_NOT_FOUND", "请先上传资料并发布知识索引。", 404)
        # read_snapshot owns the state and authorization checks for both current and historical reads.
        self.documents.evidence(session, snapshot["id"])
        return snapshot

    def method_status(self, session: dict, snapshot: dict) -> dict[str, dict]:
        evidence = self.documents.evidence(session, snapshot["id"])
        return {
            **self.documents.method_status(snapshot),
            **self.graphs.method_status(evidence, snapshot["artifacts"]),
        }

    def methods(self, session: dict, snapshot: dict) -> tuple[str, ...]:
        return tuple(
            method
            for method, status in self.method_status(session, snapshot).items()
            if status["available"]
        )

    def catalog(self, session: dict) -> dict[str, Any]:
        snapshot = self.documents.current_snapshot(session)
        statuses = self.method_status(session, snapshot) if snapshot else {}
        return {
            "documents": self.documents.list_documents(session),
            "snapshots": self.documents.list_snapshots(session),
            "current_snapshot": snapshot,
            "methods": [method for method, state in statuses.items() if state["available"]],
            "method_status": statuses,
            "capabilities": {
                "pdf_parser": self.documents.config.pdf_parser,
                "embedding_provider": self.documents.config.embedding_provider,
                "embedding_model": self.documents.config.embedding_model or None,
                "graph_backend": self.graphs.config.backend,
                "graph_extraction": self.graphs.config.extraction,
                "microsoft_graphrag": self.graphs.config.graphrag_enabled,
                "research": self.provider is not None,
                "research_default_mode": self.research_config.mode,
                "max_upload_bytes": self.documents.config.max_upload_bytes,
            },
        }

    async def build(
        self,
        session: dict,
        *,
        with_embeddings: bool,
        with_graph: bool,
        facts: tuple[GraphFact, ...] | None = None,
    ) -> dict:
        if facts and not with_graph:
            raise DomainError("GRAPH_BUILD_REQUIRED", "提供关系时必须同时构建图索引。", 422)
        if with_graph and facts is None and self.graphs.config.extraction == "explicit":
            raise DomainError(
                "GRAPH_FACTS_REQUIRED", "请提供有来源的关系，或启用模型抽取后构建图索引。", 422
            )

        async def prepare(evidence: tuple[Evidence, ...]) -> dict:
            return await self.graphs.prepare(evidence, facts=facts)

        return await self.documents.build_snapshot(
            session, with_embeddings=with_embeddings, enricher=prepare if with_graph else None
        )

    async def load_demo(
        self, session: dict, *, with_embeddings: bool = False, with_graph: bool = True
    ) -> dict:
        resources = files("tracedesk").joinpath("data")
        definitions = json.loads(
            resources.joinpath("knowledge-demo-relations.json").read_text(encoding="utf-8")
        )
        for document in sorted(
            resources.joinpath("knowledge-demo").iterdir(), key=lambda item: item.name
        ):
            if document.name.endswith(".md"):
                content = document.read_bytes()
                title = content.decode("utf-8").splitlines()[0].removeprefix("#").strip()
                await self.documents.ingest(
                    session,
                    filename=document.name,
                    title=title,
                    content=content,
                    media_type="text/markdown",
                )

        async def prepare(evidence: tuple[Evidence, ...]) -> dict:
            facts = []
            for item in definitions:
                references = tuple(
                    source.source_id
                    for source in evidence
                    if source.title == item["document"] and item["quote"] in source.text
                )
                if not references:
                    raise DomainError(
                        "DEMO_SOURCE_MISMATCH", "示例关系无法对应原文，停止发布。", 500
                    )
                facts.append(
                    GraphFact(item["subject"], item["predicate"], item["object"], references)
                )
            return await self.graphs.prepare(evidence, facts=tuple(facts))

        published = await self.documents.build_snapshot(
            session, with_embeddings=with_embeddings, enricher=prepare if with_graph else None
        )
        return {**self.catalog(session), "loaded_snapshot_id": published["id"]}

    async def search(
        self,
        session: dict,
        query: str,
        *,
        method: str = "lexical",
        snapshot_id: str | None = None,
        limit: int = 5,
        provider: ModelProvider | None = None,
    ) -> tuple[Evidence, ...]:
        snapshot = self.snapshot(session, snapshot_id)
        available = self.methods(session, snapshot)
        if method not in available:
            raise DomainError(
                "RETRIEVAL_UNAVAILABLE",
                "当前索引或服务没有启用所选检索方式。",
                409,
                {"method": method, "available_methods": list(available)},
            )
        if method in DOCUMENT_METHODS:
            return await self.documents.search(
                session, query, method=method, snapshot_id=snapshot["id"], limit=limit
            )
        evidence = self.documents.evidence(session, snapshot["id"])
        return await self.graphs.search(
            query, evidence, snapshot["artifacts"], method=method, limit=limit, provider=provider
        )

    async def answer(
        self, session: dict, question: str, trace: Trace, request: ResearchRequest
    ) -> dict:
        if self.provider is None:
            raise DomainError("SERVICE_NOT_CONFIGURED", "研究服务尚未配置语言模型。", 503)
        if self.store.run_lease_seconds <= self.research_config.timeout_seconds:
            raise DomainError(
                "RESEARCH_LEASE_TOO_SHORT", "研究任务超时配置必须短于会话运行租约。", 503
            )
        snapshot = self.snapshot(session, request.snapshot_id)
        available = tuple(
            method
            for method in self.methods(session, snapshot)
            if method in self.research_config.available_methods and method not in MICROSOFT_METHODS
        )
        if not available:
            raise DomainError(
                "RESEARCH_METHOD_UNAVAILABLE", "当前索引没有可用于研究的检索方式。", 409
            )
        config = replace(self.research_config, mode=request.mode, available_methods=available)
        source_evidence = {
            item.source_id: item for item in self.documents.evidence(session, snapshot["id"])
        }

        async def retrieve(
            query: str, method: str, dependencies: tuple[Evidence, ...], model: ModelProvider
        ) -> tuple[Evidence, ...]:
            if any(
                item.tenant_id != session["tenant_id"] or item.snapshot_id != snapshot["id"]
                for item in dependencies
            ):
                raise DomainError("EVIDENCE_SCOPE", "任务依赖包含其他商店或版本的证据。", 500)
            return await self.search(
                session, query, method=method, snapshot_id=snapshot["id"], provider=model
            )

        history = [
            {"role": role, "content": turn[field]}
            for turn in self.store.list_turns(session)[-self.history_limit :]
            for role, field in (("user", "message"), ("assistant", "answer"))
        ]
        result = await ResearchOrchestrator(self.provider, retrieve, config).answer(
            question, history=history, trace=trace
        )
        # Rebind public citations to immutable stored sources before persisting the turn.
        for citation in result["citations"]:
            original = source_evidence.get(citation["source_id"])
            if original is None or citation["quote"] not in original.text:
                raise DomainError("RESEARCH_SOURCE_MISMATCH", "研究引用未能对应原始资料。", 500)
            if citation["source_sha256"] != original.source_sha256:
                raise DomainError("RESEARCH_SOURCE_MISMATCH", "研究引用的原件指纹发生变化。", 500)
        return {
            **result,
            "research": {
                **result["research"],
                "snapshot_id": snapshot["id"],
                "methods": list(available),
            },
        }
