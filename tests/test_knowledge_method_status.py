"""Capability reports and semantic queries share one local readiness decision."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tracedesk.errors import DomainError
from tracedesk.evidence import Evidence
from tracedesk.knowledge import KnowledgeConfig, KnowledgeService
from tracedesk.knowledge.embeddings import OpenAIEmbedder

SOURCE = Evidence(
    "source", "tenant", "snapshot", "document", 1, "说明", "退货需要保留包装。", "a" * 64
)


class CountingEmbedder:
    fingerprint = "fixture-model"
    dimensions = 2

    def __init__(self):
        self.calls = 0

    async def embed(self, texts, *, kind):
        self.calls += 1
        return ((1.0, 0.0),)


def service_for(snapshot, *, config=None, embedder=None):
    service = KnowledgeService(None, config or KnowledgeConfig(), embedder=embedder)
    service.repository = SimpleNamespace(
        read_snapshot=lambda session, identifier: (snapshot, (SOURCE,)),
        vectors=lambda session, identifier, dimensions: {SOURCE.source_id: (1.0, 0.0)},
    )
    return service


@pytest.mark.parametrize(
    "case,code",
    [
        ("unbuilt", "SEMANTIC_NOT_AVAILABLE"),
        ("disabled", "EMBEDDING_NOT_CONFIGURED"),
        ("different_model", "EMBEDDING_MODEL_MISMATCH"),
    ],
)
def test_semantic_status_matches_query_error_without_calling_an_embedder(case, code):
    embedder = CountingEmbedder() if case != "disabled" else None
    snapshot = {
        "id": "snapshot",
        "with_embeddings": case != "unbuilt",
        "embedding_fingerprint": "previous-model",
    }
    service = service_for(snapshot, embedder=embedder)
    statuses = service.method_status(snapshot)
    assert statuses["lexical"] == {"available": True, "error": None}
    assert asyncio.run(service.search({}, "退货", method="lexical"))
    for method in ("semantic", "hybrid"):
        with pytest.raises(DomainError) as failure:
            asyncio.run(service.search({}, "退货", method=method))
        assert statuses[method] == {"available": False, "error": failure.value.as_dict()}
        assert failure.value.code == code
    if embedder is not None:
        assert embedder.calls == 0


def test_changed_openai_model_is_unavailable_before_any_http_request():
    original = KnowledgeConfig(
        embedding_provider="openai",
        embedding_base_url="http://127.0.0.1:1/v1",
        embedding_model="original-model",
        embedding_dimensions=2,
    )
    snapshot = {
        "id": "snapshot",
        "with_embeddings": True,
        "embedding_fingerprint": OpenAIEmbedder(original).fingerprint,
    }
    service = service_for(snapshot, config=replace(original, embedding_model="different-model"))
    statuses = service.method_status(snapshot)
    assert statuses["semantic"]["error"]["code"] == "EMBEDDING_MODEL_MISMATCH"
    with pytest.raises(DomainError) as failure:
        asyncio.run(service.search({}, "退货", method="semantic"))
    assert failure.value.as_dict() == statuses["semantic"]["error"]


def test_missing_onnx_files_are_a_visible_capability_error(tmp_path):
    config = KnowledgeConfig(
        embedding_provider="onnx",
        embedding_model="configured-model",
        embedding_dimensions=2,
        onnx_model_path=str(tmp_path / "missing.onnx"),
        tokenizer_path=str(tmp_path / "missing-tokenizer.json"),
    )
    snapshot = {"id": "snapshot", "with_embeddings": True, "embedding_fingerprint": "saved"}
    service = service_for(snapshot, config=config)
    statuses = service.method_status(snapshot)
    assert statuses["semantic"]["error"]["code"] == "EMBEDDING_NOT_CONFIGURED"
    with pytest.raises(DomainError) as failure:
        asyncio.run(service.search({}, "退货", method="semantic"))
    assert failure.value.as_dict() == statuses["semantic"]["error"]


def test_status_checks_reuse_the_owned_embedder_without_running_inference(monkeypatch):
    created = []

    def create(config):
        embedder = CountingEmbedder()
        created.append(embedder)
        return embedder

    monkeypatch.setattr("tracedesk.knowledge.service.create_embedder", create)
    snapshot = {
        "id": "snapshot",
        "with_embeddings": True,
        "embedding_fingerprint": CountingEmbedder.fingerprint,
    }
    service = service_for(snapshot)
    for _ in range(3):
        assert all(item["available"] for item in service.method_status(snapshot).values())
    assert len(created) == 1 and created[0].calls == 0
    result = asyncio.run(service.search({}, "退货", method="semantic"))
    assert result[0].source_id == SOURCE.source_id
    assert len(created) == 1 and created[0].calls == 1


def test_unexpected_readiness_errors_are_not_converted_to_unavailable(monkeypatch):
    def broken(config):
        raise RuntimeError("unexpected model initialization failure")

    monkeypatch.setattr("tracedesk.knowledge.service.create_embedder", broken)
    snapshot = {"id": "snapshot", "with_embeddings": True, "embedding_fingerprint": "saved"}
    service = service_for(snapshot)
    with pytest.raises(RuntimeError, match="unexpected model initialization"):
        service.method_status(snapshot)
    with pytest.raises(RuntimeError, match="unexpected model initialization"):
        asyncio.run(service.search({}, "退货", method="semantic"))
