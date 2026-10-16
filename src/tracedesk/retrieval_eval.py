"""Run the fixed synthetic retrieval regression set without model calls."""

from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import asdict
from datetime import date
from pathlib import Path
from time import perf_counter

from .retrieval import METHODS, KnowledgeBase, _parse_day, _unique_object

_CASE_FIELDS = {"id", "query", "as_of", "tenant_id", "expected_source_ids", "category"}


def _read_cases(raw: bytes) -> list[dict]:
    cases = []
    seen = set()
    for line_number, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            raise ValueError(f"retrieval cases line {line_number} is blank")
        try:
            case = json.loads(line, object_pairs_hook=_unique_object)
        except json.JSONDecodeError as exc:
            raise ValueError(f"retrieval cases line {line_number} is invalid JSON") from exc
        if not isinstance(case, dict) or set(case) != _CASE_FIELDS:
            raise ValueError(f"retrieval cases line {line_number} has invalid fields")
        for name in ("id", "query", "as_of", "tenant_id", "category"):
            if not isinstance(case[name], str) or not case[name].strip():
                raise ValueError(f"retrieval cases line {line_number}: {name} must be nonempty")
        if case["id"] in seen:
            raise ValueError(f"duplicate retrieval case id: {case['id']}")
        seen.add(case["id"])
        _parse_day(case["as_of"], f"retrieval case {case['id']} as_of")
        expected = case["expected_source_ids"]
        if not isinstance(expected, list) or any(
            not isinstance(item, str) or not item for item in expected
        ):
            raise ValueError(f"retrieval case {case['id']} has invalid expected_source_ids")
        if len(set(expected)) != len(expected):
            raise ValueError(f"retrieval case {case['id']} contains duplicate expected sources")
        cases.append(case)
    if not cases:
        raise ValueError("retrieval cases must not be empty")
    return cases


def _validate_expected_sources(kb: KnowledgeBase, cases: list[dict]) -> None:
    by_source = {document.source_id: document for document in kb.documents}
    for case in cases:
        case_day = date.fromisoformat(case["as_of"])
        for source in case["expected_source_ids"]:
            if source not in by_source:
                raise ValueError(f"retrieval case {case['id']} expects an unknown source: {source}")
            document = by_source[source]
            if document.tenant_id != case["tenant_id"]:
                raise ValueError(
                    f"retrieval case {case['id']} expects a cross-tenant source: {source}"
                )
            selected = kb.get(document.id, tenant_id=case["tenant_id"], as_of=case_day)
            if selected is None or selected.source_id != source:
                raise ValueError(
                    f"retrieval case {case['id']} expects an inactive source: {source}"
                )


def _evaluate_case(kb: KnowledgeBase, case: dict, method: str) -> dict:
    start = perf_counter()
    hits = kb.search(
        case["query"],
        tenant_id=case["tenant_id"],
        as_of=date.fromisoformat(case["as_of"]),
        top_k=3,
        method=method,
    )
    latency = (perf_counter() - start) * 1000
    expected = set(case["expected_source_ids"])
    retrieved = [hit.document.source_id for hit in hits]
    relevant_ranks = [rank for rank, source in enumerate(retrieved, start=1) if source in expected]
    return {
        **case,
        "retrieved_source_ids": retrieved,
        "scores": [
            {
                "source_id": hit.document.source_id,
                "score": hit.score,
                "bm25_score": hit.bm25_score,
                "ngram_score": hit.ngram_score,
            }
            for hit in hits
        ],
        "recall_at_3": len(expected.intersection(retrieved)) / len(expected) if expected else None,
        "reciprocal_rank": 1 / relevant_ranks[0] if relevant_ranks else 0.0,
        "correct_no_answer": not hits if not expected else None,
        "latency_ms": round(latency, 6),
    }


def _summarize(results: list[dict]) -> dict:
    answerable = [case for case in results if case["expected_source_ids"]]
    unanswerable = [case for case in results if not case["expected_source_ids"]]
    return {
        "queries": len(results),
        "answerable": len(answerable),
        "unanswerable": len(unanswerable),
        "recall_at_3": statistics.mean(case["recall_at_3"] for case in answerable)
        if answerable
        else None,
        "mrr": statistics.mean(case["reciprocal_rank"] for case in answerable)
        if answerable
        else None,
        "no_answer_accuracy": statistics.mean(case["correct_no_answer"] for case in unanswerable)
        if unanswerable
        else None,
        "latency_ms_p50": statistics.median(case["latency_ms"] for case in results),
        "cases": results,
    }


def evaluate_retrieval(kb: KnowledgeBase, cases_path: str | Path) -> dict:
    if not isinstance(kb, KnowledgeBase):
        raise ValueError("kb must be a KnowledgeBase")
    raw = Path(cases_path).read_bytes()
    cases = _read_cases(raw)
    _validate_expected_sources(kb, cases)
    knowledge = [
        {
            **asdict(document),
            "effective_from": document.effective_from.isoformat(),
            "effective_to": document.effective_to.isoformat() if document.effective_to else None,
        }
        for document in kb.documents
    ]
    knowledge_bytes = json.dumps(
        knowledge, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "knowledge_sha256": hashlib.sha256(knowledge_bytes).hexdigest(),
        "configuration": asdict(kb.config),
        "scope": "Fixed synthetic policy retrieval regression; not blind testing or production accuracy.",
        "methods": {
            method: _summarize([_evaluate_case(kb, case, method) for case in cases])
            for method in METHODS
        },
    }
