import hashlib
import json
import math
import tempfile
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime
from pathlib import Path

import pytest

from tracedesk.retrieval import Document, KnowledgeBase, RetrievalConfig, tokenize
from tracedesk.retrieval_eval import evaluate_retrieval

DATA = Path(__file__).resolve().parents[1] / "src" / "tracedesk" / "data"
TODAY = date(2026, 10, 16)


def make_document(identifier="alpha", **overrides):
    values = {
        "id": identifier,
        "version": 1,
        "title": "catalog",
        "text": "apple pear",
        "category": "test",
        "tenant_id": "demo-shop",
        "effective_from": date(2025, 1, 1),
        "effective_to": None,
        "rules": {},
    }
    return Document(**{**values, **overrides})


def search(kb, query, **overrides):
    return kb.search(query, **{"tenant_id": "demo-shop", "as_of": TODAY, **overrides})


@pytest.fixture
def kb():
    return KnowledgeBase.from_path(DATA / "knowledge.json")


@pytest.fixture
def temp_directory():
    with tempfile.TemporaryDirectory(prefix="tracedesk-retrieval-test-") as directory:
        yield Path(directory)


def write_cases(directory, cases):
    path = directory / "cases.jsonl"
    path.write_text(
        "\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n", encoding="utf-8"
    )
    return path


def make_case(identifier="case-1", **overrides):
    return {
        "id": identifier,
        "query": "apple",
        "as_of": TODAY.isoformat(),
        "tenant_id": "demo-shop",
        "expected_source_ids": ["alpha@1"],
        "category": "test",
        **overrides,
    }


def test_bm25_matches_hand_calculation():
    documents = [
        make_document("alpha", text="apple apple pear"),
        make_document("beta", text="apple pear pear pear"),
        make_document("gamma", text="banana"),
    ]
    hits = search(KnowledgeBase(documents), "apple", method="bm25")
    assert [hit.document.id for hit in hits] == ["alpha", "beta"]
    average_length = (4 + 5 + 2) / 3
    inverse_frequency = math.log(1 + (3 - 2 + 0.5) / (2 + 0.5))
    for hit, frequency, length in zip(hits, [2, 1], [4, 5], strict=True):
        expected = (
            inverse_frequency
            * frequency
            * 2.2
            / (frequency + 1.2 * (0.25 + 0.75 * length / average_length))
        )
        assert hit.bm25_score == pytest.approx(expected)
        assert hit.score == hit.bm25_score


def test_ngram_cosine_matches_hand_calculation():
    documents = [
        make_document("alpha", title="ab", text="ab"),
        make_document("beta", title="ab", text="cd"),
    ]
    hits = search(KnowledgeBase(documents), "ab", method="ngram")
    assert hits[0].ngram_score == pytest.approx(1)
    assert hits[1].ngram_score == pytest.approx(1 / math.sqrt(2))
    assert all(hit.score == hit.ngram_score for hit in hits)


def test_rrf_uses_rank_positions_not_raw_score_scales():
    kb = KnowledgeBase(
        [
            make_document("alpha", text="apple apple pear"),
            make_document("beta", text="apple banana banana"),
        ]
    )
    rankings = [search(kb, "apple", method=method) for method in ("bm25", "ngram")]
    expected = {}
    for ranking in rankings:
        for position, hit in enumerate(ranking, start=1):
            expected[hit.document.source_id] = expected.get(hit.document.source_id, 0) + 1 / (
                60 + position
            )
    for hit in search(kb, "apple", method="hybrid"):
        assert hit.score == pytest.approx(expected[hit.document.source_id])


@pytest.mark.parametrize("method", ["bm25", "ngram", "hybrid"])
def test_ties_are_stable_across_document_insertion_order(method):
    left = make_document("alpha")
    right = make_document("beta")
    first = search(KnowledgeBase([right, left]), "apple", method=method)
    second = search(KnowledgeBase([left, right]), "apple", method=method)
    assert [hit.document.source_id for hit in first] == ["alpha@1", "beta@1"]
    assert first == second


def test_tenant_filtering_precedes_scoring_and_statistics():
    own = make_document("alpha")
    other = make_document("other", tenant_id="other-shop", text="apple " * 1000)
    first = search(KnowledgeBase([own]), "apple", method="bm25")
    second = search(KnowledgeBase([other, own]), "apple", method="bm25")
    assert first == second
    assert search(KnowledgeBase([other]), "apple") == []


def test_inactive_versions_do_not_affect_bm25_statistics():
    current = make_document("alpha", version=2, effective_from=date(2026, 1, 1))
    old = make_document("alpha", text="apple " * 100, effective_to=date(2026, 1, 1))
    assert search(KnowledgeBase([current]), "apple") == search(
        KnowledgeBase([current, old]), "apple"
    )


@pytest.mark.parametrize(
    "day,source,days",
    [
        (date(2025, 1, 1), "returns@1", 14),
        (date(2025, 12, 31), "returns@1", 14),
        (date(2026, 1, 1), "returns@2", 7),
        (TODAY, "returns@2", 7),
    ],
)
def test_returns_half_open_intervals_and_rule_rendering(kb, day, source, days):
    document = kb.get("returns", tenant_id="demo-shop", as_of=day)
    assert document.source_id == source
    assert document.rules["window_days"] == days
    assert f"签收后 {days} 天" in document.text
    assert "已签收" in document.text and "数字商品、定制商品不适用" in document.text
    assert "{" not in document.text
    hits = search(kb, "退货申请期限", as_of=day)
    assert source in [hit.document.source_id for hit in hits]
    assert all(hit.document.id != "returns" or hit.document.source_id == source for hit in hits)


def test_missing_history_and_missing_tenant_return_no_document(kb):
    assert kb.get("returns", tenant_id="demo-shop", as_of=date(2024, 12, 31)) is None
    assert kb.get("returns", tenant_id="other-shop", as_of=TODAY) is None
    assert search(kb, "退货申请期限", as_of=date(2024, 12, 31)) == []
    assert search(kb, "退货申请期限", tenant_id="unknown-shop") == []


def test_cross_tenant_document_can_only_be_seen_by_its_tenant(kb):
    assert search(kb, "琥珀礼遇兑换暗号") == []
    hits = search(kb, "琥珀礼遇兑换暗号", tenant_id="other-shop")
    assert [hit.document.source_id for hit in hits] == ["other-benefit@1"]


@pytest.mark.parametrize("query", ["", " \t\n", None, 17])
def test_invalid_query_is_explicit(kb, query):
    with pytest.raises(ValueError, match="query"):
        search(kb, query)


@pytest.mark.parametrize(
    "overrides",
    [
        {"top_k": 0},
        {"top_k": True},
        {"top_k": "3"},
        {"method": "semantic"},
        {"tenant_id": ""},
        {"as_of": "2026-10-16"},
        {"as_of": datetime(2026, 10, 16)},
    ],
)
def test_search_rejects_invalid_controls(kb, overrides):
    with pytest.raises(ValueError):
        search(kb, "退货", **overrides)


def test_query_length_is_explicit_configurable_limit():
    kb = KnowledgeBase([make_document()], config=RetrievalConfig(max_query_characters=5))
    assert search(kb, "apple")
    with pytest.raises(ValueError, match="exceeds 5"):
        search(kb, "apple pear")


def test_no_evidence_does_not_fill_top_k(kb):
    assert search(kb, "火星的轨道半径") == []
    assert search(kb, "！！！") == []
    assert search(KnowledgeBase([]), "退货") == []
    assert len(search(KnowledgeBase([make_document()]), "apple", top_k=5)) == 1


def test_token_boundaries_nfkc_and_grammatical_particles():
    assert tokenize("退货，发票") == ("退货", "发票")
    assert tokenize("退款 发票") == ("退款", "发票")
    assert tokenize("ＵＳＢ 接口") == ("usb", "接口")
    assert tokenize("我想请问可以退款吗") == ("退款",)
    assert tokenize("票") == ("票",)
    assert "款发" not in tokenize("退款-发票")


def test_query_repetitions_do_not_multiply_bm25_weight():
    kb = KnowledgeBase([make_document()])
    assert (
        search(kb, "apple", method="bm25")[0].score
        == search(kb, "apple apple", method="bm25")[0].score
    )


def test_coverage_gate_is_explicit_and_can_be_disabled():
    document = make_document()
    strict = KnowledgeBase([document], config=RetrievalConfig(min_query_coverage=0.5))
    disabled = KnowledgeBase(
        [document], config=RetrievalConfig(min_query_coverage=0, min_ngram_score=0)
    )
    assert search(strict, "apple astronomy universe") == []
    assert search(disabled, "apple astronomy universe")
    assert search(disabled, "astronomy universe") == []


@pytest.mark.parametrize(
    "options",
    [
        {"k1": 0},
        {"k1": float("nan")},
        {"b": 1.1},
        {"b": True},
        {"rrf_k": 0},
        {"rrf_k": 1.5},
        {"min_query_coverage": -0.1},
        {"min_ngram_score": float("inf")},
        {"max_query_characters": False},
    ],
)
def test_configuration_errors_are_explicit(options):
    with pytest.raises(ValueError):
        RetrievalConfig(**options)


@pytest.mark.parametrize(
    "overrides",
    [
        {"version": 0},
        {"version": True},
        {"title": ""},
        {"text": " "},
        {"id": "bad@id"},
        {"tenant_id": ""},
        {"effective_from": "2025-01-01"},
        {"effective_to": date(2024, 1, 1)},
        {"rules": []},
        {"rules": {"x": float("nan")}},
    ],
)
def test_document_validation(overrides):
    with pytest.raises(ValueError):
        make_document(**overrides)


def test_document_and_nested_rules_cannot_drift_after_indexing():
    supplied = {"items": ["a"], "nested": {"limit": 7}}
    document = make_document(rules=supplied)
    supplied["items"].append("b")
    supplied["nested"]["limit"] = 90
    assert document.rules["items"] == ("a",)
    assert document.rules["nested"]["limit"] == 7
    with pytest.raises(FrozenInstanceError):
        document.title = "changed"
    with pytest.raises(TypeError, match="immutable"):
        document.rules["nested"]["limit"] = 3


def test_duplicate_source_ids_and_overlapping_versions_are_rejected():
    first = make_document()
    with pytest.raises(ValueError, match="duplicate"):
        KnowledgeBase([first, first])
    with pytest.raises(ValueError, match="duplicate"):
        KnowledgeBase([first, replace(first, tenant_id="other-shop")])
    with pytest.raises(ValueError, match="overlapping"):
        KnowledgeBase([first, replace(first, version=2, effective_from=date(2026, 1, 1))])
    with pytest.raises(ValueError, match="overlapping"):
        KnowledgeBase(
            [
                replace(first, effective_to=date(2026, 1, 2)),
                replace(first, version=2, effective_from=date(2026, 1, 1)),
            ]
        )


def test_adjacent_intervals_and_gaps_are_allowed():
    first = make_document(effective_to=date(2026, 1, 1))
    adjacent = replace(
        first, version=2, effective_from=date(2026, 1, 1), effective_to=date(2026, 2, 1)
    )
    after_gap = replace(first, version=3, effective_from=date(2026, 3, 1), effective_to=None)
    kb = KnowledgeBase([after_gap, first, adjacent])
    assert kb.get("alpha", tenant_id="demo-shop", as_of=date(2026, 2, 10)) is None


@pytest.mark.parametrize(
    "text",
    [
        "申请 {missing} 天",
        "申请 {window_days.real} 天",
        "申请 {window_days:02d} 天",
        "申请 {window_days!r} 天",
        "申请 {window_days 天",
    ],
)
def test_templates_reject_missing_fields_and_dynamic_formatting(text):
    with pytest.raises(ValueError):
        make_document(text=text, rules={"window_days": 7})


def test_literal_template_braces_are_not_executed():
    document = make_document(
        text="标识 {{literal}}，期限 {window_days} 天", rules={"window_days": 7}
    )
    assert document.text == "标识 {literal}，期限 7 天"


def test_template_cannot_erase_required_policy_content():
    with pytest.raises(ValueError, match="rendered policy text"):
        make_document(text="{body}", rules={"body": " "})


def test_rule_change_updates_rendered_text_from_one_source(temp_directory):
    raw = json.loads((DATA / "knowledge.json").read_text(encoding="utf-8"))
    current = next(
        item for item in raw["documents"] if item["id"] == "returns" and item["version"] == 2
    )
    current["rules"]["window_days"] = 11
    path = temp_directory / "knowledge.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    document = KnowledgeBase.from_path(path).get("returns", tenant_id="demo-shop", as_of=TODAY)
    assert document.rules["window_days"] == 11
    assert "签收后 11 天" in document.text
    assert "签收后 7 天" not in document.text


@pytest.mark.parametrize(
    "change",
    [
        {"window_days": True},
        {"window_days": 0},
        {"allowed_statuses": []},
        {"excluded_categories": ["unknown"]},
        {"allowed_statuses": ["delivered", "delivered"]},
    ],
)
def test_invalid_returns_rules_fail(change):
    rules = {
        "window_days": 7,
        "allowed_statuses": ["delivered"],
        "excluded_categories": ["digital", "custom"],
        **change,
    }
    with pytest.raises(ValueError):
        make_document("returns", rules=rules)


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        '{"schema_version":2,"documents":[]}',
        '{"schema_version":true,"documents":[]}',
        '{"schema_version":1,"documents":{}}',
        '{"schema_version":1,"documents":[{}]}',
        '{"schema_version":1,"schema_version":1,"documents":[]}',
    ],
)
def test_knowledge_schema_errors_fail_without_partial_load(temp_directory, raw):
    path = temp_directory / "bad.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError):
        KnowledgeBase.from_path(path)


def test_evaluator_denominators_and_dataset_identity(temp_directory):
    kb = KnowledgeBase([make_document()])
    cases = [make_case(), make_case("case-2", query="astronomy", expected_source_ids=[])]
    path = write_cases(temp_directory, cases)
    result = evaluate_retrieval(kb, path)
    assert result["dataset_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert len(result["knowledge_sha256"]) == 64
    assert result["configuration"]["min_query_coverage"] == 0.25
    for method in result["methods"].values():
        assert method["queries"] == 2 and method["answerable"] == 1 and method["unanswerable"] == 1
        assert (
            method["recall_at_3"] == 1 and method["mrr"] == 1 and method["no_answer_accuracy"] == 1
        )
        assert method["latency_ms_p50"] >= 0
        assert method["cases"][1]["recall_at_3"] is None


def test_evaluator_reports_real_misses_instead_of_success(temp_directory):
    path = write_cases(temp_directory, [make_case(query="astronomy")])
    result = evaluate_retrieval(KnowledgeBase([make_document()]), path)
    for method in result["methods"].values():
        assert method["recall_at_3"] == 0 and method["mrr"] == 0
        assert method["no_answer_accuracy"] is None


def test_evaluator_reports_missing_answerable_denominator(temp_directory):
    path = write_cases(temp_directory, [make_case(query="astronomy", expected_source_ids=[])])
    for method in evaluate_retrieval(KnowledgeBase([]), path)["methods"].values():
        assert method["recall_at_3"] is None and method["mrr"] is None
        assert method["no_answer_accuracy"] == 1


def test_evaluation_evidence_records_changed_policy_and_configuration(temp_directory):
    path = write_cases(temp_directory, [make_case()])
    baseline = evaluate_retrieval(KnowledgeBase([make_document()]), path)
    changed = evaluate_retrieval(
        KnowledgeBase(
            [make_document(text="apple orange")], config=RetrievalConfig(min_query_coverage=0.75)
        ),
        path,
    )
    assert baseline["dataset_sha256"] == changed["dataset_sha256"]
    assert baseline["knowledge_sha256"] != changed["knowledge_sha256"]
    assert changed["configuration"]["min_query_coverage"] == 0.75


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "\n",
        "{}\n",
        "not json\n",
        '{"id":"a","id":"b","query":"apple","as_of":"2026-10-16","tenant_id":"demo-shop","expected_source_ids":[],"category":"test"}\n',
    ],
)
def test_evaluation_rejects_empty_or_malformed_dataset(temp_directory, raw):
    path = temp_directory / "bad-cases.jsonl"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError):
        evaluate_retrieval(KnowledgeBase([]), path)


@pytest.mark.parametrize("documents", [None, "document", {"id": "a"}, ["not a document"]])
def test_invalid_corpus_is_not_silently_replaced_with_empty(documents):
    with pytest.raises(ValueError):
        KnowledgeBase(documents)


@pytest.mark.parametrize("path", [None, "", 10])
def test_invalid_knowledge_path_is_explicit(path):
    with pytest.raises(ValueError, match="path"):
        KnowledgeBase.from_path(path)


@pytest.mark.parametrize(
    "overrides",
    [
        {"expected_source_ids": ["missing@1"]},
        {"tenant_id": "other-shop"},
        {"as_of": "2024-12-31"},
        {"as_of": "2026-99-01"},
        {"expected_source_ids": ["alpha@1", "alpha@1"]},
    ],
)
def test_evaluator_rejects_broken_ground_truth(temp_directory, overrides):
    path = write_cases(temp_directory, [make_case(**overrides)])
    with pytest.raises(ValueError):
        evaluate_retrieval(KnowledgeBase([make_document()]), path)


def test_fixed_dataset_is_synthetic_complete_and_reproducible(kb):
    report = evaluate_retrieval(kb, DATA / "retrieval_cases.jsonl")
    assert "synthetic" in report["scope"] and "not blind" in report["scope"]
    for method in report["methods"].values():
        assert method["queries"] == 60
        assert method["answerable"] == 48 and method["unanswerable"] == 12
        assert len(method["cases"]) == 60
        for case in method["cases"]:
            assert all(
                kb.get(
                    source.rsplit("@", 1)[0],
                    tenant_id=case["tenant_id"],
                    as_of=date.fromisoformat(case["as_of"]),
                )
                is not None
                for source in case["retrieved_source_ids"]
            )
