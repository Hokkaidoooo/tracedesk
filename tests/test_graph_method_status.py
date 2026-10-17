"""Graph readiness is inspectable before a model chooses a retrieval method."""

import asyncio
from dataclasses import replace

import pytest

from tracedesk.errors import DomainError
from tracedesk.evidence import Evidence, GraphFact
from tracedesk.graphs import CypherExample, GraphConfig, GraphService
from tracedesk.graphs.paths import GraphPath
from tracedesk.graphs.projection import build_projection
from tracedesk.provider import ModelReply, ToolCall

SOURCE = Evidence(
    "source", "tenant", "snapshot", "document", 1, "说明", "Alpha 使用 Beta。", "a" * 64
)
FACT = GraphFact("Alpha", "使用", "Beta", (SOURCE.source_id,))


def artifacts(*, backend="local", facts=(FACT,)):
    projection = build_projection((SOURCE,), facts, GraphConfig())
    artifact = projection.artifact(extraction="explicit", backend=backend)
    if backend == "neo4j":
        artifact["neo4j"] = {"state": "ready", "projection_digest": projection.projection_digest}
    return {"graph": artifact}


def test_local_graph_status_matches_an_actual_source_bound_query():
    service = GraphService()
    saved = artifacts()
    statuses = service.method_status((SOURCE,), saved)
    assert statuses["graph"] == {"available": True, "error": None}
    assert all(
        not statuses[method]["available"]
        for method in ("text2cypher", "graphrag_local", "graphrag_global")
    )
    result = asyncio.run(service.search("Alpha", (SOURCE,), saved))
    assert result[0].source_id == SOURCE.source_id and result[0].text == SOURCE.text


@pytest.mark.parametrize(
    "case,code",
    [
        ("backend_changed", "graph_backend_mismatch"),
        ("unbuilt", "graph_artifact_invalid"),
        ("empty", "graph_empty"),
        ("scope_changed", "graph_scope_mismatch"),
        ("neo4j_unconfigured", "neo4j_unavailable"),
    ],
)
def test_graph_status_matches_query_failure_without_network_calls(case, code):
    config = GraphConfig(
        backend="neo4j" if case in {"backend_changed", "neo4j_unconfigured"} else "local"
    )
    service = GraphService(config)
    saved = artifacts(
        backend="neo4j" if case == "neo4j_unconfigured" else "local",
        facts=() if case == "empty" else (FACT,),
    )
    if case == "unbuilt":
        saved = {}
    evidence = (replace(SOURCE, tenant_id="other") if case == "scope_changed" else SOURCE,)
    statuses = service.method_status(evidence, saved)
    with pytest.raises(DomainError) as failure:
        asyncio.run(service.search("Alpha", evidence, saved))
    assert failure.value.code == code
    assert statuses["graph"] == {"available": False, "error": failure.value.as_dict()}


class Reader:
    def __init__(self):
        self.calls = 0

    async def execute(self, plan, projection, config):
        self.calls += 1
        return (GraphPath(("Alpha", "Beta"), (FACT,)),)


class QueryProvider:
    def __init__(self):
        self.calls = 0

    async def complete(self, messages, tools):
        self.calls += 1
        return ModelReply(
            (
                ToolCall(
                    "query",
                    "submit_cypher",
                    '{"cypher":"MATCH p=(a:Entity {name: \\"Alpha\\"})-[:`使用`*1..1]->(b:Entity) RETURN p LIMIT 5"}',
                ),
            ),
            {},
        )


class ExampleFixture:
    async def retrieve(self, query, *, limit):
        return (
            CypherExample(
                "Alpha 的关联",
                'MATCH p=(a:Entity {name: "Alpha"})-[:`使用`*1..1]->(b:Entity) RETURN p LIMIT 5',
            ),
        )


def test_text2cypher_requires_a_model_before_accessing_the_database():
    reader = Reader()
    service = GraphService(GraphConfig(backend="neo4j"), neo4j=reader)
    saved = artifacts(backend="neo4j")
    statuses = service.method_status((SOURCE,), saved)
    assert statuses["graph"]["available"] is True
    assert statuses["text2cypher"]["error"]["code"] == "graph_model_unavailable"
    with pytest.raises(DomainError) as failure:
        asyncio.run(service.search("Alpha", (SOURCE,), saved, method="text2cypher"))
    assert failure.value.as_dict() == statuses["text2cypher"]["error"]
    assert reader.calls == 0


def test_text2cypher_preserves_the_explicit_per_query_provider():
    reader = Reader()
    global_provider, selected_provider = QueryProvider(), QueryProvider()
    service = GraphService(
        GraphConfig(backend="neo4j"),
        neo4j=reader,
        provider=global_provider,
        examples=ExampleFixture(),
    )
    saved = artifacts(backend="neo4j")
    assert service.method_status((SOURCE,), saved)["text2cypher"]["available"] is True
    assert global_provider.calls == selected_provider.calls == reader.calls == 0
    result = asyncio.run(
        service.search("Alpha", (SOURCE,), saved, method="text2cypher", provider=selected_provider)
    )
    assert result[0].source_id == SOURCE.source_id
    assert global_provider.calls == 0 and selected_provider.calls == reader.calls == 1


def test_disabled_graphrag_is_not_instantiated_even_with_a_saved_artifact(monkeypatch):
    def forbidden(config):
        raise AssertionError("Disabled GraphRAG must not be initialized.")

    monkeypatch.setattr("tracedesk.graphs.microsoft.MicrosoftGraphRag", forbidden)
    saved = artifacts()
    saved["graph"]["graphrag"] = {"state": "ready"}
    service = GraphService()
    statuses = service.method_status((SOURCE,), saved)
    for method in ("graphrag_local", "graphrag_global"):
        with pytest.raises(DomainError) as failure:
            asyncio.run(service.search("Alpha", (SOURCE,), saved, method=method))
        assert failure.value.code == "graphrag_unavailable"
        assert statuses[method] == {"available": False, "error": failure.value.as_dict()}


def test_unexpected_graph_initialization_error_is_not_hidden(monkeypatch):
    def broken(config):
        raise RuntimeError("unexpected graph initialization failure")

    monkeypatch.setattr("tracedesk.graphs.service.Neo4jProjection.from_config", broken)
    service = GraphService(GraphConfig(backend="neo4j"))
    saved = artifacts(backend="neo4j")
    with pytest.raises(RuntimeError, match="unexpected graph initialization"):
        service.method_status((SOURCE,), saved)
    with pytest.raises(RuntimeError, match="unexpected graph initialization"):
        asyncio.run(service.search("Alpha", (SOURCE,), saved))
