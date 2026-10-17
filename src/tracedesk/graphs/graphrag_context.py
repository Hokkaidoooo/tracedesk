"""Map official GraphRAG 2.7.0 context identifiers back to stable source IDs."""

from collections.abc import Mapping
from math import isfinite
from typing import Any

from ..errors import DomainError
from ..evidence import Evidence

CONTEXT_TABLES = {"sources": "text_units", "entities": "entities", "relationships": "relationships"}


def source_map(
    tables: Mapping[str, list[dict[str, Any]]],
    evidence: tuple[Evidence, ...],
) -> dict[str, dict[str, list[str]]]:
    expected = {item.source_id: item for item in evidence}
    documents = tables["documents"]
    actual = {_identifier(row.get("id")) for row in documents}
    if actual != set(expected) or len(documents) != len(actual):
        raise _invalid("GraphRAG 文档表与固定证据快照不一致。")
    for row in documents:
        if row.get("text") != expected[_identifier(row["id"])].text:
            raise _invalid("GraphRAG 文档表原文与证据快照不一致。")
    result: dict[str, dict[str, list[str]]] = {key: {} for key in (*CONTEXT_TABLES, "reports")}
    units: dict[str, list[str]] = {}
    for row in tables["text_units"]:
        refs = _refs(row.get("document_ids"), expected)
        unit_id = _identifier(row.get("id"))
        _insert(units, unit_id, refs)
        for key in ("id", "human_readable_id"):
            _insert(result["sources"], _identifier(row.get(key)), refs)
    if not units:
        raise _invalid("GraphRAG 未生成可追溯的源块。")
    for context in ("entities", "relationships"):
        for row in tables[context]:
            refs = _linked_sources(row.get("text_unit_ids"), units)
            for key in ("id", "human_readable_id"):
                _insert(result[context], _identifier(row.get(key)), refs)
    communities: dict[str, list[str]] = {}
    for row in tables["communities"]:
        refs = _linked_sources(row.get("text_unit_ids"), units)
        _insert(communities, _identifier(row.get("community")), refs)
    for row in tables["community_reports"]:
        community = _identifier(row.get("community"))
        if community not in communities:
            raise _invalid("GraphRAG 社区报告引用了未知社区。")
        # read_indexer_reports sets short_id_col='community', not human_readable_id.
        for identifier in (community, _identifier(row.get("id"))):
            _insert(result["reports"], identifier, communities[community])
    return result


def context_evidence(
    context: Mapping[str, Any],
    mapping: Mapping[str, Mapping[str, list[str]]],
    evidence: tuple[Evidence, ...],
    *,
    method: str,
    limit: int,
) -> tuple[Evidence, ...]:
    if not isinstance(context, Mapping):
        raise _invalid("GraphRAG 返回的上下文不是 2.7.0 的字典契约。")
    known = {item.source_id: item for item in evidence}
    selected: dict[str, Evidence] = {}
    rank = 0
    for kind, value in context.items():
        records = _records(value)
        if not records:
            continue
        if kind not in mapping:
            raise _invalid("GraphRAG 返回了尚未建立来源映射的上下文类型。")
        for row in records:
            if "in_context" in row and not _in_context(row["in_context"]):
                continue
            identifier = _identifier(row.get("id"))
            if identifier not in mapping[kind]:
                raise _invalid("GraphRAG 上下文 ID 无法关联到目标快照来源。")
            refs = mapping[kind][identifier]
            if not refs or any(ref not in known for ref in refs):
                raise _invalid("GraphRAG 上下文引用了目标快照之外的来源。")
            rank += 1
            for source_id in refs:
                if source_id not in selected:
                    selected[source_id] = known[source_id].ranked(
                        1.0 / rank,
                        method,
                        ("Microsoft GraphRAG", f"{kind}:{identifier}"),
                    )
    return tuple(selected.values())[:limit]


def _records(value: Any) -> list[dict[str, Any]]:
    if hasattr(value, "to_dict"):
        value = value.to_dict(orient="records")
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise _invalid("GraphRAG 上下文表不是记录列表或 DataFrame。")
    return value


def _in_context(value: Any) -> bool:
    if hasattr(value, "item"):
        value = value.item()
    if type(value) is not bool:
        raise _invalid("GraphRAG in_context 标记必须是布尔值。")
    return value


def _identifier(value: Any) -> str:
    if hasattr(value, "item"):
        value = value.item()
    if type(value) is int:
        return str(value)
    if type(value) is float and isfinite(value) and value.is_integer():
        return str(int(value))
    if isinstance(value, str) and value.strip():
        return value
    raise _invalid("GraphRAG 来源标识为空或格式无效。")


def _refs(value: Any, known: Mapping[str, Any]) -> list[str]:
    if hasattr(value, "tolist"):
        value = value.tolist()
    if not isinstance(value, (list, tuple)) or not value:
        raise _invalid("GraphRAG 记录没有可追溯来源列表。")
    identifiers = {_identifier(item) for item in value}
    if any(identifier not in known for identifier in identifiers):
        raise _invalid("GraphRAG 记录引用了来源表之外的 ID。")
    return sorted(identifiers)


def _linked_sources(value: Any, known: Mapping[str, list[str]]) -> list[str]:
    return sorted({source for identifier in _refs(value, known) for source in known[identifier]})


def _insert(mapping: dict[str, list[str]], identifier: str, refs: list[str]) -> None:
    if identifier in mapping and mapping[identifier] != refs:
        raise _invalid("GraphRAG 来源 ID 出现歧义。")
    mapping[identifier] = refs


def _invalid(message: str) -> DomainError:
    return DomainError("graphrag_provenance_invalid", message, 502)
