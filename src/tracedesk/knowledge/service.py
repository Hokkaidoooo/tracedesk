"""Upload, version and publish evidence before any retrieval can consume it."""

from __future__ import annotations

import asyncio
import unicodedata
from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath, PureWindowsPath
from threading import Lock

from ..errors import DomainError
from ..evidence import Evidence
from ..store import Store
from .chunks import chunk_blocks
from .config import KnowledgeConfig
from .embeddings import Embedder, Vector, create_embedder, validate_vectors
from .jsonio import dump_json
from .parsing import ConfiguredParser, DocumentParser
from .ranking import fuse_rankings, lexical_scores, ordered, semantic_scores
from .repository import KnowledgeRepository

Enricher = Callable[[tuple[Evidence, ...]], Awaitable[dict]]
_MEDIA_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".pdf": "application/pdf",
}


class KnowledgeService:
    def __init__(
        self,
        store: Store,
        config: KnowledgeConfig,
        *,
        parser: DocumentParser | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        self.config = config
        self.repository = KnowledgeRepository(store)
        self.parser = parser if parser is not None else ConfiguredParser(config)
        self._injected_embedder = embedder
        self._owned_embedder: Embedder | None = None
        self._embedder_lock = Lock()

    async def close(self) -> None:
        # HTTP adapters own a request-scoped AsyncClient. Only the local model is retained.
        self._owned_embedder = None

    async def ingest(
        self, session: dict, *, filename: str, title: str, content: bytes, media_type: str
    ) -> dict:
        scope = dict(session)
        title, media_type = self._validate_upload(filename, title, content, media_type)
        document = self.repository.begin_document(
            scope,
            filename=filename,
            title=title,
            content=content,
            media_type=media_type,
            parser=self.parser.name,
            timeout_seconds=self.config.parse_timeout_seconds,
        )
        if document["idempotent"]:
            return document
        return await self._parse_document(scope, document, content)

    async def retry_document(self, session: dict, document_id: str, version: int) -> dict:
        scope = dict(session)
        original = self.original(scope, document_id, version)
        document = self.repository.retry_document(
            scope,
            document_id,
            version,
            parser=self.parser.name,
            timeout_seconds=self.config.parse_timeout_seconds,
        )
        return await self._parse_document(scope, document, original["content"])

    async def _parse_document(self, scope: dict, document: dict, content: bytes) -> dict:
        try:
            try:
                blocks = await asyncio.wait_for(
                    self.parser.parse(
                        content, filename=document["filename"], media_type=document["media_type"]
                    ),
                    timeout=self.config.parse_timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                raise DomainError("PARSER_TIMEOUT", "文档解析超时，原件已保留。", 504) from exc
            chunks = chunk_blocks(blocks, self.config.max_chunk_characters)
            return self.repository.complete_document(scope, document, chunks)
        except BaseException as exc:
            # Persist interrupted and unexpected failures, then preserve the original failure.
            self.repository.fail_document(scope, document, exc)
            raise

    def _validate_upload(
        self, filename: str, title: str, content: bytes, media_type: str
    ) -> tuple[str, str]:
        if (
            not isinstance(filename, str)
            or not filename.strip()
            or len(filename) > 255
            or any(ord(char) < 32 or ord(char) == 127 for char in filename)
            or PurePosixPath(filename).name != filename
            or PureWindowsPath(filename).name != filename
        ):
            raise DomainError("DOCUMENT_FILENAME", "文件名必须是不含路径的有效名称。", 422)
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise DomainError("DOCUMENT_TITLE", "文档标题必须包含 1 至 200 个字符。", 422)
        if not isinstance(content, bytes) or not content:
            raise DomainError("DOCUMENT_EMPTY", "上传文件不能为空。", 422)
        if len(content) > self.config.max_upload_bytes:
            raise DomainError("DOCUMENT_TOO_LARGE", "上传文件超过配置的大小上限。", 413)
        expected = _MEDIA_TYPES.get(PurePosixPath(filename).suffix.lower())
        supplied = (
            media_type.split(";", 1)[0].strip().lower() if isinstance(media_type, str) else ""
        )
        if expected is None or supplied not in {expected, "application/octet-stream"}:
            raise DomainError(
                "DOCUMENT_MEDIA_TYPE", "仅支持类型一致的 UTF-8 TXT、Markdown 和 PDF 文件。", 415
            )
        return unicodedata.normalize("NFC", title.strip()), expected

    def list_documents(self, session: dict) -> list[dict]:
        return self.repository.list_documents(session)

    def list_snapshots(self, session: dict) -> list[dict]:
        return self.repository.list_snapshots(session)

    def current_snapshot(self, session: dict) -> dict | None:
        return self.repository.current_snapshot(session)

    def original(self, session: dict, document_id: str, version: int) -> dict:
        if (
            not isinstance(document_id, str)
            or not document_id.strip()
            or type(version) is not int
            or version < 1
        ):
            raise DomainError("DOCUMENT_NOT_FOUND", "未找到当前商店的文档版本。", 404)
        return self.repository.original(session, document_id, version)

    def evidence(self, session: dict, snapshot_id: str | None = None) -> tuple[Evidence, ...]:
        return self.repository.read_snapshot(session, snapshot_id)[1]

    def _embedder(self) -> Embedder:
        if self._injected_embedder is not None:
            embedder = self._injected_embedder
        else:
            with self._embedder_lock:
                if self._owned_embedder is None:
                    self._owned_embedder = create_embedder(self.config)
                embedder = self._owned_embedder
        if (
            not isinstance(embedder.fingerprint, str)
            or not embedder.fingerprint.strip()
            or type(embedder.dimensions) is not int
            or embedder.dimensions < 1
        ):
            raise DomainError("EMBEDDING_PROTOCOL", "嵌入器必须声明模型指纹和正整数维度。", 502)
        return embedder

    def method_status(self, snapshot: dict) -> dict[str, dict]:
        """Inspect a bound snapshot without embedding queries or probing a remote service."""
        result = {"lexical": {"available": True, "error": None}}
        try:
            self._require_semantic(snapshot)
        except DomainError as error:
            for method in ("semantic", "hybrid"):
                result[method] = {"available": False, "error": error.as_dict()}
        else:
            for method in ("semantic", "hybrid"):
                result[method] = {"available": True, "error": None}
        return result

    def _require_semantic(self, snapshot: dict) -> Embedder:
        if not snapshot["with_embeddings"]:
            raise DomainError(
                "SEMANTIC_NOT_AVAILABLE", "该快照未构建向量，无法执行语义或混合检索。", 409
            )
        embedder = self._embedder()
        if embedder.fingerprint != snapshot["embedding_fingerprint"]:
            raise DomainError(
                "EMBEDDING_MODEL_MISMATCH", "当前嵌入模型与快照不一致，请重新构建索引。", 409
            )
        return embedder

    async def build_snapshot(
        self, session: dict, *, with_embeddings: bool = False, enricher: Enricher | None = None
    ) -> dict:
        if type(with_embeddings) is not bool:
            raise ValueError("with_embeddings must be a boolean.")
        scope = self.repository.scope(session)
        embedder = self._embedder() if with_embeddings else None
        snapshot, evidence = self.repository.begin_snapshot(
            scope,
            fingerprint=embedder.fingerprint if embedder is not None else None,
            timeout_seconds=self.config.build_timeout_seconds,
        )
        try:
            try:
                vectors, artifacts = await asyncio.wait_for(
                    self._build(evidence, embedder, enricher),
                    timeout=self.config.build_timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                raise DomainError(
                    "SNAPSHOT_TIMEOUT", "知识构建超时，已发布快照保持原版本。", 504
                ) from exc
            try:
                serialized = dump_json(artifacts)
            except ValueError as exc:
                raise DomainError(
                    "SNAPSHOT_ARTIFACTS", "附加索引包含无法保存的非 JSON 数据。", 502
                ) from exc
            return self.repository.publish_snapshot(
                scope, snapshot["id"], evidence, vectors, serialized
            )
        except BaseException as exc:
            self.repository.fail_snapshot(scope, snapshot["id"], exc)
            raise

    async def _build(
        self, evidence: tuple[Evidence, ...], embedder: Embedder | None, enricher: Enricher | None
    ) -> tuple[tuple[Vector, ...], dict]:
        vectors: tuple[Vector, ...] = ()
        if embedder is not None:
            batches = []
            size = self.config.embedding_batch_size
            for index in range(0, len(evidence), size):
                texts = tuple(item.text for item in evidence[index : index + size])
                raw = await asyncio.wait_for(
                    embedder.embed(texts, kind="document"),
                    timeout=self.config.embedding_timeout_seconds,
                )
                batches.extend(validate_vectors(raw, len(texts), embedder.dimensions))
            vectors = tuple(batches)
        artifacts = await enricher(evidence) if enricher is not None else {}
        if type(artifacts) is not dict:
            raise DomainError("SNAPSHOT_ARTIFACTS", "附加索引必须返回 JSON 对象。", 502)
        return vectors, artifacts

    async def search(
        self,
        session: dict,
        query: str,
        *,
        method: str = "lexical",
        snapshot_id: str | None = None,
        limit: int = 5,
    ) -> tuple[Evidence, ...]:
        self._validate_search(query, method, limit)
        scope = dict(session)
        snapshot, evidence = self.repository.read_snapshot(scope, snapshot_id)
        if snapshot is None:
            raise DomainError("KNOWLEDGE_NOT_PUBLISHED", "尚未发布知识快照，请先构建索引。", 409)
        lexical = lexical_scores(query, evidence)
        if method == "lexical":
            scores = lexical
        else:
            semantic = await self._semantic(scope, query, snapshot, evidence)
            scores = (
                semantic
                if method == "semantic"
                else fuse_rankings(lexical, semantic, rrf_k=self.config.rrf_k)
            )
        sources = {item.source_id: item for item in evidence}
        return tuple(
            sources[source].ranked(scores[source], method) for source in ordered(scores)[:limit]
        )

    def _validate_search(self, query: str, method: str, limit: int) -> None:
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query) > self.config.max_query_characters
        ):
            raise DomainError("KNOWLEDGE_QUERY", "查询不能为空且不能超过配置的长度上限。", 422)
        if not isinstance(method, str) or method not in {"lexical", "semantic", "hybrid"}:
            raise DomainError(
                "KNOWLEDGE_METHOD", "检索方式必须是 lexical、semantic 或 hybrid。", 422
            )
        if type(limit) is not int or not 1 <= limit <= self.config.max_search_results:
            raise DomainError("KNOWLEDGE_LIMIT", "检索结果数量超出配置范围。", 422)

    async def _semantic(
        self, session: dict, query: str, snapshot: dict, evidence: tuple[Evidence, ...]
    ) -> dict[str, float]:
        embedder = self._require_semantic(snapshot)
        vectors = self.repository.vectors(session, snapshot["id"], embedder.dimensions)
        if set(vectors) != {item.source_id for item in evidence}:
            raise DomainError("SOURCE_INTEGRITY", "向量来源与快照证据不一致。", 500)
        try:
            raw = await asyncio.wait_for(
                embedder.embed((query,), kind="query"),
                timeout=self.config.embedding_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise DomainError("EMBEDDING_TIMEOUT", "查询嵌入超时。", 504) from exc
        query_vector = validate_vectors(raw, 1, embedder.dimensions)[0]
        return semantic_scores(query_vector, vectors)
