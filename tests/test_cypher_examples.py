"""Semantic example retrieval is wired into the real service assembly."""

import asyncio
import json

import pytest

from tracedesk.bootstrap import create_services
from tracedesk.errors import DomainError
from tracedesk.evidence import Evidence, GraphFact
from tracedesk.graphs import CypherExample, GraphConfig, Text2Cypher
from tracedesk.graphs.examples import SemanticCypherExamples, load_cypher_examples
from tracedesk.graphs.projection import build_projection
from tracedesk.knowledge import KnowledgeConfig
from tracedesk.provider import ModelReply, ToolCall
from tracedesk.settings import Settings

QUERY = 'MATCH p=(a:Entity {name: "Alpha"})-[:USES*1..1]->(b:Entity) RETURN p LIMIT 3'
EXAMPLES = (
    CypherExample("设备售后", QUERY),
    CypherExample("请求校验规则", QUERY),
    CypherExample("关联凭据", QUERY),
)


class VectorFixture:
    """Intentional dense vectors distinguish ranking from lexical overlap."""

    fingerprint = "semantic-fixture-v1"
    dimensions = 2

    def __init__(self):
        self.calls = []

    async def embed(self, texts, *, kind):
        self.calls.append((kind, texts))
        vectors = {"设备售后": (0.0, 2.0), "请求校验规则": (3.0, 0.0), "关联凭据": (-1.0, 0.0)}
        if kind == "query":
            vectors = {"设备售后": (5.0, 0.0), "其他需求": (0.0, 4.0)}
        return tuple(vectors[text] for text in texts)


class NoModelCalls:
    async def complete(self, messages, tools):
        raise AssertionError("This check must not call a chat model.")


class NoDatabaseCalls:
    async def execute(self, *args):
        raise AssertionError("This check must not query a database.")


def test_bootstrap_installs_semantic_cypher_examples(tmp_path):
    services = create_services(Settings(database_path=tmp_path / "assembly.db"))
    try:
        assert services.graphs.examples is not None
    finally:
        asyncio.run(services.close())


def test_dense_similarity_controls_order_and_document_vectors_are_cached():
    model = VectorFixture()
    retriever = SemanticCypherExamples(
        lambda: model,
        KnowledgeConfig(embedding_batch_size=2),
        examples=EXAMPLES,
    )
    first = asyncio.run(retriever.retrieve("设备售后", limit=3))
    second = asyncio.run(retriever.retrieve("其他需求", limit=1))
    assert first == (EXAMPLES[1], EXAMPLES[0], EXAMPLES[2])
    assert second == (EXAMPLES[0],)
    assert model.calls == [
        ("document", ("设备售后", "请求校验规则")),
        ("document", ("关联凭据",)),
        ("query", ("设备售后",)),
        ("query", ("其他需求",)),
    ]


@pytest.mark.parametrize("stage", ["document", "query"])
def test_example_dimension_errors_are_rejected_by_the_shared_vector_validator(stage):
    class WrongDimensions(VectorFixture):
        async def embed(self, texts, *, kind):
            if kind == stage:
                return tuple((1.0, 0.0, 0.0) for _ in texts)
            return await super().embed(texts, kind=kind)

    model = WrongDimensions()
    retriever = SemanticCypherExamples(lambda: model, KnowledgeConfig(), examples=EXAMPLES)
    with pytest.raises(DomainError) as failure:
        asyncio.run(retriever.retrieve("设备售后", limit=3))
    assert failure.value.code == "EMBEDDING_DIMENSIONS"


def test_failed_example_index_is_not_reused_as_successful_or_replaced_by_lexical():
    class Unavailable(VectorFixture):
        fails = True

        async def embed(self, texts, *, kind):
            if self.fails:
                self.calls.append((kind, texts))
                raise DomainError("EMBEDDING_UNAVAILABLE", "fixture service failure", 502)
            return await super().embed(texts, kind=kind)

    model = Unavailable()
    retriever = SemanticCypherExamples(lambda: model, KnowledgeConfig(), examples=EXAMPLES)
    with pytest.raises(DomainError) as failure:
        asyncio.run(retriever.retrieve("设备售后", limit=1))
    assert failure.value.code == "EMBEDDING_UNAVAILABLE"
    model.fails = False
    assert asyncio.run(retriever.retrieve("设备售后", limit=1)) == (EXAMPLES[1],)
    assert [kind for kind, _ in model.calls] == ["document", "document", "query"]


def test_model_fingerprint_change_cannot_mix_cached_example_vectors():
    model = VectorFixture()
    retriever = SemanticCypherExamples(lambda: model, KnowledgeConfig(), examples=EXAMPLES)
    asyncio.run(retriever.retrieve("设备售后", limit=1))
    before = len(model.calls)
    model.fingerprint = "different-model"
    with pytest.raises(DomainError) as readiness:
        retriever.require_ready()
    with pytest.raises(DomainError) as query:
        asyncio.run(retriever.retrieve("设备售后", limit=1))
    assert readiness.value.code == query.value.code == "cypher_examples_model_mismatch"
    assert len(model.calls) == before


def test_model_identity_change_during_index_creation_fails_before_query_embedding():
    class Changed(VectorFixture):
        async def embed(self, texts, *, kind):
            result = await super().embed(texts, kind=kind)
            self.fingerprint = "changed-during-request"
            return result

    model = Changed()
    retriever = SemanticCypherExamples(lambda: model, KnowledgeConfig(), examples=EXAMPLES)
    with pytest.raises(DomainError) as failure:
        asyncio.run(retriever.retrieve("设备售后", limit=1))
    assert failure.value.code == "cypher_examples_model_mismatch"
    assert [kind for kind, _ in model.calls] == ["document"]


def test_example_embedding_timeout_cancels_the_request():
    async def run():
        stopped = asyncio.Event()

        class Blocking(VectorFixture):
            async def embed(self, texts, *, kind):
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()

        model = Blocking()
        retriever = SemanticCypherExamples(
            lambda: model,
            KnowledgeConfig(embedding_timeout_seconds=0.05),
            examples=EXAMPLES,
        )
        with pytest.raises(DomainError) as failure:
            await retriever.retrieve("设备售后", limit=1)
        assert failure.value.code == "EMBEDDING_TIMEOUT"
        assert stopped.is_set()

    asyncio.run(run())


@pytest.mark.parametrize("limit", [0, 4, True])
def test_example_request_budget_fails_before_embedding(limit):
    model = VectorFixture()
    retriever = SemanticCypherExamples(lambda: model, KnowledgeConfig(), examples=EXAMPLES)
    with pytest.raises(DomainError) as failure:
        asyncio.run(retriever.retrieve("设备售后", limit=limit))
    assert failure.value.code == "cypher_examples_budget"
    assert model.calls == []


def test_semantically_retrieved_examples_reach_the_actual_cypher_prompt():
    class Capture:
        messages = None

        async def complete(self, messages, tools):
            self.messages = messages
            return ModelReply(
                (ToolCall("call", "submit_cypher", json.dumps({"cypher": QUERY})),), {}
            )

    model = VectorFixture()
    retriever = SemanticCypherExamples(lambda: model, KnowledgeConfig(), examples=EXAMPLES)
    provider = Capture()
    evidence = (Evidence("s", "t", "v", "d", 1, "说明", "Alpha USES Beta", "a" * 64),)
    projection = build_projection(
        evidence, (GraphFact("Alpha", "USES", "Beta", ("s",)),), GraphConfig()
    )
    plan = asyncio.run(
        Text2Cypher(provider, GraphConfig(), examples=retriever).plan("设备售后", projection)
    )
    assert plan.start == "Alpha"
    prompt = provider.messages[1]["content"]
    assert "synthetic" in prompt and "not evidence" in prompt
    assert prompt.index(EXAMPLES[1].question) < prompt.index(EXAMPLES[0].question)


def test_bootstrap_shares_the_document_embedder_and_keeps_base_graph_available(
    tmp_path, monkeypatch
):
    created = []

    class CountOnly(VectorFixture):
        async def embed(self, texts, *, kind):
            self.calls.append((kind, texts))
            return tuple((1.0, 0.0) for _ in texts)

    def create(config):
        model = CountOnly()
        created.append(model)
        return model

    monkeypatch.setattr("tracedesk.knowledge.service.create_embedder", create)
    config = KnowledgeConfig(
        embedding_provider="openai",
        embedding_base_url="http://127.0.0.1:1/v1",
        embedding_model="fixture-model",
        embedding_dimensions=2,
    )
    services = create_services(Settings(database_path=tmp_path / "assembly.db", knowledge=config))
    try:
        assert created == []
        services.graphs.examples.require_ready()
        assert len(created) == 1 and services.documents._embedder() is created[0]
        assert len(asyncio.run(services.graphs.examples.retrieve("设备售后", limit=3))) == 3
        assert len(created) == 1
        evidence = (Evidence("s", "t", "v", "d", 1, "说明", "Alpha USES Beta", "a" * 64),)
        artifact = asyncio.run(
            services.graphs.prepare(evidence, facts=(GraphFact("Alpha", "USES", "Beta", ("s",)),))
        )
        assert asyncio.run(services.graphs.search("Alpha", evidence, artifact))[0].source_id == "s"
        assert services.documents.method_status({"with_embeddings": False})["lexical"]["available"]
    finally:
        asyncio.run(services.close())


def test_packaged_examples_are_self_authored_and_cover_supported_query_shapes():
    examples = load_cypher_examples()
    assert len(examples) == 8 and len({item.question for item in examples}) == 8
    assert all("示例" in example.cypher for example in examples)
    assert any("<-" in example.cypher for example in examples)
    assert any("*1..3" in example.cypher for example in examples)


def test_default_base_local_graph_does_not_initialize_any_embedding_model(tmp_path, monkeypatch):
    def forbidden(config):
        raise AssertionError("The default local graph must not initialize embeddings.")

    monkeypatch.setattr("tracedesk.knowledge.service.create_embedder", forbidden)
    services = create_services(Settings(database_path=tmp_path / "base.db"))
    try:
        evidence = (Evidence("s", "t", "v", "d", 1, "说明", "Alpha USES Beta", "a" * 64),)
        artifact = asyncio.run(
            services.graphs.prepare(evidence, facts=(GraphFact("Alpha", "USES", "Beta", ("s",)),))
        )
        assert services.graphs.method_status(evidence, artifact)["graph"]["available"]
        assert asyncio.run(services.graphs.search("Alpha", evidence, artifact))[0].source_id == "s"
        assert services.documents.method_status({"with_embeddings": False})["lexical"]["available"]
    finally:
        asyncio.run(services.close())


def test_unexpected_example_embedding_failure_is_not_hidden():
    class Broken(VectorFixture):
        async def embed(self, texts, *, kind):
            raise RuntimeError("unexpected embedding failure")

    retriever = SemanticCypherExamples(lambda: Broken(), KnowledgeConfig(), examples=EXAMPLES)
    with pytest.raises(RuntimeError, match="unexpected embedding failure"):
        asyncio.run(retriever.retrieve("设备售后", limit=1))


def test_empty_example_corpus_is_not_replaced_with_another_retrieval_method():
    model = VectorFixture()
    with pytest.raises(DomainError) as failure:
        SemanticCypherExamples(lambda: model, KnowledgeConfig(), examples=())
    assert failure.value.code == "cypher_examples_invalid"
    assert model.calls == []


def test_bootstrap_text2cypher_refuses_missing_embedding_configuration(tmp_path):
    services = create_services(
        Settings(database_path=tmp_path / "assembly.db", graphs=GraphConfig(backend="neo4j")),
        provider=NoModelCalls(),
    )
    services.graphs.neo4j = NoDatabaseCalls()
    evidence = (
        Evidence("s", "tenant", "snapshot", "document", 1, "说明", "Alpha 使用 Beta。", "a" * 64),
    )
    projection = build_projection(
        evidence, (GraphFact("Alpha", "使用", "Beta", ("s",)),), GraphConfig()
    )
    artifact = projection.artifact(extraction="explicit", backend="neo4j")
    artifact["neo4j"] = {"state": "ready", "projection_digest": projection.projection_digest}
    try:
        status = services.graphs.method_status(evidence, {"graph": artifact})
        assert status["text2cypher"]["available"] is False
        assert status["text2cypher"]["error"]["code"] == "EMBEDDING_NOT_CONFIGURED"
    finally:
        asyncio.run(services.close())
