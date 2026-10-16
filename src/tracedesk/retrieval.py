"""Deterministic, tenant-scoped retrieval over versioned policy documents."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, fields
from datetime import date
from pathlib import Path
from string import Formatter

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_SPANS = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+|[a-z0-9]+")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_BOILERPLATE = re.compile(
    r"可不可以|能不能|有没有|会不会|是不是|什么时候|为什么|"
    r"请问|您好|你好|麻烦|帮我|我想|想问|可以|是否|如何|怎么|什么|一下|现在"
)
_EDGE_PARTICLES = "我你您他她它的了呢吗呀啊吧能要还想会着"
_STOP_TERMS = frozenset({"多少", "多久", "时候", "需要", "进行", "这个", "那个"})
_NGRAM_WEIGHTS = ((1, 0.2), (2, 1.0), (3, 0.5))
_RULE_LABELS = {
    "allowed_statuses": {
        "paid": "已付款",
        "processing": "备货中",
        "shipped": "运输中",
        "delivered": "已签收",
        "cancelled": "已取消",
    },
    "excluded_categories": {
        "physical": "实物商品",
        "digital": "数字商品",
        "custom": "定制商品",
    },
}
METHODS = ("bm25", "ngram", "hybrid")


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} must be a nonempty ASCII identifier (letters, digits, ._-)")
    return value


def _day(value: object, name: str) -> date:
    if type(value) is not date:
        raise ValueError(f"{name} must be a date, not a datetime or string")
    return value


def _parse_day(value: object, name: str) -> date:
    if not isinstance(value, str) or _ISO_DATE.fullmatch(value) is None:
        raise ValueError(f"{name} must be an ISO date in YYYY-MM-DD form")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} is not a valid calendar date: {value}") from exc


def _nonempty_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


class _FrozenRules(dict):
    """Keep a dict-compatible public interface without mutable policy state."""

    def _deny(self, *args: object, **kwargs: object) -> None:
        raise TypeError("document rules are immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _deny


def _freeze_json(value: object) -> object:
    if isinstance(value, dict):
        for key in value:
            _nonempty_text(key, "rule key")
        return _FrozenRules({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError("rules must contain only finite JSON values")


def _validate_returns_rules(rules: dict) -> None:
    expected = {"window_days", "allowed_statuses", "excluded_categories"}
    if set(rules) != expected:
        raise ValueError(f"returns rules must contain exactly {sorted(expected)}")
    window = rules["window_days"]
    if type(window) is not int or window <= 0:
        raise ValueError("returns.window_days must be a positive integer")
    for name, labels in _RULE_LABELS.items():
        values = rules[name]
        if not isinstance(values, (list, tuple)) or not values:
            raise ValueError(f"returns.{name} must be a nonempty array")
        if any(not isinstance(value, str) or value not in labels for value in values):
            raise ValueError(f"returns.{name} contains an unsupported value")
        if len(set(values)) != len(values):
            raise ValueError(f"returns.{name} contains duplicate values")


def _rule_text(name: str, value: object) -> str:
    if type(value) in (str, int, float, bool):
        return str(value)
    if isinstance(value, tuple) and name in _RULE_LABELS:
        labels = _RULE_LABELS[name]
        if any(not isinstance(item, str) or item not in labels for item in value):
            raise ValueError(f"template rule {name} contains an unsupported label")
        return "、".join(labels[item] for item in value)
    raise ValueError(f"template rule {name} must be a scalar or a supported label array")


def _render_policy(template: str, rules: dict) -> str:
    parts: list[str] = []
    for literal, name, format_spec, conversion in Formatter().parse(template):
        parts.append(literal)
        if name is None:
            continue
        if not name.isidentifier() or name not in rules:
            raise ValueError(f"unknown policy template field: {name!r}")
        if format_spec or conversion:
            raise ValueError("policy templates do not allow formatting or conversions")
        parts.append(_rule_text(name, rules[name]))
    return "".join(parts)


@dataclass(frozen=True)
class Document:
    id: str
    version: int
    title: str
    text: str
    category: str
    tenant_id: str
    effective_from: date
    effective_to: date | None
    rules: dict

    def __post_init__(self) -> None:
        _identifier(self.id, "document id")
        _identifier(self.category, "category")
        _identifier(self.tenant_id, "tenant_id")
        _nonempty_text(self.title, "title")
        _nonempty_text(self.text, "text")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("version must be a positive integer")
        _day(self.effective_from, "effective_from")
        if self.effective_to is not None:
            _day(self.effective_to, "effective_to")
            if self.effective_to <= self.effective_from:
                raise ValueError("effective_to must be later than effective_from")
        if not isinstance(self.rules, dict):
            raise ValueError("rules must be an object")
        if self.id == "returns":
            _validate_returns_rules(self.rules)
        immutable_rules = _freeze_json(self.rules)
        rendered = _render_policy(self.text, immutable_rules)
        _nonempty_text(rendered, "rendered policy text")
        object.__setattr__(self, "rules", immutable_rules)
        object.__setattr__(self, "text", rendered)

    @property
    def source_id(self) -> str:
        return f"{self.id}@{self.version}"


@dataclass(frozen=True)
class SearchHit:
    document: Document
    score: float
    bm25_score: float
    ngram_score: float


@dataclass(frozen=True)
class RetrievalConfig:
    k1: float = 1.2
    b: float = 0.75
    rrf_k: int = 60
    min_query_coverage: float = 0.25
    min_ngram_score: float = 0.05
    max_query_characters: int = 2000

    def __post_init__(self) -> None:
        for name in ("k1", "b", "min_query_coverage", "min_ngram_score"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f"{name} must be a finite number")
        if self.k1 <= 0 or not 0 <= self.b <= 1:
            raise ValueError("k1 must be positive and b must be in [0, 1]")
        for name in ("min_query_coverage", "min_ngram_score"):
            if not 0 <= getattr(self, name) <= 1:
                raise ValueError(f"{name} must be in [0, 1]")
        for name in ("rrf_k", "max_query_characters"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive integer")


def _segments(text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = _BOILERPLATE.sub(" ", normalized)
    result = []
    for match in _SPANS.finditer(normalized):
        span = match.group()
        if _CJK.match(span):
            span = span.strip(_EDGE_PARTICLES)
        if span:
            result.append(span)
    return tuple(result)


def tokenize(text: str) -> tuple[str, ...]:
    """CJK bigrams within spans, plus whole Latin/digit tokens; no learned model."""
    _nonempty_text(text, "tokenizer input")
    return _tokens_from_segments(_segments(text))


def _tokens_from_segments(segments: tuple[str, ...]) -> tuple[str, ...]:
    tokens: list[str] = []
    for span in segments:
        if _CJK.match(span) and len(span) > 1:
            tokens.extend(span[i : i + 2] for i in range(len(span) - 1))
        else:
            tokens.append(span)
    return tuple(token for token in tokens if token not in _STOP_TERMS)


def _ngram_features(segments: tuple[str, ...]) -> dict[str, float]:
    features: dict[str, float] = {}
    for span in segments:
        for width, weight in _NGRAM_WEIGHTS:
            for index in range(len(span) - width + 1):
                key = f"{width}:{span[index : index + width]}"
                features[key] = features.get(key, 0.0) + weight
    return features


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    numerator = sum(value * right.get(term, 0) for term, value in left.items())
    if not numerator:
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    return min(1.0, numerator / (left_norm * right_norm))


@dataclass(frozen=True)
class _IndexedDocument:
    document: Document
    terms: Counter[str]
    grams: dict[str, float]
    length: int


def _index_document(document: Document) -> _IndexedDocument:
    segments = _segments(f"{document.title} {document.text}")
    terms = Counter(_tokens_from_segments(segments))
    return _IndexedDocument(document, terms, _ngram_features(segments), sum(terms.values()))


def _bm25_scores(
    query_terms: frozenset[str],
    corpus: tuple[_IndexedDocument, ...],
    config: RetrievalConfig,
) -> dict[str, float]:
    average_length = sum(item.length for item in corpus) / len(corpus)
    if not average_length:
        return {item.document.source_id: 0.0 for item in corpus}
    document_frequency = Counter(
        term for item in corpus for term in query_terms if term in item.terms
    )
    inverse_frequency = {
        term: math.log(1 + (len(corpus) - count + 0.5) / (count + 0.5))
        for term, count in document_frequency.items()
    }
    result: dict[str, float] = {}
    for item in corpus:
        normalization = config.k1 * (1 - config.b + config.b * item.length / average_length)
        score = 0.0
        for term in sorted(query_terms):
            frequency = item.terms.get(term, 0)
            if frequency:
                score += (
                    inverse_frequency[term]
                    * frequency
                    * (config.k1 + 1)
                    / (frequency + normalization)
                )
        result[item.document.source_id] = score
    return result


def _rank(scores: dict[str, float]) -> list[str]:
    return sorted(
        (source for source, score in scores.items() if score > 0),
        key=lambda source: (-scores[source], source),
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _document_from_json(record: object) -> Document:
    expected = {field.name for field in fields(Document)}
    if not isinstance(record, dict) or set(record) != expected:
        raise ValueError(f"document fields must be exactly {sorted(expected)}")
    values = dict(record)
    values["effective_from"] = _parse_day(record["effective_from"], "effective_from")
    if record["effective_to"] is not None:
        values["effective_to"] = _parse_day(record["effective_to"], "effective_to")
    return Document(**values)


class KnowledgeBase:
    def __init__(self, documents: Iterable[Document], *, config: RetrievalConfig | None = None):
        if not isinstance(documents, Iterable) or isinstance(documents, (str, bytes, dict)):
            raise ValueError("documents must be an iterable of Document objects")
        if config is not None and not isinstance(config, RetrievalConfig):
            raise ValueError("config must be a RetrievalConfig")
        self.config = config if config is not None else RetrievalConfig()
        items = tuple(documents)
        if any(not isinstance(document, Document) for document in items):
            raise ValueError("every knowledge item must be a Document")
        self._validate_versions(items)
        self._documents = tuple(sorted(items, key=lambda document: document.source_id))
        self._index = tuple(_index_document(document) for document in self._documents)

    @property
    def documents(self) -> tuple[Document, ...]:
        return self._documents

    @classmethod
    def from_path(cls, path: str | Path, *, config: RetrievalConfig | None = None) -> KnowledgeBase:
        if not isinstance(path, (str, Path)) or (isinstance(path, str) and not path.strip()):
            raise ValueError("knowledge path must be a nonempty string or Path")
        raw = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(raw, dict) or set(raw) != {"schema_version", "documents"}:
            raise ValueError("knowledge JSON must contain exactly schema_version and documents")
        if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
            raise ValueError("unsupported knowledge schema_version; expected 1")
        if not isinstance(raw["documents"], list):
            raise ValueError("knowledge documents must be an array")
        documents = []
        for index, record in enumerate(raw["documents"], start=1):
            try:
                documents.append(_document_from_json(record))
            except ValueError as exc:
                raise ValueError(f"knowledge document {index}: {exc}") from exc
        return cls(documents, config=config)

    @staticmethod
    def _validate_versions(documents: tuple[Document, ...]) -> None:
        sources: set[str] = set()
        groups: dict[tuple[str, str], list[Document]] = {}
        for document in documents:
            if document.source_id in sources:
                raise ValueError(f"duplicate document source_id: {document.source_id}")
            sources.add(document.source_id)
            groups.setdefault((document.tenant_id, document.id), []).append(document)
        for versions in groups.values():
            ordered = sorted(versions, key=lambda item: item.effective_from)
            for previous, current in zip(ordered, ordered[1:], strict=False):
                if previous.effective_to is None or previous.effective_to > current.effective_from:
                    raise ValueError(
                        f"overlapping effective intervals: {previous.source_id}, {current.source_id}"
                    )

    def _eligible(self, tenant_id: str, as_of: date) -> tuple[_IndexedDocument, ...]:
        _identifier(tenant_id, "tenant_id")
        _day(as_of, "as_of")
        return tuple(
            item
            for item in self._index
            if item.document.tenant_id == tenant_id
            and item.document.effective_from <= as_of
            and (item.document.effective_to is None or as_of < item.document.effective_to)
        )

    def get(self, document_id: str, *, tenant_id: str, as_of: date) -> Document | None:
        _identifier(document_id, "document_id")
        return next(
            (
                item.document
                for item in self._eligible(tenant_id, as_of)
                if item.document.id == document_id
            ),
            None,
        )

    def search(
        self,
        query: str,
        *,
        tenant_id: str,
        as_of: date,
        top_k: int = 3,
        method: str = "hybrid",
    ) -> list[SearchHit]:
        _nonempty_text(query, "query")
        if len(query) > self.config.max_query_characters:
            raise ValueError(f"query exceeds {self.config.max_query_characters} characters")
        if type(top_k) is not int or top_k < 1:
            raise ValueError("top_k must be a positive integer")
        if method not in METHODS:
            raise ValueError(f"method must be one of {METHODS}")
        corpus = self._eligible(tenant_id, as_of)
        segments = _segments(query)
        query_terms = frozenset(_tokens_from_segments(segments))
        if not corpus or not query_terms:
            return []
        bm25 = _bm25_scores(query_terms, corpus, self.config)
        query_grams = _ngram_features(segments)
        ngram = {item.document.source_id: _cosine(query_grams, item.grams) for item in corpus}
        candidates = {
            item.document.source_id: item.document
            for item in corpus
            if len(query_terms.intersection(item.terms)) / len(query_terms)
            >= self.config.min_query_coverage
            and bm25[item.document.source_id] > 0
        }
        scores = self._method_scores(method, candidates, bm25, ngram)
        return [
            SearchHit(candidates[source], scores[source], bm25[source], ngram[source])
            for source in _rank(scores)[:top_k]
        ]

    def _method_scores(
        self,
        method: str,
        candidates: dict[str, Document],
        bm25: dict[str, float],
        ngram: dict[str, float],
    ) -> dict[str, float]:
        lexical = {source: bm25[source] for source in candidates}
        character = {
            source: ngram[source]
            for source in candidates
            if ngram[source] >= self.config.min_ngram_score
        }
        if method == "bm25":
            return lexical
        if method == "ngram":
            return character
        fused: dict[str, float] = {}
        for ranking in (_rank(lexical), _rank(character)):
            for rank, source in enumerate(ranking, start=1):
                fused[source] = fused.get(source, 0.0) + 1 / (self.config.rrf_k + rank)
        return fused
