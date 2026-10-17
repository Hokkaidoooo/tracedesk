from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import httpx
import pytest

from tracedesk.errors import DomainError
from tracedesk.evidence import ParsedBlock
from tracedesk.knowledge import KnowledgeConfig, MinerUCLIParser, MinerUHTTPParser, OpenAIEmbedder
from tracedesk.knowledge.chunks import chunk_blocks, markdown_blocks
from tracedesk.knowledge.embeddings import OnnxBGEEmbedder, create_embedder
from tracedesk.knowledge.parsing import ConfiguredParser


def mineru_payload(items, *, version="2.7.6"):
    return {
        "version": version,
        "backend": "pipeline",
        "results": {"source": {"content_list": json.dumps(items)}},
    }


def test_mineru_http_uses_official_multipart_and_structured_pages() -> None:
    requests = []
    items = [
        {"type": "text", "text": "退款说明", "page_idx": 0},
        {
            "type": "table",
            "table_body": "<table><tr><td>七天</td></tr></table>",
            "table_caption": ["时限"],
            "table_footnote": [],
            "page_idx": 2,
        },
        {"type": "image", "image_caption": ["签收截图"], "image_footnote": [], "page_idx": 3},
    ]

    def handle(request):
        requests.append(request)
        assert request.url.path == "/file_parse"
        assert request.method == "POST"
        assert b'name="files"; filename="source.pdf"' in request.content
        assert b'name="return_content_list"\r\n\r\ntrue' in request.content
        assert b'name="backend"\r\n\r\npipeline' in request.content
        assert b"private-filename" not in request.content
        return httpx.Response(200, json=mineru_payload(items))

    parser = MinerUHTTPParser(
        KnowledgeConfig(pdf_parser="mineru_http", mineru_base_url="http://mineru.example"),
        transport=httpx.MockTransport(handle),
    )
    blocks = asyncio.run(
        parser.parse(b"%PDF-1.7", filename="private-filename.pdf", media_type="application/pdf")
    )
    assert [item.page for item in blocks] == [1, 3, 3, 4]
    assert [item.kind for item in blocks] == ["text", "text", "table", "text"]
    assert len(requests) == 1


@pytest.mark.parametrize(
    "payload",
    [
        mineru_payload([{"type": "text", "text": "正文", "page_idx": -1}]),
        mineru_payload([{"type": "text", "text": "正文", "page_idx": True}]),
        mineru_payload([{"type": "text", "text": "正文"}]),
        mineru_payload([{"type": "unknown", "text": "正文", "page_idx": 0}]),
        mineru_payload([{"type": "text", "text": "正文", "page_idx": 0}], version="4.0.0"),
        {"version": "2.7.6", "results": {"wrong-source": {"content_list": "[]"}}},
        {"version": "2.7.6", "results": {"source": {"content_list": []}}},
    ],
)
def test_mineru_rejects_unsupported_or_unbound_provenance(payload) -> None:
    parser = MinerUHTTPParser(
        KnowledgeConfig(pdf_parser="mineru_http", mineru_base_url="http://mineru.example"),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )
    with pytest.raises(DomainError) as caught:
        asyncio.run(parser.parse(b"%PDF-1.7", filename="doc.pdf", media_type="application/pdf"))
    assert caught.value.code == "PARSER_PROTOCOL"


@pytest.mark.parametrize("failure", ["timeout", "http", "non-json", "duplicate-json"])
def test_mineru_transport_failures_are_explicit(failure) -> None:
    def handle(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("fixture timeout", request=request)
        if failure == "http":
            return httpx.Response(503, text="upstream unavailable")
        if failure == "duplicate-json":
            return httpx.Response(200, content=b'{"version":"2.7.6","version":"2.7.6"}')
        return httpx.Response(200, text="not json")

    parser = MinerUHTTPParser(
        KnowledgeConfig(pdf_parser="mineru_http", mineru_base_url="http://mineru.example"),
        transport=httpx.MockTransport(handle),
    )
    with pytest.raises(DomainError) as caught:
        asyncio.run(parser.parse(b"%PDF-1.7", filename="doc.pdf", media_type="application/pdf"))
    assert (
        caught.value.code
        == {
            "timeout": "PARSER_TIMEOUT",
            "http": "PARSER_UNAVAILABLE",
            "non-json": "PARSER_PROTOCOL",
            "duplicate-json": "PARSER_PROTOCOL",
        }[failure]
    )


def test_cli_flash_markdown_has_no_invented_page_and_cleans_its_own_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    executable = tmp_path / "mineru-test.exe"
    executable.write_bytes(b"protocol fixture")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    workspaces = []

    class Process:
        returncode = 0

        async def communicate(self):
            return b"", b"fixture progress"

    async def launch(*arguments, **options):
        assert arguments[1] == "flash-extract"
        source = Path(arguments[2])
        assert source.read_bytes() == b"%PDF-1.7"
        output = Path(arguments[arguments.index("-o") + 1])
        (output / "source.md").write_text(
            "退款说明\n\n|项目|期限|\n|---|---|\n|退款|七天|", encoding="utf-8"
        )
        workspaces.append(source.parent)
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    parser = MinerUCLIParser(
        KnowledgeConfig(pdf_parser="mineru_cli", mineru_cli_path=str(executable))
    )
    blocks = asyncio.run(
        parser.parse(b"%PDF-1.7", filename="guide.pdf", media_type="application/pdf")
    )
    assert all(item.page is None for item in blocks)
    assert blocks[1].kind == "table"
    assert all(not root.exists() for root in workspaces)
    assert executable.exists()


@pytest.mark.parametrize("exit_code", [0, 7])
def test_cli_failure_or_missing_output_never_uses_a_fake_success(
    tmp_path: Path, monkeypatch, exit_code: int
) -> None:
    executable = tmp_path / "mineru-test.exe"
    executable.write_bytes(b"protocol fixture")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    class Process:
        returncode = exit_code

        async def communicate(self):
            return b"", b"private process diagnostic"

    async def launch(*arguments, **options):
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    parser = MinerUCLIParser(
        KnowledgeConfig(pdf_parser="mineru_cli", mineru_cli_path=str(executable))
    )
    with pytest.raises(DomainError) as caught:
        asyncio.run(parser.parse(b"%PDF-1.7", filename="guide.pdf", media_type="application/pdf"))
    assert caught.value.code == ("PARSER_PROTOCOL" if exit_code == 0 else "PARSER_UNAVAILABLE")
    assert "private process diagnostic" not in str(caught.value)


def openai_config():
    return KnowledgeConfig(
        embedding_provider="openai",
        embedding_base_url="http://embed.example/v1",
        embedding_model="fixture-model",
        embedding_dimensions=2,
        embedding_api_key="fixture-key",
        embedding_query_prefix="query: ",
    )


def test_embedding_http_payload_identity_and_normalization() -> None:
    def handle(request):
        assert request.url.path == "/v1/embeddings"
        assert request.headers["authorization"] == "Bearer fixture-key"
        assert json.loads(request.content) == {
            "model": "fixture-model",
            "input": ["query: 退款"],
            "encoding_format": "float",
        }
        return httpx.Response(
            200,
            json={
                "model": "fixture-model",
                "data": [{"index": 0, "embedding": [3.0, 4.0]}],
            },
        )

    config = openai_config()
    embedder = OpenAIEmbedder(config, transport=httpx.MockTransport(handle))
    assert asyncio.run(embedder.embed(("退款",), kind="query")) == ((0.6, 0.8),)
    assert len(embedder.fingerprint) == 64
    assert "fixture-key" not in repr(config)


@pytest.mark.parametrize(
    "payload,code",
    [
        (
            {
                "model": "fixture-model",
                "data": [{"index": 1, "embedding": [1, 0]}, {"index": 0, "embedding": [0, 1]}],
            },
            "EMBEDDING_PROTOCOL",
        ),
        (
            {
                "model": "fixture-model",
                "data": [{"index": 0, "embedding": [1, 0]}, {"index": 0, "embedding": [0, 1]}],
            },
            "EMBEDDING_PROTOCOL",
        ),
        ({"model": "another-model", "data": []}, "EMBEDDING_PROTOCOL"),
        (
            {
                "model": "fixture-model",
                "data": [{"index": 0, "embedding": [1]}, {"index": 1, "embedding": [0, 1]}],
            },
            "EMBEDDING_DIMENSIONS",
        ),
        (
            {
                "model": "fixture-model",
                "data": [
                    {"index": 0, "embedding": [float("nan"), 0]},
                    {"index": 1, "embedding": [0, 1]},
                ],
            },
            "EMBEDDING_PROTOCOL",
        ),
        (
            {
                "model": "fixture-model",
                "data": [{"index": True, "embedding": [1, 0]}, {"index": 1, "embedding": [0, 1]}],
            },
            "EMBEDDING_PROTOCOL",
        ),
    ],
)
def test_embedding_rejects_wrong_order_dimension_and_nonfinite_values(payload, code) -> None:
    embedder = OpenAIEmbedder(
        openai_config(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=json.dumps(payload).encode())
        ),
    )
    with pytest.raises(DomainError) as caught:
        asyncio.run(embedder.embed(("退款", "发票")))
    assert caught.value.code == code


def test_embedding_missing_configuration_never_downloads_a_default_model(tmp_path: Path) -> None:
    with pytest.raises(DomainError) as caught:
        create_embedder(KnowledgeConfig())
    assert caught.value.code == "EMBEDDING_NOT_CONFIGURED"
    config = KnowledgeConfig(
        embedding_provider="onnx",
        embedding_model="explicit-bge",
        embedding_dimensions=512,
        onnx_model_path=str(tmp_path / "missing.onnx"),
        tokenizer_path=str(tmp_path / "missing.json"),
    )
    with pytest.raises(DomainError) as caught:
        OnnxBGEEmbedder(config)
    assert caught.value.code == "EMBEDDING_NOT_CONFIGURED"
    assert list(tmp_path.iterdir()) == []


def test_config_env_is_scoped_and_invalid_values_fail(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-fixture")
    monkeypatch.setenv("TRACEDESK_KNOWLEDGE_MAX_CHUNK_CHARACTERS", "350")
    config = KnowledgeConfig.from_env()
    assert config.max_chunk_characters == 350
    assert config.embedding_api_key == ""
    monkeypatch.setenv("TRACEDESK_KNOWLEDGE_EMBEDDING_PROVIDER", "guessed")
    with pytest.raises(ValueError):
        KnowledgeConfig.from_env()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_chunk_characters": 0},
        {"parse_timeout_seconds": float("inf")},
        {"embedding_provider": "openai", "embedding_model": "model", "embedding_dimensions": 3},
        {"pdf_parser": "mineru_http", "mineru_base_url": "http://user:secret@example.org"},
        {"pdf_parser": "mineru_cli"},
        {"embedding_dimensions": True},
    ],
)
def test_config_rejects_incomplete_and_unsafe_protocol_settings(kwargs) -> None:
    with pytest.raises(ValueError):
        KnowledgeConfig(**kwargs)


def test_table_chunks_keep_header_and_never_cut_rows_or_invent_pages() -> None:
    table = "|项目|说明|\n|---|---|\n" + "\n".join(f"|项目{i}|说明{i}|" for i in range(10))
    chunks = chunk_blocks((ParsedBlock(table, page=4, kind="table"),), 50)
    assert len(chunks) > 1
    assert all(chunk.text.startswith("|项目|说明|\n|---|---|") for chunk in chunks)
    assert all(chunk.kind == "table" and chunk.page == 4 for chunk in chunks)
    assert sum(chunk.text.count("|项目") - 1 for chunk in chunks) == 10
    assert all(len(chunk.text) <= 50 for chunk in chunks)


def test_html_table_and_markdown_code_keep_distinct_structures() -> None:
    html = "<table><tr><th>项目</th></tr><tr><td>退款</td></tr></table>"
    assert markdown_blocks(html)[0].kind == "table"
    fenced = "\x60\x60\x60text\n|项目|值|\n|---|---|\n\x60\x60\x60"
    assert all(block.kind == "text" for block in markdown_blocks(fenced))
    oversized = ParsedBlock("|项目|说明|\n|---|---|\n|退款|" + "长" * 100 + "|", kind="table")
    with pytest.raises(DomainError) as caught:
        chunk_blocks((oversized,), 50)
    assert caught.value.code == "TABLE_ROW_TOO_LARGE"


@pytest.mark.parametrize(
    "content,code",
    [
        (b"\xff", "DOCUMENT_ENCODING"),
        (b"text\x00", "DOCUMENT_ENCODING"),
        (b"fake", "DOCUMENT_FORMAT"),
    ],
)
def test_text_and_pdf_format_failures_are_not_silently_reinterpreted(content, code) -> None:
    parser = ConfiguredParser(KnowledgeConfig())
    media = "application/pdf" if code == "DOCUMENT_FORMAT" else "text/plain"
    with pytest.raises(DomainError) as caught:
        asyncio.run(parser.parse(content, filename="guide", media_type=media))
    assert caught.value.code == code
