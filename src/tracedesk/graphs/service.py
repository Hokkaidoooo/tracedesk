"""Graph capabilities enrich snapshots but never publish their own truth pointer."""

import asyncio
from contextlib import AsyncExitStack
from dataclasses import replace
from threading import Lock
from typing import Any, Protocol

from ..errors import DomainError
from ..evidence import Evidence, GraphFact
from ..provider import ModelProvider
from ..resources import close_on_exit
from .config import GraphConfig
from .cypher import CypherPlan
from .extraction import extract_facts
from .neo4j import Neo4jProjection
from .paths import GraphPath, path_evidence, query_paths
from .projection import Projection, build_projection, occurs, restore_projection
from .text2cypher import SemanticExampleRetriever, Text2Cypher, require_semantic_examples


class GraphRagRuntime(Protocol):
    async def prepare(self, evidence: tuple[Evidence, ...]) -> dict[str, Any]: ...

    async def search(
        self,
        query: str,
        evidence: tuple[Evidence, ...],
        artifact: dict[str, Any],
        *,
        method: str,
        limit: int,
    ) -> tuple[Evidence, ...]: ...

    def require_ready(
        self,
        evidence: tuple[Evidence, ...],
        artifact: dict[str, Any],
    ) -> Any: ...

    async def close(self) -> None: ...


class GraphService:
    def __init__(
        self,
        config: GraphConfig | None = None,
        *,
        provider: ModelProvider | None = None,
        neo4j: Neo4jProjection | None = None,
        graphrag: GraphRagRuntime | None = None,
        examples: SemanticExampleRetriever | None = None,
    ) -> None:
        self.config = config or GraphConfig()
        self.provider = provider
        self.neo4j = neo4j
        self.graphrag = graphrag
        self.examples = examples
        self._owned_neo4j: Neo4jProjection | None = None
        self._owned_graphrag: GraphRagRuntime | None = None
        self._resource_lock = Lock()

    def method_status(
        self,
        evidence: tuple[Evidence, ...],
        artifacts: dict[str, Any],
    ) -> dict[str, dict]:
        methods = ("graph", "text2cypher", "graphrag_local", "graphrag_global")
        try:
            projection, artifact = restore_projection(evidence, artifacts, self.config)
        except DomainError as error:
            return {method: {"available": False, "error": error.as_dict()} for method in methods}
        result = {}
        for method in methods:
            try:
                self._require_method(projection, artifact, method, self.provider)
            except DomainError as error:
                result[method] = {"available": False, "error": error.as_dict()}
            else:
                result[method] = {"available": True, "error": None}
        return result

    async def prepare(
        self,
        evidence: tuple[Evidence, ...],
        *,
        facts: tuple[GraphFact, ...] | None = None,
    ) -> dict[str, Any]:
        # Check scope before either an external model call or a backend write.
        build_projection(evidence, (), self.config)
        extraction = "explicit" if facts is not None else self.config.extraction
        if facts is None:
            facts = (
                await extract_facts(evidence, self.provider, self.config)
                if extraction == "model"
                else ()
            )
        projection = build_projection(evidence, facts, self.config)
        artifact = projection.artifact(extraction=extraction, backend=self.config.backend)
        if self.config.backend == "neo4j":
            artifact["neo4j"] = await self._neo4j().import_projection(projection)
        if self.config.graphrag_enabled:
            artifact["graphrag"] = await self._graphrag().prepare(evidence)
        return {"graph": artifact}

    async def search(
        self,
        query: str,
        evidence: tuple[Evidence, ...],
        artifacts: dict[str, Any],
        *,
        method: str = "graph",
        limit: int = 5,
        provider: ModelProvider | None = None,
    ) -> tuple[Evidence, ...]:
        if not isinstance(query, str) or not query.strip() or len(query) > 8000:
            raise DomainError("graph_query_invalid", "图检索问题为空或过长。")
        if type(limit) is not int or not 1 <= limit <= self.config.max_rows:
            raise DomainError("graph_query_budget", "图检索返回数量超过预算。")
        if method not in {"graph", "neo4j", "text2cypher", "graphrag_local", "graphrag_global"}:
            raise DomainError("graph_method_invalid", "未知的图检索方式。")
        projection, artifact = restore_projection(evidence, artifacts, self.config)
        selected_provider = provider if provider is not None else self.provider
        self._require_method(projection, artifact, method, selected_provider)
        if method in {"graphrag_local", "graphrag_global"}:
            result = await self._graphrag().search(
                query, evidence, artifact["graphrag"], method=method, limit=limit
            )
            _validate_returned_evidence(result, evidence, limit)
            return result
        if method == "text2cypher":
            generator = Text2Cypher(selected_provider, self.config, examples=self.examples)
            plan = await generator.plan(query, projection)
            paths = await self._neo4j().execute(
                replace(plan, limit=min(plan.limit, limit)), projection, self.config
            )
        elif self.config.backend == "neo4j" or method == "neo4j":
            try:
                paths = await asyncio.wait_for(
                    self._database_paths(query, projection, limit),
                    timeout=self.config.query_timeout_seconds + 1,
                )
            except asyncio.TimeoutError as exc:
                raise DomainError("graph_query_timeout", "Neo4j 检索超过总时间预算。", 504) from exc
        else:
            paths = query_paths(query, projection, self.config)
        return path_evidence(paths, projection, method=method, limit=limit)

    def _require_method(
        self,
        projection: Projection,
        artifact: dict[str, Any],
        method: str,
        provider: ModelProvider | None,
    ) -> None:
        if method in {"graphrag_local", "graphrag_global"}:
            if not self.config.graphrag_enabled or not isinstance(artifact.get("graphrag"), dict):
                raise DomainError(
                    "graphrag_unavailable", "此快照未构建 Microsoft GraphRAG 索引。", 503
                )
            self._graphrag().require_ready(projection.evidence, artifact["graphrag"])
            return
        if not projection.facts:
            raise DomainError(
                "graph_empty",
                "此快照没有经来源核验的图关系，无法提供图证据。",
                409,
                {"extraction": artifact["extraction"], "fact_count": 0},
            )
        if artifact["backend"] != self.config.backend:
            raise DomainError("graph_backend_mismatch", "图存储配置已改变，需要构建新快照。", 409)
        if method == "text2cypher" or self.config.backend == "neo4j" or method == "neo4j":
            self._require_neo4j_artifact(artifact, projection)
            if method == "text2cypher" and provider is None:
                raise DomainError("graph_model_unavailable", "Text2Cypher 尚未配置模型。", 503)
            if method == "text2cypher":
                require_semantic_examples(self.examples)
            self._neo4j()

    async def _database_paths(
        self, query: str, projection: Projection, limit: int
    ) -> tuple[GraphPath, ...]:
        query_paths(query, projection, self.config)
        starts = tuple(node for node in projection.nodes if occurs(node, query))
        if len(starts) > self.config.max_rows:
            raise DomainError("graph_query_budget", "查询命中起点过多，超过数据库调用预算。", 422)
        paths: list[GraphPath] = []
        for start in starts:
            plan = CypherPlan(
                start, projection.predicates, max_hops=self.config.max_hops, limit=limit
            )
            paths.extend(await self._neo4j().execute(plan, projection, self.config))
        return tuple(paths)

    def _neo4j(self) -> Neo4jProjection:
        with self._resource_lock:
            if self.neo4j is None:
                self.neo4j = Neo4jProjection.from_config(self.config)
                self._owned_neo4j = self.neo4j
            return self.neo4j

    def _graphrag(self) -> GraphRagRuntime:
        with self._resource_lock:
            if self.graphrag is None:
                from .microsoft import MicrosoftGraphRag

                self.graphrag = MicrosoftGraphRag(self.config)
                self._owned_graphrag = self.graphrag
            return self.graphrag

    def _require_neo4j_artifact(self, artifact: dict[str, Any], projection: Projection) -> None:
        neo4j = artifact.get("neo4j")
        if (
            artifact["backend"] != "neo4j"
            or not isinstance(neo4j, dict)
            or neo4j.get("state") != "ready"
            or neo4j.get("projection_digest") != projection.projection_digest
        ):
            raise DomainError("neo4j_unavailable", "此快照没有可用的 Neo4j 投影。", 503)

    async def close(self) -> None:
        async with AsyncExitStack() as closing:
            if self._owned_graphrag is not None:
                await closing.enter_async_context(close_on_exit(self._owned_graphrag.close))
            if self._owned_neo4j is not None:
                await closing.enter_async_context(close_on_exit(self._owned_neo4j.close))


def _validate_returned_evidence(
    result: tuple[Evidence, ...],
    expected: tuple[Evidence, ...],
    limit: int,
) -> None:
    known = {item.source_id: item for item in expected}
    if len(result) > limit or len({item.source_id for item in result}) != len(result):
        raise DomainError("graphrag_result_invalid", "GraphRAG 返回证据数量或唯一性无效。", 502)
    for item in result:
        canonical = known.get(item.source_id)
        if (
            canonical is None
            or replace(
                item,
                score=canonical.score,
                method=canonical.method,
                graph_path=canonical.graph_path,
            )
            != canonical
        ):
            raise DomainError(
                "graphrag_result_invalid", "GraphRAG 返回证据不属于目标快照原文。", 502
            )
