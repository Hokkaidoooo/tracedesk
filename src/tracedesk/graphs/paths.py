"""Deterministic directed traversal with a hard expansion budget."""

from collections import deque
from dataclasses import dataclass

from ..errors import DomainError
from ..evidence import Evidence, GraphFact
from .config import GraphConfig
from .projection import Projection, fact_id, occurs


@dataclass(frozen=True)
class GraphPath:
    nodes: tuple[str, ...]
    facts: tuple[GraphFact, ...]

    def display(self) -> tuple[str, ...]:
        parts = [self.nodes[0]]
        for index, fact in enumerate(self.facts):
            direction = (
                f"-[{fact.predicate}]->"
                if fact.subject == self.nodes[index]
                else f"<-[{fact.predicate}]-"
            )
            parts.extend((direction, self.nodes[index + 1]))
        return tuple(parts)


def walk(
    projection: Projection,
    starts: tuple[str, ...],
    *,
    config: GraphConfig,
    max_hops: int,
    min_hops: int = 1,
    predicates: tuple[str, ...] | None = None,
    direction: str = "out",
    end: str | None = None,
) -> tuple[GraphPath, ...]:
    adjacency: dict[str, list[tuple[str, GraphFact]]] = {}
    for fact in projection.facts:
        if predicates is not None and fact.predicate not in predicates:
            continue
        if direction in {"out", "both"}:
            adjacency.setdefault(fact.subject, []).append((fact.object, fact))
        if direction in {"in", "both"}:
            adjacency.setdefault(fact.object, []).append((fact.subject, fact))
    queue = deque(GraphPath((start,), ()) for start in starts)
    result: list[GraphPath] = []
    expansions = 0
    while queue:
        path = queue.popleft()
        if len(path.facts) >= max_hops:
            continue
        for target, fact in adjacency.get(path.nodes[-1], ()):
            expansions += 1
            if expansions > config.max_expansions:
                raise DomainError(
                    "graph_query_budget", "图路径展开超过预算；请缩小问题或路径长度。", 422
                )
            if fact in path.facts:
                continue
            candidate = GraphPath((*path.nodes, target), (*path.facts, fact))
            if len(candidate.facts) >= min_hops and (end is None or target == end):
                result.append(candidate)
            queue.append(candidate)
    return tuple(result)


def query_paths(query: str, projection: Projection, config: GraphConfig) -> tuple[GraphPath, ...]:
    starts = tuple(node for node in projection.nodes if occurs(node, query))
    return walk(projection, starts, config=config, max_hops=config.max_hops)


def path_evidence(
    paths: tuple[GraphPath, ...],
    projection: Projection,
    *,
    method: str,
    limit: int,
) -> tuple[Evidence, ...]:
    sources = {item.source_id: item for item in projection.evidence}
    ranked: dict[str, Evidence] = {}
    for path in sorted(
        paths, key=lambda p: (len(p.facts), p.nodes, tuple(fact_id(f) for f in p.facts))
    ):
        score = 1.0 / len(path.facts)
        for fact in path.facts:
            for source_id in fact.source_ids:
                if source_id not in ranked:
                    ranked[source_id] = sources[source_id].ranked(score, method, path.display())
    return tuple(sorted(ranked.values(), key=lambda e: (-(e.score or 0), e.source_id))[:limit])
