"""Deterministic paragraph and table-row chunking without invented page numbers."""

from __future__ import annotations

import re

from ..errors import DomainError
from ..evidence import ParsedBlock

_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")


def markdown_blocks(text: str) -> tuple[ParsedBlock, ...]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    result: list[ParsedBlock] = []
    paragraph: list[str] = []
    index = 0
    fence = ""
    while index < len(lines):
        line = lines[index]
        fence_match = re.match(r"^\s*(\x60{3,}|~{3,})", line)
        if fence_match:
            marker = fence_match[1][0]
            fence = "" if fence == marker else marker
        if fence or fence_match:
            paragraph.append(line)
            index += 1
            continue
        if re.match(r"\s*<table\b", line, re.IGNORECASE):
            _flush_paragraph(paragraph, result)
            table = [line]
            index += 1
            while "</table>" not in table[-1].lower() and index < len(lines):
                table.append(lines[index])
                index += 1
            if "</table>" not in table[-1].lower():
                raise DomainError("TABLE_STRUCTURE", "Markdown 中的 HTML 表格未闭合。", 422)
            result.append(ParsedBlock("\n".join(table).strip(), kind="table"))
            continue
        if index + 1 < len(lines) and "|" in line and _TABLE_SEPARATOR.fullmatch(lines[index + 1]):
            _flush_paragraph(paragraph, result)
            table = [line, lines[index + 1]]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                table.append(lines[index])
                index += 1
            result.append(ParsedBlock("\n".join(table).strip(), kind="table"))
            continue
        if not line.strip():
            _flush_paragraph(paragraph, result)
        else:
            paragraph.append(line)
        index += 1
    _flush_paragraph(paragraph, result)
    return tuple(result)


def _flush_paragraph(paragraph: list[str], result: list[ParsedBlock]) -> None:
    if paragraph:
        result.append(ParsedBlock("\n".join(paragraph).strip()))
        paragraph.clear()


def chunk_blocks(blocks: tuple[ParsedBlock, ...], limit: int) -> tuple[ParsedBlock, ...]:
    if not isinstance(blocks, tuple) or any(not isinstance(block, ParsedBlock) for block in blocks):
        raise DomainError("PARSER_PROTOCOL", "解析器必须返回 ParsedBlock 元组。", 502)
    result = []
    for block in blocks:
        parts = (
            _table_parts(block.text, limit)
            if block.kind == "table"
            else _text_parts(block.text, limit)
        )
        result.extend(ParsedBlock(part, page=block.page, kind=block.kind) for part in parts)
    if not result:
        raise DomainError("DOCUMENT_EMPTY", "文档未解析出可检索的文本或表格。", 422)
    return tuple(result)


def _text_parts(text: str, limit: int) -> list[str]:
    remaining = text.strip()
    result = []
    while len(remaining) > limit:
        choices = [
            remaining.rfind(separator, limit // 2, limit) for separator in ("\n", "。", "；", " ")
        ]
        boundary = max(choices) + 1
        if boundary <= 0:
            boundary = limit
        result.append(remaining[:boundary].strip())
        remaining = remaining[boundary:].strip()
    if remaining:
        result.append(remaining)
    return result


def _table_parts(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    if re.match(r"\s*<table\b", text, re.IGNORECASE):
        if re.search(r"\browspan\s*=\s*['\"]?[2-9]", text, re.IGNORECASE):
            raise DomainError(
                "TABLE_STRUCTURE", "跨行 HTML 表格不能在行间切开，请增大分块上限。", 422
            )
        rows = re.findall(r"<tr\b[^>]*>.*?</tr\s*>", text, flags=re.IGNORECASE | re.DOTALL)
        if not rows:
            raise DomainError("TABLE_STRUCTURE", "表格缺少完整的行，无法安全分块。", 422)
        header = []
        for row in rows:
            if re.search(r"<th\b", row, re.IGNORECASE) is None:
                break
            header.append(row)
        return _group_rows(rows[len(header) :], header, limit, "<table>", "</table>")
    lines = text.splitlines()
    if len(lines) < 2 or _TABLE_SEPARATOR.fullmatch(lines[1]) is None:
        raise DomainError("TABLE_STRUCTURE", "表格过长且缺少可识别的行结构。", 422)
    return _group_rows(lines[2:], lines[:2], limit, "", "")


def _group_rows(
    rows: list[str], header: list[str], limit: int, prefix: str, suffix: str
) -> list[str]:
    result = []
    current = list(header)

    def render(items: list[str]) -> str:
        return prefix + "\n".join(items) + suffix

    for row in rows:
        if len(render([*header, row])) > limit:
            raise DomainError(
                "TABLE_ROW_TOO_LARGE", "表格单行超过分块上限，请增大 max_chunk_characters。", 422
            )
        if len(render([*current, row])) > limit:
            result.append(render(current))
            current = list(header)
        current.append(row)
    if len(current) > len(header) or not result:
        result.append(render(current))
    if any(not item.strip() or len(item) > limit for item in result):
        raise DomainError("TABLE_ROW_TOO_LARGE", "表格表头超过分块上限。", 422)
    return result
