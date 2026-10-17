"""Embedding adapters with explicit model identity and strict vector validation."""

from __future__ import annotations

import asyncio
import hashlib
import math
from contextvars import copy_context
from pathlib import Path
from typing import Literal, Protocol

import httpx

from ..errors import DomainError
from .config import KnowledgeConfig, service_url
from .jsonio import dump_json, load_json

Vector = tuple[float, ...]
EmbeddingKind = Literal["document", "query"]


class Embedder(Protocol):
    fingerprint: str
    dimensions: int

    async def embed(
        self, texts: tuple[str, ...], *, kind: EmbeddingKind = "document"
    ) -> tuple[Vector, ...]: ...


def validate_vectors(raw: object, count: int, dimensions: int) -> tuple[Vector, ...]:
    if not isinstance(raw, (list, tuple)) or len(raw) != count:
        raise DomainError("EMBEDDING_PROTOCOL", "嵌入数量与输入数量不一致。", 502)
    vectors = []
    for vector in raw:
        if not isinstance(vector, (list, tuple)) or len(vector) != dimensions:
            raise DomainError("EMBEDDING_DIMENSIONS", "嵌入维度与配置不一致。", 502)
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in vector):
            raise DomainError("EMBEDDING_PROTOCOL", "嵌入包含非数值或非有限值。", 502)
        norm = math.hypot(*vector)
        if not math.isfinite(norm) or norm == 0:
            raise DomainError("EMBEDDING_PROTOCOL", "嵌入必须具有有限且非零的向量长度。", 502)
        vectors.append(tuple(float(value) / norm for value in vector))
    return tuple(vectors)


def _identity(payload: dict) -> str:
    return hashlib.sha256(dump_json(payload).encode("utf-8")).hexdigest()


def _inputs(texts: tuple[str, ...], kind: EmbeddingKind) -> None:
    if (
        not isinstance(texts, tuple)
        or not texts
        or any(not isinstance(text, str) or not text.strip() for text in texts)
    ):
        raise ValueError("Embedding inputs must be a nonempty tuple of nonempty strings.")
    if kind not in {"document", "query"}:
        raise ValueError("Embedding kind must be document or query.")


class OpenAIEmbedder:
    def __init__(
        self, config: KnowledgeConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.config = config
        self.dimensions = config.embedding_dimensions
        self.url = service_url(config.embedding_base_url, "embedding_base_url") + "/embeddings"
        self.transport = transport
        self.fingerprint = _identity(
            {
                "provider": "openai-compatible",
                "url": self.url,
                "model": config.embedding_model,
                "dimensions": self.dimensions,
                "query_prefix": config.embedding_query_prefix,
            }
        )

    async def embed(
        self, texts: tuple[str, ...], *, kind: EmbeddingKind = "document"
    ) -> tuple[Vector, ...]:
        _inputs(texts, kind)
        inputs = [
            self.config.embedding_query_prefix + text if kind == "query" else text for text in texts
        ]
        headers = {}
        if self.config.embedding_api_key:
            headers["Authorization"] = "Bearer " + self.config.embedding_api_key
        try:
            async with httpx.AsyncClient(
                timeout=self.config.embedding_timeout_seconds,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    self.url,
                    headers=headers,
                    json={
                        "model": self.config.embedding_model,
                        "input": inputs,
                        "encoding_format": "float",
                    },
                )
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise DomainError("EMBEDDING_TIMEOUT", "嵌入服务调用超时。", 504) from exc
        except httpx.HTTPError as exc:
            raise DomainError("EMBEDDING_UNAVAILABLE", "嵌入服务 HTTP 请求失败。", 502) from exc
        try:
            payload = load_json(response.content)
            if not isinstance(payload, dict) or payload.get("model") != self.config.embedding_model:
                raise ValueError("Embedding response model differs from the requested model.")
            data = payload["data"]
            if not isinstance(data, list) or len(data) != len(texts):
                raise ValueError("Embedding response count mismatch.")
            for index, item in enumerate(data):
                if (
                    not isinstance(item, dict)
                    or type(item.get("index")) is not int
                    or item["index"] != index
                ):
                    raise ValueError("Embedding items must be in input order with exact indices.")
            return validate_vectors(
                [item["embedding"] for item in data], len(texts), self.dimensions
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DomainError(
                "EMBEDDING_PROTOCOL", "嵌入响应的模型、顺序或 JSON 结构无效。", 502
            ) from exc


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for piece in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(piece)
    return digest.hexdigest()


class _OnnxRunOptions(Protocol):
    terminate: bool


def _check_onnx_cancelled(options: _OnnxRunOptions) -> None:
    if options.terminate:
        raise asyncio.CancelledError("The ONNX embedding call was cancelled.")


async def _wait_for_onnx_exit(worker: asyncio.Future[tuple[Vector, ...]]) -> None:
    exited = asyncio.Event()
    worker.add_done_callback(lambda _: exited.set())
    while not exited.is_set():
        try:
            await exited.wait()
        except asyncio.CancelledError:
            # Repeated cancellation must not release a still-running native call.
            continue


class OnnxBGEEmbedder:
    """Local BGE encoder: tokenizer.json, CLS pooling and L2 normalization."""

    def __init__(self, config: KnowledgeConfig) -> None:
        self.config = config
        self.dimensions = config.embedding_dimensions
        model_path = Path(config.onnx_model_path)
        tokenizer_path = Path(config.tokenizer_path)
        if (
            not model_path.is_absolute()
            or not tokenizer_path.is_absolute()
            or not model_path.is_file()
            or not tokenizer_path.is_file()
        ):
            raise DomainError(
                "EMBEDDING_NOT_CONFIGURED",
                "ONNX 模型和 tokenizer.json 必须是已存在的绝对路径。",
                503,
            )
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise DomainError(
                "EMBEDDING_DEPENDENCY", "本地嵌入需要安装 onnxruntime、numpy 和 tokenizers。", 503
            ) from exc
        self._np = np
        self._run_options = ort.RunOptions
        self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        pad_id = self._tokenizer.token_to_id("[PAD]")
        self._cls_id = self._tokenizer.token_to_id("[CLS]")
        if pad_id is None or self._cls_id is None:
            raise DomainError(
                "EMBEDDING_MODEL_FORMAT", "BGE tokenizer 必须定义 [PAD] 和 [CLS]。", 422
            )
        self._tokenizer.enable_padding(pad_id=pad_id, pad_token="[PAD]")
        self._tokenizer.enable_truncation(max_length=config.tokenizer_max_length)
        self._session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        self._input_names = {item.name: item.type for item in self._session.get_inputs()}
        if (
            not {"input_ids", "attention_mask"} <= self._input_names.keys()
            or not self._input_names.keys() <= {"input_ids", "attention_mask", "token_type_ids"}
            or any(
                kind not in {"tensor(int64)", "tensor(int32)"}
                for kind in self._input_names.values()
            )
        ):
            raise DomainError("EMBEDDING_MODEL_FORMAT", "ONNX 输入不符合 BGE 编码器协议。", 422)
        if "last_hidden_state" not in {item.name for item in self._session.get_outputs()}:
            raise DomainError(
                "EMBEDDING_MODEL_FORMAT", "ONNX 模型必须输出 last_hidden_state。", 422
            )
        self.fingerprint = _identity(
            {
                "provider": "onnx-bge-cls",
                "model": config.embedding_model,
                "model_sha256": _file_digest(model_path),
                "tokenizer_sha256": _file_digest(tokenizer_path),
                "dimensions": self.dimensions,
                "max_length": config.tokenizer_max_length,
                "query_prefix": config.embedding_query_prefix,
            }
        )

    async def embed(
        self, texts: tuple[str, ...], *, kind: EmbeddingKind = "document"
    ) -> tuple[Vector, ...]:
        _inputs(texts, kind)
        options = self._run_options()
        # A separate Task could be cancelled by loop shutdown before its thread has exited.
        worker = asyncio.get_running_loop().run_in_executor(
            None, copy_context().run, self._encode, texts, kind, options
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError as cancelled:
            error_to_raise: BaseException = cancelled
            try:
                options.terminate = True
            except Exception as error:
                error_to_raise = error
            finally:
                await _wait_for_onnx_exit(worker)
            worker_error = None if worker.cancelled() else worker.exception()
            if worker_error is error_to_raise:
                worker_error = None
            raise error_to_raise from worker_error

    def _encode(
        self, texts: tuple[str, ...], kind: EmbeddingKind, options: _OnnxRunOptions
    ) -> tuple[Vector, ...]:
        _check_onnx_cancelled(options)
        prepared = [
            self.config.embedding_query_prefix + text if kind == "query" else text for text in texts
        ]
        encoded = self._tokenizer.encode_batch(prepared)
        _check_onnx_cancelled(options)
        if any(item.overflowing for item in encoded):
            raise DomainError(
                "EMBEDDING_INPUT_TOO_LONG",
                "文本超过模型 token 上限，请减小分块长度或查询长度。",
                422,
            )
        if any(not item.ids or item.ids[0] != self._cls_id for item in encoded):
            raise DomainError("EMBEDDING_MODEL_FORMAT", "tokenizer 未在首位生成 [CLS]。", 422)
        values = {
            "input_ids": [item.ids for item in encoded],
            "attention_mask": [item.attention_mask for item in encoded],
            "token_type_ids": [item.type_ids for item in encoded],
        }
        inputs = {
            name: self._np.asarray(
                values[name], dtype=self._np.int64 if kind == "tensor(int64)" else self._np.int32
            )
            for name, kind in self._input_names.items()
        }
        _check_onnx_cancelled(options)
        hidden = self._session.run(["last_hidden_state"], inputs, run_options=options)[0]
        _check_onnx_cancelled(options)
        if hidden.ndim != 3 or hidden.shape[0] != len(texts) or hidden.shape[2] != self.dimensions:
            raise DomainError("EMBEDDING_DIMENSIONS", "ONNX 输出形状与配置不一致。", 502)
        vectors = validate_vectors(hidden[:, 0, :].tolist(), len(texts), self.dimensions)
        _check_onnx_cancelled(options)
        return vectors


def create_embedder(config: KnowledgeConfig) -> Embedder:
    if config.embedding_provider == "openai":
        return OpenAIEmbedder(config)
    if config.embedding_provider == "onnx":
        return OnnxBGEEmbedder(config)
    raise DomainError("EMBEDDING_NOT_CONFIGURED", "语义检索需要显式配置嵌入模型。", 503)
