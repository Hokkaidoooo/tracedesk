"""Comparable lexical and dense rankings over one immutable evidence corpus."""

import math
from collections import Counter

from ..evidence import Evidence
from ..retrieval import tokenize
from .embeddings import Vector


def lexical_scores(query: str, evidence: tuple[Evidence, ...]) -> dict[str, float]:
    terms = Counter(tokenize(query))
    norm = math.sqrt(sum(value * value for value in terms.values()))
    if norm == 0:
        return {}
    scores = {}
    for item in evidence:
        document = Counter(tokenize(item.title + " " + item.text))
        numerator = sum(count * document.get(term, 0) for term, count in terms.items())
        if numerator:
            length = math.sqrt(sum(value * value for value in document.values()))
            scores[item.source_id] = numerator / (norm * length)
    return scores


def semantic_scores(query: Vector, vectors: dict[str, Vector]) -> dict[str, float]:
    return {
        source: max(
            -1.0, min(1.0, sum(left * right for left, right in zip(query, vector, strict=True)))
        )
        for source, vector in vectors.items()
    }


def ordered(scores: dict[str, float]) -> list[str]:
    return sorted(scores, key=lambda source: (-scores[source], source))


def fuse_rankings(*rankings: dict[str, float], rrf_k: int) -> dict[str, float]:
    result: dict[str, float] = {}
    for scores in rankings:
        for rank, source in enumerate(ordered(scores), start=1):
            result[source] = result.get(source, 0.0) + 1 / (rrf_k + rank)
    return result
