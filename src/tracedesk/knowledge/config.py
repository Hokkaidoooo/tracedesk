"""Explicit, independently configurable document and embedding integrations."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from math import isfinite
from urllib.parse import urlsplit


def service_url(value: str, name: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{name} must be an HTTP(S) service URL without credentials or query.")
    return value.rstrip("/")


@dataclass(frozen=True)
class KnowledgeConfig:
    pdf_parser: str = "none"
    mineru_base_url: str = ""
    mineru_cli_path: str = ""
    mineru_backend: str = "pipeline"
    mineru_language: str = "ch"
    embedding_provider: str = "none"
    embedding_base_url: str = ""
    embedding_model: str = ""
    embedding_api_key: str = field(default="", repr=False)
    embedding_dimensions: int = 0
    embedding_batch_size: int = 32
    embedding_query_prefix: str = ""
    onnx_model_path: str = ""
    tokenizer_path: str = ""
    tokenizer_max_length: int = 512
    max_upload_bytes: int = 20 * 1024 * 1024
    max_chunk_characters: int = 1200
    parse_timeout_seconds: float = 120.0
    embedding_timeout_seconds: float = 60.0
    build_timeout_seconds: float = 300.0
    max_query_characters: int = 2000
    max_search_results: int = 50
    rrf_k: int = 60

    def __post_init__(self) -> None:
        if self.pdf_parser not in {"none", "mineru_http", "mineru_cli"}:
            raise ValueError("pdf_parser must be none, mineru_http or mineru_cli.")
        if self.embedding_provider not in {"none", "openai", "onnx"}:
            raise ValueError("embedding_provider must be none, openai or onnx.")
        for name in (
            "embedding_batch_size",
            "tokenizer_max_length",
            "max_upload_bytes",
            "max_chunk_characters",
            "max_query_characters",
            "max_search_results",
            "rrf_k",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive integer.")
        for name in (
            "parse_timeout_seconds",
            "embedding_timeout_seconds",
            "build_timeout_seconds",
        ):
            value = getattr(self, name)
            if type(value) not in (int, float) or not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number.")
        if type(self.embedding_dimensions) is not int or self.embedding_dimensions < 0:
            raise ValueError("embedding_dimensions must be a nonnegative integer.")
        if self.pdf_parser == "mineru_http":
            service_url(self.mineru_base_url, "mineru_base_url")
        if self.pdf_parser == "mineru_cli" and not self.mineru_cli_path.strip():
            raise ValueError("mineru_cli_path is required for the CLI parser.")
        if self.mineru_backend not in {
            "pipeline",
            "vlm-auto-engine",
            "hybrid-auto-engine",
        }:
            raise ValueError("mineru_backend must name a self-contained MinerU 2.x backend.")
        if not self.mineru_language.strip():
            raise ValueError("mineru_language must be explicit.")
        if self.embedding_provider == "none":
            return
        if not self.embedding_model.strip() or self.embedding_dimensions < 1:
            raise ValueError("An embedding model and its output dimensions must be configured.")
        if self.embedding_provider == "openai":
            service_url(self.embedding_base_url, "embedding_base_url")
        elif not self.onnx_model_path.strip() or not self.tokenizer_path.strip():
            raise ValueError("ONNX weights and tokenizer.json paths must be configured.")

    @classmethod
    def from_env(cls) -> KnowledgeConfig:
        defaults = cls()
        values = {}
        for item in fields(cls):
            raw = os.environ.get("TRACEDESK_KNOWLEDGE_" + item.name.upper())
            if raw is None:
                continue
            default = getattr(defaults, item.name)
            converter = type(default)
            try:
                values[item.name] = converter(raw)
            except ValueError as exc:
                raise ValueError(f"Invalid knowledge configuration: {item.name}.") from exc
        return cls(**values)
