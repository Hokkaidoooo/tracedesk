"""Shared, immutable evidence exchanged by document, graph and research services."""

from dataclasses import asdict, dataclass, replace
from math import isfinite
from typing import Any


@dataclass(frozen=True)
class ParsedBlock:
    text: str
    page: int | None = None
    kind: str = "text"

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("Parsed text must not be empty.")
        if self.page is not None and (type(self.page) is not int or self.page < 1):
            raise ValueError("Page numbers must be positive integers when available.")
        if self.kind not in {"text", "table"}:
            raise ValueError("Unknown parsed block kind.")


@dataclass(frozen=True)
class Evidence:
    source_id: str
    tenant_id: str
    snapshot_id: str
    document_id: str
    document_version: int
    title: str
    text: str
    source_sha256: str
    page: int | None = None
    kind: str = "text"
    score: float | None = None
    method: str = "source"
    graph_path: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        required = (
            self.source_id,
            self.tenant_id,
            self.snapshot_id,
            self.document_id,
            self.title,
            self.text,
            self.source_sha256,
        )
        if any(not isinstance(value, str) or not value.strip() for value in required):
            raise ValueError("Evidence identity, scope and content must be explicit.")
        if type(self.document_version) is not int or self.document_version < 1:
            raise ValueError("Document versions start at one.")
        if self.page is not None and (type(self.page) is not int or self.page < 1):
            raise ValueError("Page numbers must be positive integers when available.")
        if len(self.source_sha256) != 64 or any(
            c not in "0123456789abcdef" for c in self.source_sha256
        ):
            raise ValueError("Evidence must identify the original source SHA-256.")
        if self.score is not None and not isfinite(self.score):
            raise ValueError("Evidence scores must be finite.")

    def ranked(self, score: float, method: str, graph_path: tuple[str, ...] = ()) -> "Evidence":
        return replace(self, score=score, method=method, graph_path=graph_path)

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["quote"] = self.text
        result["graph_path"] = list(self.graph_path)
        return result


@dataclass(frozen=True)
class GraphFact:
    subject: str
    predicate: str
    object: str
    source_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (
                self.subject,
                self.predicate,
                self.object,
            )
        ):
            raise ValueError("A graph fact requires both entities and a relation.")
        if not self.source_ids or len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("Graph facts require distinct source references.")

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "source_ids": list(self.source_ids)}
