"""Semantic Cypher examples reuse the application's validated embedding and ranking stack."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files

from ..errors import DomainError
from ..knowledge.config import KnowledgeConfig
from ..knowledge.embeddings import Embedder, EmbeddingKind, Vector, validate_vectors
from ..knowledge.jsonio import load_json
from ..knowledge.ranking import ordered, semantic_scores
from .cypher import parse_cypher
from .text2cypher import MAX_RETRIEVED_EXAMPLES, CypherExample

MAX_CORPUS_EXAMPLES = 64
MAX_EXAMPLE_QUESTION_CHARS = 2000


def _validate_examples(examples: tuple[CypherExample, ...]) -> tuple[CypherExample, ...]:
    if not isinstance(examples, tuple) or not 1 <= len(examples) <= MAX_CORPUS_EXAMPLES:
        raise DomainError(
            "cypher_examples_invalid", "Cypher 示例集必须是有界、非空的不可变集合。", 500
        )
    for example in examples:
        if (
            not isinstance(example, CypherExample)
            or not isinstance(example.question, str)
            or not example.question.strip()
            or len(example.question) > MAX_EXAMPLE_QUESTION_CHARS
            or not isinstance(example.cypher, str)
        ):
            raise DomainError(
                "cypher_examples_invalid", "Cypher 示例必须包含有效的问题和查询。", 500
            )
        parse_cypher(example.cypher)
    if len({example.question for example in examples}) != len(examples):
        raise DomainError("cypher_examples_invalid", "Cypher 示例问题不能重复。", 500)
    return examples


def load_cypher_examples() -> tuple[CypherExample, ...]:
    resource = files("tracedesk").joinpath("data", "cypher-examples.json")
    try:
        payload = load_json(resource.read_bytes())
    except (OSError, ValueError) as exc:
        raise DomainError("cypher_examples_invalid", "内置 Cypher 示例资源无法读取。", 500) from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema", "origin", "examples"}
        or type(payload["schema"]) is not int
        or payload["schema"] != 1
        or payload["origin"] != "tracedesk-authored"
        or not isinstance(payload["examples"], list)
        or any(
            not isinstance(item, dict) or set(item) != {"question", "cypher"}
            for item in payload["examples"]
        )
    ):
        raise DomainError("cypher_examples_invalid", "内置 Cypher 示例资源不符合数据契约。", 500)
    return _validate_examples(tuple(CypherExample(**item) for item in payload["examples"]))


@dataclass(frozen=True)
class _ExampleIndex:
    identity: tuple[str, int]
    vectors: tuple[Vector, ...]


class SemanticCypherExamples:
    def __init__(
        self,
        embedder: Callable[[], Embedder],
        config: KnowledgeConfig,
        *,
        examples: tuple[CypherExample, ...] | None = None,
    ) -> None:
        # The assembly root supplies the same validated, cached getter used by document retrieval.
        self._embedder = embedder
        self.config = config
        self.examples = load_cypher_examples() if examples is None else _validate_examples(examples)
        self._index: _ExampleIndex | None = None
        self._index_lock = asyncio.Lock()

    def require_ready(self) -> None:
        self._checked_embedder()

    def _checked_embedder(self, expected: tuple[str, int] | None = None) -> Embedder:
        model = self._embedder()
        if expected is None and self._index is not None:
            expected = self._index.identity
        if expected is not None and (model.fingerprint, model.dimensions) != expected:
            raise DomainError(
                "cypher_examples_model_mismatch",
                "Cypher 示例向量与当前嵌入模型不一致，请重新创建服务。",
                409,
            )
        return model

    async def _embed(
        self,
        model: Embedder,
        texts: tuple[str, ...],
        kind: EmbeddingKind,
    ) -> tuple[Vector, ...]:
        dimensions = model.dimensions
        try:
            raw = await asyncio.wait_for(
                model.embed(texts, kind=kind),
                self.config.embedding_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise DomainError("EMBEDDING_TIMEOUT", "Cypher 语义示例嵌入超时。", 504) from exc
        return validate_vectors(raw, len(texts), dimensions)

    async def _index_examples(self) -> _ExampleIndex:
        async with self._index_lock:
            model = self._checked_embedder()
            if self._index is not None:
                return self._index
            identity = (model.fingerprint, model.dimensions)
            vectors = []
            size = self.config.embedding_batch_size
            for start in range(0, len(self.examples), size):
                questions = tuple(item.question for item in self.examples[start : start + size])
                vectors.extend(await self._embed(model, questions, "document"))
                self._checked_embedder(identity)
            self._index = _ExampleIndex(identity, tuple(vectors))
            return self._index

    async def retrieve(self, query: str, *, limit: int) -> tuple[CypherExample, ...]:
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query) > self.config.max_query_characters
        ):
            raise DomainError(
                "cypher_examples_query_invalid", "语义示例查询为空或超过长度预算。", 422
            )
        if type(limit) is not int or not 1 <= limit <= MAX_RETRIEVED_EXAMPLES:
            raise DomainError("cypher_examples_budget", "语义示例召回数量超过预算。", 422)
        index = await self._index_examples()
        model = self._checked_embedder(index.identity)
        query_vector = (await self._embed(model, (query,), "query"))[0]
        self._checked_embedder(index.identity)
        vectors = {f"{position:03}": vector for position, vector in enumerate(index.vectors)}
        selected = ordered(semantic_scores(query_vector, vectors))[:limit]
        return tuple(self.examples[int(position)] for position in selected)
