"""A graph is a checked projection of one immutable evidence snapshot."""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from ..errors import DomainError
from ..evidence import Evidence, GraphFact
from .config import GraphConfig


def normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def occurs(entity: str, text: str) -> bool:
    name = normalize(entity)
    left = r"(?<![a-z0-9_])" if re.match(r"[a-z0-9_]", name[0]) else ""
    right = r"(?![a-z0-9_])" if re.match(r"[a-z0-9_]", name[-1]) else ""
    return re.search(left + re.escape(name) + right, normalize(text)) is not None


def digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Projection:
    tenant_id: str
    snapshot_id: str
    source_digest: str
    facts: tuple[GraphFact, ...]
    evidence: tuple[Evidence, ...]

    @property
    def nodes(self) -> tuple[str, ...]:
        return tuple(sorted({node for fact in self.facts for node in (fact.subject, fact.object)}))

    @property
    def predicates(self) -> tuple[str, ...]:
        return tuple(sorted({fact.predicate for fact in self.facts}))

    @property
    def projection_digest(self) -> str:
        return digest(
            {"sources": self.source_digest, "facts": [fact.as_dict() for fact in self.facts]}
        )

    def scope(self) -> dict[str, str]:
        return {"tenant_id": self.tenant_id, "snapshot_id": self.snapshot_id}

    def artifact(self, *, extraction: str, backend: str) -> dict[str, Any]:
        return {
            "schema": 1,
            **self.scope(),
            "source_digest": self.source_digest,
            "projection_digest": self.projection_digest,
            "extraction": extraction,
            "state": "ready" if self.facts else "empty",
            "backend": backend,
            "facts": [fact.as_dict() for fact in self.facts],
            "fact_count": len(self.facts),
            "neo4j": None,
            "graphrag": None,
        }


def build_projection(
    evidence: tuple[Evidence, ...],
    facts: tuple[GraphFact, ...],
    config: GraphConfig,
) -> Projection:
    if not evidence:
        raise DomainError("graph_scope_missing", "图索引必须绑定非空证据快照。")
    source_map = {item.source_id: item for item in evidence}
    if len(source_map) != len(evidence):
        raise DomainError("graph_source_duplicate", "证据快照中出现重复来源 ID。")
    scope = {(item.tenant_id, item.snapshot_id) for item in evidence}
    if len(scope) != 1:
        raise DomainError("graph_scope_mismatch", "图索引不能混用商店或快照。", 403)
    if len(facts) > config.max_facts:
        raise DomainError("graph_build_budget", "图关系数量超过构建预算。")
    for fact in facts:
        _validate_fact(fact, source_map)
    grouped: dict[tuple[str, str, str], set[str]] = {}
    for fact in facts:
        grouped.setdefault((fact.subject, fact.predicate, fact.object), set()).update(
            fact.source_ids
        )
    ordered = tuple(GraphFact(*key, tuple(sorted(refs))) for key, refs in sorted(grouped.items()))
    source_records = [
        {
            key: value
            for key, value in item.as_dict().items()
            if key not in {"score", "method", "graph_path", "quote"}
        }
        for item in sorted(evidence, key=lambda item: item.source_id)
    ]
    tenant_id, snapshot_id = next(iter(scope))
    projection = Projection(tenant_id, snapshot_id, digest(source_records), ordered, evidence)
    if len(projection.nodes) > config.max_nodes:
        raise DomainError("graph_build_budget", "图实体数量超过构建预算。")
    return projection


def _validate_fact(fact: GraphFact, source_map: dict[str, Evidence]) -> None:
    if not isinstance(fact, GraphFact):
        raise DomainError("graph_fact_invalid", "图关系必须使用 GraphFact 类型。")
    if any(
        len(value) > 256 or any(ord(char) < 32 for char in value)
        for value in (fact.subject, fact.predicate, fact.object)
    ):
        raise DomainError("graph_fact_invalid", "图实体或关系名称过长或包含控制字符。")
    if not isinstance(fact.source_ids, tuple) or any(
        not isinstance(s, str) for s in fact.source_ids
    ):
        raise DomainError("graph_fact_invalid", "图关系来源必须是来源 ID 元组。")
    for source_id in fact.source_ids:
        if source_id not in source_map:
            raise DomainError("graph_source_unknown", "图关系引用了快照之外的来源。", 403)
        text = source_map[source_id].text
        if not occurs(fact.subject, text) or not occurs(fact.object, text):
            raise DomainError("graph_fact_unsupported", "关系来源原文未同时出现两端实体。")


def restore_projection(
    evidence: tuple[Evidence, ...],
    artifacts: dict[str, Any],
    config: GraphConfig,
) -> tuple[Projection, dict[str, Any]]:
    graph = artifacts.get("graph") if isinstance(artifacts, dict) else None
    if not isinstance(graph, dict) or graph.get("schema") != 1:
        raise DomainError("graph_artifact_invalid", "快照缺少支持的图索引。", 409)
    raw_facts = graph.get("facts")
    if not isinstance(raw_facts, list) or len(raw_facts) > config.max_facts:
        raise DomainError("graph_artifact_invalid", "图索引关系格式或数量无效。", 409)
    facts = tuple(_fact_from_record(record) for record in raw_facts)
    projection = build_projection(evidence, facts, config)
    expected = {
        **projection.scope(),
        "source_digest": projection.source_digest,
        "projection_digest": projection.projection_digest,
        "fact_count": len(projection.facts),
        "state": "ready" if projection.facts else "empty",
    }
    if any(graph.get(key) != value for key, value in expected.items()):
        raise DomainError("graph_scope_mismatch", "图索引与请求的证据快照不一致。", 403)
    if graph.get("extraction") not in {"explicit", "model"} or graph.get("backend") not in {
        "local",
        "neo4j",
    }:
        raise DomainError("graph_artifact_invalid", "图索引缺少明确的提取方式或存储后端。", 409)
    return projection, graph


def _fact_from_record(record: Any) -> GraphFact:
    if not isinstance(record, dict) or set(record) != {
        "subject",
        "predicate",
        "object",
        "source_ids",
    }:
        raise DomainError("graph_fact_invalid", "关系记录字段无效。")
    if not isinstance(record["source_ids"], list) or any(
        not isinstance(s, str) for s in record["source_ids"]
    ):
        raise DomainError("graph_fact_invalid", "关系来源必须为来源 ID 列表。")
    try:
        return GraphFact(
            record["subject"], record["predicate"], record["object"], tuple(record["source_ids"])
        )
    except ValueError as exc:
        raise DomainError("graph_fact_invalid", "关系记录内容无效。") from exc


def fact_id(fact: GraphFact) -> str:
    return digest(fact.as_dict())
