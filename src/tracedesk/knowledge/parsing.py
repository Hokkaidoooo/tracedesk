"""Text parsing plus explicit MinerU 2.x HTTP and official flash CLI adapters."""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol

import httpx

from ..errors import DomainError
from ..evidence import ParsedBlock
from .chunks import markdown_blocks
from .config import KnowledgeConfig, service_url
from .jsonio import load_json


class DocumentParser(Protocol):
    name: str

    async def parse(
        self, content: bytes, *, filename: str, media_type: str
    ) -> tuple[ParsedBlock, ...]: ...


class ConfiguredParser:
    def __init__(self, config: KnowledgeConfig) -> None:
        self.config = config
        self.name = "text-v1+" + config.pdf_parser

    async def parse(
        self, content: bytes, *, filename: str, media_type: str
    ) -> tuple[ParsedBlock, ...]:
        if media_type in {"text/plain", "text/markdown"}:
            try:
                text = content.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise DomainError(
                    "DOCUMENT_ENCODING", "文本文件必须使用 UTF-8 编码。", 422
                ) from exc
            if "\x00" in text:
                raise DomainError(
                    "DOCUMENT_ENCODING", "文本文件包含空字节，拒绝作为正文解析。", 422
                )
            return markdown_blocks(text)
        if not content.lstrip().startswith(b"%PDF-"):
            raise DomainError("DOCUMENT_FORMAT", "文件内容缺少 PDF 标识。", 422)
        if self.config.pdf_parser == "none":
            raise DomainError(
                "PARSER_NOT_CONFIGURED", "PDF 解析器尚未配置，请设置 MinerU HTTP 或官方 CLI。", 503
            )
        parser: DocumentParser
        if self.config.pdf_parser == "mineru_http":
            parser = MinerUHTTPParser(self.config)
        else:
            parser = MinerUCLIParser(self.config)
        return await parser.parse(content, filename=filename, media_type=media_type)


class MinerUHTTPParser:
    """The /file_parse protocol verified against mineru-2.7.6-released.

    This deliberately rejects the incompatible MinerU 4.x /v1 protocol.
    """

    name = "mineru-http-content-list-v2"

    def __init__(
        self, config: KnowledgeConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.config = config
        self.url = service_url(config.mineru_base_url, "mineru_base_url") + "/file_parse"
        self.transport = transport

    async def parse(
        self, content: bytes, *, filename: str, media_type: str
    ) -> tuple[ParsedBlock, ...]:
        data = {
            "backend": self.config.mineru_backend,
            "lang_list": self.config.mineru_language,
            "parse_method": "auto",
            "table_enable": "true",
            "formula_enable": "true",
            "return_md": "false",
            "return_content_list": "true",
            "return_images": "false",
            "response_format_zip": "false",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.config.parse_timeout_seconds,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    self.url, data=data, files={"files": ("source.pdf", content, "application/pdf")}
                )
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise DomainError("PARSER_TIMEOUT", "MinerU 解析超时。", 504) from exc
        except httpx.HTTPError as exc:
            raise DomainError("PARSER_UNAVAILABLE", "MinerU HTTP 请求失败。", 502) from exc
        try:
            payload = load_json(response.content)
            return self._content_list(payload)
        except (ValueError, TypeError, KeyError) as exc:
            raise DomainError(
                "PARSER_PROTOCOL", "MinerU 响应不符合 2.x content_list 协议。", 502
            ) from exc

    @staticmethod
    def _content_list(payload: object) -> tuple[ParsedBlock, ...]:
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("version"), str)
            or not payload["version"].startswith("2.")
        ):
            raise ValueError("MinerU 2.x version is required.")
        results = payload.get("results")
        if not isinstance(results, dict) or set(results) != {"source"}:
            raise ValueError("Response must identify exactly the submitted source.")
        raw = results["source"]["content_list"]
        # Official 2.7.6 serializes the content-list file as a JSON string.
        if not isinstance(raw, str):
            raise ValueError("content_list must be a serialized JSON array.")
        items = load_json(raw)
        if not isinstance(items, list):
            raise ValueError("content_list must contain an array.")
        blocks = []
        for item in items:
            blocks.extend(_mineru_item(item))
        return tuple(blocks)


def _strings(item: dict, key: str) -> list[str]:
    values = item.get(key, [])
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError(f"{key} must be a list of strings.")
    return [value for value in values if value.strip()]


def _required_text(item: dict, key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must contain extracted text.")
    return value


def _mineru_item(item: object) -> list[ParsedBlock]:
    if not isinstance(item, dict):
        raise ValueError("MinerU content items must be objects.")
    page_index = item.get("page_idx")
    if type(page_index) is not int or page_index < 0:
        raise ValueError("MinerU page_idx must be a zero-based nonnegative integer.")
    page = page_index + 1
    kind = item.get("type")
    if kind in {"text", "discarded"}:
        return [ParsedBlock(_required_text(item, "text"), page=page)]
    if kind == "equation":
        if not item.get("text"):
            raise ValueError("An equation image has no extracted text.")
        return [ParsedBlock(_required_text(item, "text"), page=page)]
    if kind == "table":
        caption = [ParsedBlock(text, page=page) for text in _strings(item, "table_caption")]
        body = ParsedBlock(_required_text(item, "table_body"), page=page, kind="table")
        footnotes = [ParsedBlock(text, page=page) for text in _strings(item, "table_footnote")]
        return [*caption, body, *footnotes]
    if kind == "image":
        # Images are not visual evidence; only explicitly extracted captions are indexed.
        return [
            ParsedBlock(text, page=page)
            for text in [*_strings(item, "image_caption"), *_strings(item, "image_footnote")]
        ]
    raise ValueError(f"Unsupported MinerU content type: {kind}")


class MinerUCLIParser:
    """Official token-free flash extraction, with Markdown-only provenance."""

    name = "mineru-flash-markdown-v1"

    def __init__(self, config: KnowledgeConfig) -> None:
        self.config = config

    async def parse(
        self, content: bytes, *, filename: str, media_type: str
    ) -> tuple[ParsedBlock, ...]:
        executable = Path(self.config.mineru_cli_path)
        if not executable.is_absolute() or not executable.is_file():
            raise DomainError(
                "PARSER_NOT_CONFIGURED",
                "mineru_cli_path 必须指向已安装的官方 CLI 可执行文件。",
                503,
            )
        if len(content) > 10 * 1024 * 1024:
            raise DomainError(
                "PARSER_FILE_LIMIT", "MinerU flash-extract 只支持不超过 10 MB 的文件。", 413
            )
        with tempfile.TemporaryDirectory(prefix="tracedesk-mineru-") as directory:
            root = Path(directory)
            source = root / "source.pdf"
            output = root / "source-mineru"
            output.mkdir()
            source.write_bytes(content)
            await self._run(executable, source, output)
            target = output / "source.md"
            if not target.is_file():
                raise DomainError("PARSER_PROTOCOL", "MinerU CLI 成功退出但未生成 Markdown。", 502)
            try:
                text = target.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise DomainError(
                    "PARSER_PROTOCOL", "MinerU CLI 输出不是 UTF-8 Markdown。", 502
                ) from exc
            return markdown_blocks(text)

    async def _run(self, executable: Path, source: Path, output: Path) -> None:
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        try:
            process = await asyncio.create_subprocess_exec(
                str(executable),
                "flash-extract",
                str(source),
                "-o",
                str(output) + os.sep,
                "--language",
                self.config.mineru_language,
                "--timeout",
                str(max(1, int(self.config.parse_timeout_seconds))),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **options,
            )
        except OSError as exc:
            raise DomainError("PARSER_UNAVAILABLE", "无法启动配置的 MinerU CLI。", 503) from exc
        try:
            await asyncio.wait_for(process.communicate(), timeout=self.config.parse_timeout_seconds)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                process.kill()
            await process.communicate()
            raise
        if process.returncode != 0:
            raise DomainError(
                "PARSER_UNAVAILABLE",
                "MinerU flash-extract 执行失败。",
                502,
                {"exit_code": process.returncode},
            )
