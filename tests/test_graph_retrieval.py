import asyncio
from dataclasses import replace

import pytest

from tracedesk.errors import DomainError
from tracedesk.evidence import Evidence, GraphFact
from tracedesk.graphs import GraphConfig, GraphService
from tracedesk.graphs.projection import build_projection
from tracedesk.provider import ModelReply, ToolCall


def evidence() -> tuple[Evidence, ...]:
    return (
        Evidence(
            "source-a",
            "tenant-a",
            "snapshot-a",
            "document-a",
            1,
            "设备",
            "Alpha 使用 Beta。",
            "a" * 64,
        ),
        Evidence(
            "source-b",
            "tenant-a",
            "snapshot-a",
            "document-b",
            2,
            "保障",
            "Beta 遵循 Gamma。",
            "b" * 64,
            page=2,
        ),
    )


def facts() -> tuple[GraphFact, ...]:
    return (
        GraphFact("Alpha", "使用", "Beta", ("source-a",)),
        GraphFact("Beta", "遵循", "Gamma", ("source-b",)),
    )


def test_graph_paths_keep_direction_multihop_and_original_evidence():
    service = GraphService()
    source = evidence()
    artifact = asyncio.run(service.prepare(source, facts=facts()))
    result = asyncio.run(service.search("Alpha 的关联保障", source, artifact))
    assert [item.source_id for item in result] == ["source-a", "source-b"]
    assert result[1].graph_path == ("Alpha", "-[使用]->", "Beta", "-[遵循]->", "Gamma")
    assert result[1].text == source[1].text
    assert result[1].source_sha256 == "b" * 64
    assert result[1].document_version == 2 and result[1].page == 2
    assert asyncio.run(service.search("Gamma 的后继", source, artifact)) == ()
    assert [item.source_id for item in asyncio.run(service.search("Beta", source, artifact))] == [
        "source-b"
    ]


@pytest.mark.parametrize(
    "field,value", [("tenant_id", "other-tenant"), ("snapshot_id", "other-snapshot")]
)
def test_cross_scope_evidence_rejected_before_enrichment(field, value):
    source = evidence()
    mixed = (source[0], replace(source[1], **{field: value}))
    with pytest.raises(DomainError, match="不能混用") as error:
        asyncio.run(GraphService().prepare(mixed, facts=facts()))
    assert error.value.code == "graph_scope_mismatch"


@pytest.mark.parametrize(
    "fact,code",
    [
        (GraphFact("Alpha", "使用", "Beta", ("missing",)), "graph_source_unknown"),
        (GraphFact("Alpha", "使用", "Imaginary", ("source-a",)), "graph_fact_unsupported"),
        (GraphFact("Alpha", "使用", "Beta", ("source-a", "source-b")), "graph_fact_unsupported"),
    ],
)
def test_every_fact_source_must_contain_both_entities(fact, code):
    with pytest.raises(DomainError) as error:
        asyncio.run(GraphService().prepare(evidence(), facts=(fact,)))
    assert error.value.code == code


def test_entity_occurrence_does_not_accept_a_latin_word_prefix():
    source = (replace(evidence()[0], text="AlphaBeta 使用 Gamma。"),)
    with pytest.raises(DomainError) as error:
        asyncio.run(
            GraphService().prepare(
                source, facts=(GraphFact("Alpha", "使用", "Gamma", ("source-a",)),)
            )
        )
    assert error.value.code == "graph_fact_unsupported"


def test_same_relation_merges_sources_deterministically():
    source = evidence()
    duplicate_source = replace(source[0], source_id="source-c", document_id="document-c")
    projection = build_projection(
        (*source, duplicate_source),
        (
            facts()[0],
            GraphFact("Alpha", "使用", "Beta", ("source-c", "source-a")),
        ),
        GraphConfig(),
    )
    assert projection.facts == (GraphFact("Alpha", "使用", "Beta", ("source-a", "source-c")),)


def test_empty_explicit_graph_is_reported_and_not_silently_lexical():
    service = GraphService()
    artifact = asyncio.run(service.prepare(evidence()))
    graph = artifact["graph"]
    assert (graph["extraction"], graph["state"], graph["fact_count"]) == ("explicit", "empty", 0)
    with pytest.raises(DomainError) as error:
        asyncio.run(service.search("Alpha", evidence(), artifact))
    assert error.value.code == "graph_empty"


def test_artifact_cannot_be_reused_for_another_scope_or_changed_text():
    service = GraphService()
    artifact = asyncio.run(service.prepare(evidence(), facts=facts()))
    for changed in (
        tuple(replace(item, tenant_id="other") for item in evidence()),
        tuple(replace(item, snapshot_id="other") for item in evidence()),
        (replace(evidence()[0], text="Alpha 使用 Beta。内容改变。"), evidence()[1]),
    ):
        with pytest.raises(DomainError) as error:
            asyncio.run(service.search("Alpha", changed, artifact))
        assert error.value.code == "graph_scope_mismatch"


def test_graph_expansion_budget_fails_visibly():
    service = GraphService(GraphConfig(max_expansions=1))
    artifact = asyncio.run(service.prepare(evidence(), facts=facts()))
    with pytest.raises(DomainError) as error:
        asyncio.run(service.search("Alpha", evidence(), artifact))
    assert error.value.code == "graph_query_budget"


def test_model_extraction_is_an_actual_provider_call_with_source_validation():
    class Provider:
        calls = 0

        async def complete(self, messages, tools):
            self.calls += 1
            assert tools[0]["function"]["name"] == "record_graph_facts"
            assert "source-a" in messages[1]["content"]
            args = '{"facts":[{"subject":"Alpha","predicate":"使用","object":"Beta","source_ids":["source-a"]}]}'
            return ModelReply(
                (ToolCall("extract", "record_graph_facts", args),), {"role": "assistant"}
            )

    provider = Provider()
    service = GraphService(GraphConfig(extraction="model"), provider=provider)
    artifact = asyncio.run(service.prepare(evidence()))
    assert provider.calls == 1
    assert artifact["graph"]["extraction"] == "model"
    assert artifact["graph"]["fact_count"] == 1
    explicit = asyncio.run(service.prepare(evidence(), facts=facts()))
    assert explicit["graph"]["extraction"] == "explicit"
    assert provider.calls == 1


def test_missing_model_and_disabled_graphrag_are_explicit():
    with pytest.raises(DomainError) as error:
        asyncio.run(GraphService(GraphConfig(extraction="model")).prepare(evidence()))
    assert error.value.code == "graph_model_unavailable"
    service = GraphService()
    artifact = asyncio.run(service.prepare(evidence(), facts=facts()))
    with pytest.raises(DomainError) as error:
        asyncio.run(service.search("Alpha", evidence(), artifact, method="graphrag_local"))
    assert error.value.code == "graphrag_unavailable"


def test_config_explicit_environment_and_secret_repr():
    config = GraphConfig.from_env(
        {
            "TRACEDESK_GRAPH_MAX_HOPS": "2",
            "TRACEDESK_GRAPHRAG_ENABLED": "false",
            "TRACEDESK_NEO4J_READER_PASSWORD": "private-test-value",
        }
    )
    assert config.max_hops == 2 and not config.graphrag_enabled
    assert "private-test-value" not in repr(config)
    with pytest.raises(ValueError):
        GraphConfig.from_env({"TRACEDESK_GRAPHRAG_ENABLED": "probably"})
    with pytest.raises(ValueError):
        GraphConfig(max_hops=99)
