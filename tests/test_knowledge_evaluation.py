import asyncio
import json
from dataclasses import replace

import pytest

from tracedesk.bootstrap import create_services
from tracedesk.errors import DomainError
from tracedesk.knowledge import KnowledgeConfig
from tracedesk.knowledge_evaluation import _score, evaluate_knowledge, load_cases
from tracedesk.settings import Settings


async def prepare(tmp_path, *, chunk_size=1200):
    services = create_services(
        Settings(
            database_path=tmp_path / "knowledge.db",
            knowledge=KnowledgeConfig(max_chunk_characters=chunk_size),
        )
    )
    created = services.store.create_session("customer-a")
    session = services.store.authenticate(created["token"])
    catalog = await services.workflows.load_demo(session)
    return services, session, catalog["loaded_snapshot_id"]


def test_real_local_ablation_keeps_missing_quote_support_and_unanswerable_retrieval(tmp_path):
    async def scenario():
        services, session, snapshot_id = await prepare(tmp_path, chunk_size=160)
        try:
            before = services.documents.evidence(session, snapshot_id)
            report = await evaluate_knowledge(services.workflows, session, snapshot_id=snapshot_id)
            assert report["status"] == "complete" and report["methods"] == ["lexical", "graph"]
            assert report["dataset"]["case_count"] == 28
            assert report["dataset"]["answerable_count"] == 23
            assert report["dataset"]["unanswerable_count"] == 5
            assert report["models"]["embedding_fingerprint"] is None
            assert report["models"]["graph"]["extraction"] == "explicit"
            assert report["models"]["graph"]["fact_count"] == 6
            assert len(report["snapshot"]["sha256"]) == len(report["corpus"]["sha256"]) == 64
            assert report["execution"]["chat_model_calls"] == 0
            assert services.documents.evidence(session, snapshot_id) == before
            cases = {case["id"]: case for case in report["cases"]}
            table = cases["KH08"]["results"]["graph"]
            assert table["recall_at_3"] == 1.0
            assert table["support_recall_at_3"] == 0.0
            assert "missing_required_quotes" in table["failure_reasons"]
            assert cases["KD28"]["results"]["graph"]["no_answer_false_recall"] is True
            assert report["failures"]
            assert report["metrics"]["graph"]["support_recall_at_3"] < 1.0
            assert report["metrics"]["lexical"]["no_answer_false_recall_rate"] > 0
            json.dumps(report, ensure_ascii=False, allow_nan=False)
        finally:
            await services.close()

    asyncio.run(scenario())


def test_multisource_recall_denominator_and_duplicate_document_gain_use_real_retrieval(tmp_path):
    async def scenario():
        services, session, snapshot_id = await prepare(tmp_path)
        try:
            cases, _ = load_cases()
            case = next(case for case in cases if case.id == "KH05")
            source = await services.workflows.search(
                session, "X100", snapshot_id=snapshot_id, limit=1
            )
            assert source[0].title == "X100 主机产品说明"
            score = _score(case, source)
            assert score["required_document_count"] == 2
            assert score["matched_document_count"] == 1
            assert score["recall_at_3"] == 0.5
            assert score["mrr_at_3"] == 1.0
            duplicate = _score(
                case, (source[0], replace(source[0], source_id="additional-block-in-same-document"))
            )
            assert duplicate["ndcg_at_3"] == score["ndcg_at_3"]
        finally:
            await services.close()

    asyncio.run(scenario())


def test_explicit_unconfigured_method_is_not_dropped(tmp_path):
    async def scenario():
        services, session, _ = await prepare(tmp_path)
        try:
            with pytest.raises(DomainError) as error:
                await evaluate_knowledge(
                    services.workflows, session, methods=("lexical", "semantic")
                )
            assert error.value.code == "EVALUATION_METHOD_UNAVAILABLE"
            assert error.value.details["unavailable"] == ["semantic"]
        finally:
            await services.close()

    asyncio.run(scenario())


def test_fixed_snapshot_does_not_follow_a_new_publication_mid_evaluation(tmp_path, monkeypatch):
    async def scenario():
        services, session, snapshot_id = await prepare(tmp_path)
        original = services.workflows.search
        requested = []

        async def changing_current(session, query, **kwargs):
            requested.append(kwargs["snapshot_id"])
            if len(requested) == 1:
                await services.documents.build_snapshot(session)
            return await original(session, query, **kwargs)

        monkeypatch.setattr(services.workflows, "search", changing_current)
        try:
            report = await evaluate_knowledge(services.workflows, session)
            assert report["status"] == "complete"
            assert set(requested) == {snapshot_id}
            assert services.documents.current_snapshot(session)["id"] != snapshot_id
            assert report["snapshot"]["id"] == snapshot_id
        finally:
            await services.close()

    asyncio.run(scenario())


def test_actual_backend_failure_keeps_incomplete_metrics_and_continues_independent_method(
    tmp_path, monkeypatch
):
    async def scenario():
        services, session, _ = await prepare(tmp_path)
        failures = []

        async def fail_graph(*args, **kwargs):
            failures.append(True)
            raise DomainError("INJECTED_GRAPH_OUTAGE", "故障注入：图后端不可达。", 503)

        monkeypatch.setattr(services.graphs, "search", fail_graph)
        try:
            report = await evaluate_knowledge(services.workflows, session)
            assert report["status"] == "incomplete"
            assert report["metrics"]["graph"]["recall_at_3"] is None
            assert report["metrics"]["graph"]["errors"] == 1
            assert report["metrics"]["graph"]["not_run"] == 27
            assert report["metrics"]["lexical"]["status"] == "complete"
            assert failures == [True]
            assert any(
                item["error"] and item["error"]["code"] == "INJECTED_GRAPH_OUTAGE"
                for item in report["failures"]
            )
        finally:
            await services.close()

    asyncio.run(scenario())


def test_changed_or_extra_corpus_is_rejected_instead_of_relabelling_questions(tmp_path):
    async def scenario():
        services, session, _ = await prepare(tmp_path)
        try:
            await services.documents.ingest(
                session,
                filename="extra.md",
                title="额外材料",
                content=b"Extra document",
                media_type="text/markdown",
            )
            await services.documents.build_snapshot(session)
            with pytest.raises(DomainError) as error:
                await evaluate_knowledge(services.workflows, session)
            assert error.value.code == "EVALUATION_CORPUS_MISMATCH"
        finally:
            await services.close()

    asyncio.run(scenario())


def test_case_labels_are_validated_before_evaluation(tmp_path):
    path = tmp_path / "invalid-cases.jsonl"
    path.write_text(
        json.dumps(
            {
                "id": "bad",
                "split": "heldout",
                "question": "问题",
                "answerable": False,
                "expected_sources": [{"title": "X100 主机产品说明", "quotes": ["X100"]}],
                "tags": ["unanswerable"],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(DomainError) as error:
        load_cases(path)
    assert error.value.code == "EVALUATION_DATA_INVALID"
