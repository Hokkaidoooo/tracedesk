"""Reproducible retrieval diagnostics on one published, synthetic-data snapshot."""

import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.resources import files
from math import log2
from pathlib import Path
from statistics import mean, median
from time import perf_counter
from typing import Any

from .errors import DomainError
from .evidence import Evidence
from .knowledge_workflows import KnowledgeWorkflows

LOGGER = logging.getLogger(__name__)
TOP_K = 3
EVALUATION_METHODS = ("lexical", "semantic", "hybrid", "graph")
CASE_FIELDS = {"id", "split", "question", "answerable", "expected_sources", "tags"}
METRIC_NOTES = {
    "recall_at_3": "仅统计有答案问题；每题为前三个证据块涉及的正确文档数 / 所需文档数，再宏平均。",
    "micro_recall_at_3": "有答案问题的正确文档总数 / 所需文档总数；多来源问题的每份必要文档各计一次。",
    "mrr_at_3": "有答案问题的首个正确文档所在证据块排名倒数；前三块没有正确文档时为 0。",
    "ndcg_at_3": "每份必要文档仅在首次命中时计一次二元增益，重复块不重复加分。",
    "support_recall_at_3": "前三个原文块实际包含的标注片段数 / 必要片段数，再对有答案问题宏平均。",
    "no_answer_false_recall_rate": "无答案问题仍返回任意证据块的比例；这是检索层误召回，不是生成答案的幻觉率。",
    "latency_ms": "实际检索调用的墙钟时间；没有预热，首次调用可能包含模型或后端初始化。",
}


@dataclass(frozen=True)
class ExpectedSource:
    title: str
    quotes: tuple[str, ...]


@dataclass(frozen=True)
class KnowledgeCase:
    id: str
    split: str
    question: str
    answerable: bool
    expected_sources: tuple[ExpectedSource, ...]
    tags: tuple[str, ...]


def _fingerprint(value: Any) -> str:
    text = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_cases(path: Path | None = None) -> tuple[tuple[KnowledgeCase, ...], str]:
    source = (
        path if path is not None else files("tracedesk").joinpath("data", "knowledge-eval.jsonl")
    )
    raw = source.read_bytes()
    cases = []
    for number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DomainError(
                "EVALUATION_DATA_INVALID", f"评测数据第 {number} 行不是有效 JSON。"
            ) from exc
        cases.append(_case(record, number))
    if (
        not cases
        or len({case.id for case in cases}) != len(cases)
        or len({case.question for case in cases}) != len(cases)
    ):
        raise DomainError("EVALUATION_DATA_INVALID", "评测数据为空或包含重复题号、问题。")
    return tuple(cases), hashlib.sha256(raw).hexdigest()


def _case(record: Any, number: int) -> KnowledgeCase:
    invalid = DomainError("EVALUATION_DATA_INVALID", f"评测数据第 {number} 行字段或标注无效。")
    if not isinstance(record, dict) or set(record) != CASE_FIELDS:
        raise invalid
    if any(
        not isinstance(record[name], str) or not record[name].strip() for name in ("id", "question")
    ):
        raise invalid
    if record["split"] not in {"heldout", "diagnostic"} or type(record["answerable"]) is not bool:
        raise invalid
    tags = record["tags"]
    if (
        not isinstance(tags, list)
        or not tags
        or any(not isinstance(tag, str) or not tag for tag in tags)
        or len(set(tags)) != len(tags)
    ):
        raise invalid
    expected = record["expected_sources"]
    if not isinstance(expected, list) or bool(expected) != record["answerable"]:
        raise invalid
    sources = []
    for item in expected:
        if not isinstance(item, dict) or set(item) != {"title", "quotes"}:
            raise invalid
        if not isinstance(item["title"], str) or not item["title"].strip():
            raise invalid
        quotes = item["quotes"]
        if (
            not isinstance(quotes, list)
            or not quotes
            or any(not isinstance(quote, str) or not quote.strip() for quote in quotes)
            or len(set(quotes)) != len(quotes)
        ):
            raise invalid
        sources.append(ExpectedSource(item["title"], tuple(quotes)))
    if len({item.title for item in sources}) != len(sources):
        raise invalid
    return KnowledgeCase(
        record["id"],
        record["split"],
        record["question"].strip(),
        record["answerable"],
        tuple(sources),
        tuple(tags),
    )


def _corpus(evidence: tuple[Evidence, ...], cases: tuple[KnowledgeCase, ...]) -> dict[str, Any]:
    expected = {}
    resource = files("tracedesk").joinpath("data", "knowledge-demo")
    for document in resource.iterdir():
        if document.name.endswith(".md"):
            raw = document.read_bytes()
            title = raw.decode("utf-8").splitlines()[0].removeprefix("#").strip()
            expected[title] = hashlib.sha256(raw).hexdigest()
    actual: dict[str, set[tuple[str, int, str]]] = {}
    for item in evidence:
        actual.setdefault(item.title, set()).add(
            (item.document_id, item.document_version, item.source_sha256)
        )
    if set(actual) != set(expected) or any(len(records) != 1 for records in actual.values()):
        raise DomainError(
            "EVALUATION_CORPUS_MISMATCH",
            "固定评测需要四份且仅有四份演示资料，请使用独立评测数据库。",
            409,
        )
    for title, records in actual.items():
        if next(iter(records))[2] != expected[title]:
            raise DomainError(
                "EVALUATION_CORPUS_MISMATCH", "评测快照原件与标注使用的演示资料不同。", 409
            )
    for case in cases:
        for source in case.expected_sources:
            for quote in source.quotes:
                if not any(item.title == source.title and quote in item.text for item in evidence):
                    raise DomainError(
                        "EVALUATION_LABEL_MISMATCH",
                        f"问题 {case.id} 的标注原文不在固定快照中。",
                        409,
                    )
        if any(case.question in item.text for item in evidence):
            raise DomainError(
                "EVALUATION_QUERY_LEAKAGE", f"问题 {case.id} 被混入索引，停止评测。", 409
            )
    documents = [{"title": title, "source_sha256": expected[title]} for title in sorted(expected)]
    return {
        "documents": documents,
        "document_count": len(documents),
        "source_count": len(evidence),
        "sha256": _fingerprint(documents),
        "synthetic": True,
    }


def _snapshot_fingerprint(snapshot: dict, evidence: tuple[Evidence, ...]) -> str:
    return _fingerprint(
        {
            "snapshot_id": snapshot["id"],
            "tenant_id": snapshot["tenant_id"],
            "embedding_fingerprint": snapshot["embedding_fingerprint"],
            "artifacts": snapshot["artifacts"],
            "sources": [
                {
                    "source_id": item.source_id,
                    "text": item.text,
                    "source_sha256": item.source_sha256,
                    "version": item.document_version,
                }
                for item in sorted(evidence, key=lambda item: item.source_id)
            ],
        }
    )


def _methods(requested: tuple[str, ...] | None, available: tuple[str, ...]) -> tuple[str, ...]:
    selected = (
        requested
        if requested is not None
        else tuple(method for method in EVALUATION_METHODS if method in available)
    )
    if not selected or len(set(selected)) != len(selected):
        raise DomainError("EVALUATION_METHOD_INVALID", "评测方法列表为空或包含重复项。")
    unsupported = [
        method for method in selected if method not in EVALUATION_METHODS or method not in available
    ]
    if unsupported:
        raise DomainError(
            "EVALUATION_METHOD_UNAVAILABLE",
            "当前快照或服务未启用这些评测方法：" + ", ".join(unsupported),
            409,
            {
                "unavailable": unsupported,
                "available": [m for m in EVALUATION_METHODS if m in available],
            },
        )
    return selected


def _score(case: KnowledgeCase, evidence: tuple[Evidence, ...]) -> dict[str, Any]:
    expected = {source.title for source in case.expected_sources}
    titles = [item.title for item in evidence[:TOP_K]]
    matched = expected.intersection(titles)
    missing = sorted(expected.difference(matched))
    quotes = [(source.title, quote) for source in case.expected_sources for quote in source.quotes]
    covered = sum(
        any(item.title == title and quote in item.text for item in evidence[:TOP_K])
        for title, quote in quotes
    )
    reasons = []
    if missing:
        reasons.append("missing_required_documents")
    if case.answerable and covered != len(quotes):
        reasons.append("missing_required_quotes")
    if not case.answerable and evidence:
        reasons.append("retrieved_for_unanswerable_question")
    first_rank = next((rank for rank, title in enumerate(titles, 1) if title in expected), None)
    seen = set()
    dcg = 0.0
    for rank, title in enumerate(titles, 1):
        if title in expected and title not in seen:
            dcg += 1 / log2(rank + 1)
            seen.add(title)
    ideal = sum(1 / log2(rank + 1) for rank in range(1, min(TOP_K, len(expected)) + 1))
    return {
        "matched_documents": sorted(matched),
        "missing_documents": missing,
        "required_document_count": len(expected),
        "matched_document_count": len(matched),
        "required_quote_count": len(quotes),
        "matched_quote_count": covered,
        "recall_at_3": len(matched) / len(expected) if case.answerable else None,
        "mrr_at_3": (1 / first_rank if first_rank else 0.0) if case.answerable else None,
        "ndcg_at_3": dcg / ideal if ideal else None,
        "support_recall_at_3": covered / len(quotes) if case.answerable else None,
        "no_answer_false_recall": bool(evidence) if not case.answerable else None,
        "failure_reasons": reasons,
    }


async def _retrieve(
    workflows: KnowledgeWorkflows,
    session: dict,
    case: KnowledgeCase,
    method: str,
    snapshot_id: str,
    known: dict[str, Evidence],
) -> dict[str, Any]:
    started = perf_counter()
    try:
        result = await workflows.search(
            session, case.question, method=method, snapshot_id=snapshot_id, limit=TOP_K
        )
        if len(result) > TOP_K or len({item.source_id for item in result}) != len(result):
            raise DomainError("EVALUATION_RESULT_INVALID", "检索结果超过上限或包含重复来源。", 502)
        for item in result:
            source = known.get(item.source_id)
            if (
                item.method != method
                or source is None
                or any(
                    getattr(item, name) != getattr(source, name)
                    for name in (
                        "tenant_id",
                        "snapshot_id",
                        "document_id",
                        "document_version",
                        "title",
                        "text",
                        "source_sha256",
                        "page",
                        "kind",
                    )
                )
            ):
                raise DomainError(
                    "EVALUATION_SCOPE_MISMATCH", "检索结果与固定快照原文不一致。", 502
                )
        return {
            "status": "ok",
            "latency_ms": round((perf_counter() - started) * 1000, 3),
            "sources": [item.as_dict() for item in result],
            **_score(case, result),
        }
    except DomainError as exc:
        error = {"code": exc.code, "message": exc.message, "status_code": exc.status_code}
    except Exception as exc:
        # An evaluation boundary records the failure and keeps metrics unavailable, never substitutes results.
        LOGGER.exception("Knowledge evaluation failed for %s/%s", case.id, method)
        error = {
            "code": "EVALUATION_RETRIEVAL_FAILED",
            "message": "检索执行失败，详见服务日志。",
            "exception_type": type(exc).__name__,
        }
    return {
        "status": "error",
        "latency_ms": round((perf_counter() - started) * 1000, 3),
        "error": error,
    }


def _summary(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    complete = all(row["status"] == "ok" for row in rows)
    measured = [row for row in rows if row["status"] == "ok"]
    answerable = [row for row in measured if row["recall_at_3"] is not None]
    unanswerable = [row for row in measured if row["no_answer_false_recall"] is not None]
    elapsed = [row["latency_ms"] for row in rows if "latency_ms" in row]
    metrics = {
        key: mean(row[key] for row in answerable) if complete and answerable else None
        for key in ("recall_at_3", "mrr_at_3", "ndcg_at_3", "support_recall_at_3")
    }
    denominator = sum(row["required_document_count"] for row in answerable)
    metrics["micro_recall_at_3"] = (
        sum(row["matched_document_count"] for row in answerable) / denominator
        if complete and denominator
        else None
    )
    metrics["no_answer_false_recall_rate"] = (
        mean(int(row["no_answer_false_recall"]) for row in unanswerable)
        if complete and unanswerable
        else None
    )
    return {
        "status": "complete" if complete else "incomplete",
        "case_count": len(rows),
        "measured_cases": len(measured),
        "answerable_measured": len(answerable),
        "unanswerable_measured": len(unanswerable),
        "errors": sum(row["status"] == "error" for row in rows),
        "not_run": sum(row["status"] == "not_run" for row in rows),
        "failed_cases": sum(bool(row["failure_reasons"]) for row in measured),
        "latency_total_ms": round(sum(elapsed), 3),
        "latency_median_ms": median(elapsed) if complete and elapsed else None,
        **metrics,
    }


async def evaluate_knowledge(
    workflows: KnowledgeWorkflows,
    session: dict,
    *,
    snapshot_id: str | None = None,
    methods: tuple[str, ...] | None = None,
    cases_path: Path | None = None,
) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc).isoformat()
    started = perf_counter()
    cases, data_fingerprint = load_cases(cases_path)
    snapshot = workflows.snapshot(session, snapshot_id)
    evidence = workflows.documents.evidence(session, snapshot["id"])
    selected = _methods(methods, workflows.methods(session, snapshot))
    corpus = _corpus(evidence, cases)
    snapshot_digest = _snapshot_fingerprint(snapshot, evidence)
    known = {item.source_id: item for item in evidence}
    halted: dict[str, str] = {}
    records = []
    for index, case in enumerate(cases):
        results = {}
        order = selected[index % len(selected) :] + selected[: index % len(selected)]
        for method in order:
            if method in halted:
                results[method] = {
                    "status": "not_run",
                    "reason": "earlier_method_error",
                    "failed_case_id": halted[method],
                }
                continue
            results[method] = await _retrieve(
                workflows, session, case, method, snapshot["id"], known
            )
            if results[method]["status"] == "error":
                halted[method] = case.id
        records.append(
            {
                "id": case.id,
                "split": case.split,
                "question": case.question,
                "answerable": case.answerable,
                "tags": list(case.tags),
                "expected_sources": [
                    {"title": source.title, "quotes": list(source.quotes)}
                    for source in case.expected_sources
                ],
                "results": results,
            }
        )
    final_snapshot = workflows.snapshot(session, snapshot["id"])
    final_evidence = workflows.documents.evidence(session, snapshot["id"])
    if _snapshot_fingerprint(final_snapshot, final_evidence) != snapshot_digest:
        raise DomainError(
            "EVALUATION_SNAPSHOT_CHANGED", "评测期间固定快照发生改变，结果不可使用。", 409
        )
    metrics = {
        method: _summary([record["results"][method] for record in records]) for method in selected
    }
    graph = snapshot["artifacts"].get("graph")
    retrieval_options = {
        "rrf_k": workflows.documents.config.rrf_k,
        "graph_backend": workflows.graphs.config.backend,
        "graph_max_hops": workflows.graphs.config.max_hops,
        "graph_max_expansions": workflows.graphs.config.max_expansions,
        "graph_query_timeout_seconds": workflows.graphs.config.query_timeout_seconds,
    }
    failures = [
        {
            "case_id": record["id"],
            "question": record["question"],
            "method": method,
            "status": result["status"],
            "reasons": result.get("failure_reasons", []),
            "error": result.get("error"),
        }
        for record in records
        for method, result in record["results"].items()
        if result["status"] == "error" or result.get("failure_reasons")
    ]
    return {
        "schema": 1,
        "title": "TraceDesk 固定快照知识检索评测",
        "status": "incomplete" if halted else "complete",
        "scope_notes": [
            "28 道自建问题基于四份虚构产品与咨询资料；不代表真实客户问题分布。"
            if cases_path is None
            else "使用调用者提供的问题文件与四份固定演示资料。",
            "heldout 表示问题未用于建索引或调整检索参数；不声称独立行业测试集或预训练隔离。",
            "图结果使用快照内已有关系；显式人工示例关系的检索成绩不能当作模型关系抽取效果。",
            "本次不生成答案，不调用聊天模型；语义检索按实际配置调用嵌入后端。",
            "不存在预设的胜出方法或拒答阈值；所有漏召回和无答案误召回均保留。",
            "任一方法发生执行错误后，该方法后续题目标为未运行，汇总质量指标为 null。",
        ],
        "metric_definitions": dict(METRIC_NOTES),
        "top_k": TOP_K,
        "execution": {
            "started_at": started_at,
            "elapsed_ms": round((perf_counter() - started) * 1000, 3),
            "method_order": "各题轮换方法顺序；无预热。",
            "chat_model_calls": 0,
        },
        "dataset": {
            "sha256": data_fingerprint,
            "case_count": len(cases),
            "answerable_count": sum(case.answerable for case in cases),
            "unanswerable_count": sum(not case.answerable for case in cases),
        },
        "corpus": corpus,
        "snapshot": {
            "id": snapshot["id"],
            "tenant_id": snapshot["tenant_id"],
            "sha256": snapshot_digest,
            "with_embeddings": snapshot["with_embeddings"],
        },
        "retrieval_options": {**retrieval_options, "sha256": _fingerprint(retrieval_options)},
        "models": {
            "embedding_selected": any(method in {"semantic", "hybrid"} for method in selected),
            "embedding_provider": workflows.documents.config.embedding_provider,
            "embedding_model": workflows.documents.config.embedding_model or None,
            "embedding_dimensions": workflows.documents.config.embedding_dimensions or None,
            "embedding_fingerprint": snapshot["embedding_fingerprint"],
            "graph_selected": "graph" in selected,
            "graph": {
                "backend": graph["backend"],
                "extraction": graph["extraction"],
                "fact_count": graph["fact_count"],
                "projection_digest": graph["projection_digest"],
            }
            if graph
            else None,
        },
        "methods": list(selected),
        "metrics": metrics,
        "by_split": {
            split: {
                method: _summary(
                    [record["results"][method] for record in records if record["split"] == split]
                )
                for method in selected
            }
            for split in sorted({case.split for case in cases})
        },
        "by_tag": {
            tag: {
                method: _summary(
                    [record["results"][method] for record in records if tag in record["tags"]]
                )
                for method in selected
            }
            for tag in sorted({tag for case in cases for tag in case.tags})
        },
        "cases": records,
        "failures": failures,
    }
