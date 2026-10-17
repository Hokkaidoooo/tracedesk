"""CLI adapters call the same authenticated workflows as the HTTP API."""

import argparse
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from .bootstrap import Services
from .errors import DomainError
from .evidence import GraphFact
from .knowledge_evaluation import evaluate_knowledge
from .knowledge_workflows import ResearchRequest

EXTENDED_COMMANDS = {"knowledge", "research", "evaluate-knowledge"}
UPLOAD_MEDIA_TYPES = {".md": "text/markdown", ".txt": "text/plain", ".pdf": "application/pdf"}


def _scope_arguments(parser: argparse.ArgumentParser, *, defaults: bool = True) -> None:
    parser.add_argument(
        "--database",
        type=Path,
        default=None if defaults else argparse.SUPPRESS,
        help="SQLite 数据库路径；未指定时沿用环境配置",
    )
    parser.add_argument(
        "--customer",
        default="customer-a" if defaults else argparse.SUPPRESS,
        help="演示客户 ID，例如 customer-a 或 customer-b",
    )


def _method_list(value: str) -> tuple[str, ...]:
    methods = tuple(item.strip() for item in value.split(","))
    if any(not item for item in methods) or len(set(methods)) != len(methods):
        raise argparse.ArgumentTypeError("检索方法必须是逗号分隔的非空、不重复列表。")
    return methods


def add_commands(commands: Any) -> None:
    knowledge = commands.add_parser("knowledge", help="上传、发布与查询版本化知识")
    _scope_arguments(knowledge)
    actions = knowledge.add_subparsers(dest="knowledge_action", required=True)
    listing = actions.add_parser("list", help="列出文档、快照和实际可用检索方式")
    ingest = actions.add_parser("ingest", help="摄入一个 TXT、Markdown 或 PDF 原件")
    ingest.add_argument("file", type=Path)
    ingest.add_argument("--title", required=True)
    retry = actions.add_parser("retry", help="重试已保存版本的文档解析")
    retry.add_argument("document_id")
    retry.add_argument("version", type=int)
    build = actions.add_parser("build", help="构建并发布新快照")
    build.add_argument("--with-embeddings", action="store_true")
    build.add_argument("--with-graph", action="store_true")
    build.add_argument(
        "--relations", type=Path, help="GraphFact 数组 JSON，必须与 --with-graph 一起使用"
    )
    demo = actions.add_parser("load-demo", help="摄入四份虚构资料并发布快照，默认带人工示例关系")
    demo.add_argument("--with-embeddings", action="store_true")
    graph_flags = demo.add_mutually_exclusive_group()
    graph_flags.add_argument("--with-graph", action="store_true", dest="with_graph", default=True)
    graph_flags.add_argument("--without-graph", action="store_false", dest="with_graph")
    search = actions.add_parser("search", help="在同一固定快照中检索并输出原文证据")
    search.add_argument("query")
    search.add_argument("--method", default="lexical")
    search.add_argument("--snapshot-id")
    search.add_argument("--limit", type=int, default=5)
    for parser in (listing, ingest, retry, build, demo, search):
        _scope_arguments(parser, defaults=False)
    research = commands.add_parser("research", help="通过持久化会话运行单角色或多角色资料研究")
    research.add_argument("query")
    research.add_argument("--mode", choices=("single", "multi"), default="multi")
    research.add_argument("--snapshot-id")
    _scope_arguments(research)
    evaluate = commands.add_parser("evaluate-knowledge", help="比较固定快照上的知识检索方法")
    evaluate.add_argument(
        "--output", type=Path, default=Path("reports") / "knowledge-evaluation.json"
    )
    evaluate.add_argument(
        "--methods",
        type=_method_list,
        help="例如 lexical,semantic,hybrid,graph；默认取实际可用方法",
    )
    evaluate.add_argument("--snapshot-id")
    _scope_arguments(evaluate)


def _read_file(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise DomainError("FILE_TOO_LARGE", "文件超过当前上传大小限制。", 413)
    return content


def _relations(path: Path, limit: int) -> tuple[GraphFact, ...]:
    try:
        records = json.loads(_read_file(path, limit))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DomainError("GRAPH_FACTS_INVALID", "关系文件必须是 UTF-8 JSON 数组。", 422) from exc
    if not isinstance(records, list):
        raise DomainError("GRAPH_FACTS_INVALID", "关系文件必须是 GraphFact 对象数组。", 422)
    result = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {
            "subject",
            "predicate",
            "object",
            "source_ids",
        }:
            raise DomainError(
                "GRAPH_FACTS_INVALID",
                "关系字段必须为 subject、predicate、object、source_ids。",
                422,
            )
        if not isinstance(record["source_ids"], list) or any(
            not isinstance(item, str) for item in record["source_ids"]
        ):
            raise DomainError(
                "GRAPH_FACTS_INVALID", "关系 source_ids 必须是来源 ID 字符串数组。", 422
            )
        result.append(
            GraphFact(
                record["subject"],
                record["predicate"],
                record["object"],
                tuple(record["source_ids"]),
            )
        )
    return tuple(result)


async def _knowledge(args: argparse.Namespace, services: Services, session: dict) -> dict:
    action = args.knowledge_action
    if action == "list":
        return services.workflows.catalog(session)
    if action == "ingest":
        content = _read_file(args.file, services.documents.config.max_upload_bytes)
        return await services.documents.ingest(
            session,
            filename=args.file.name,
            title=args.title,
            content=content,
            media_type=UPLOAD_MEDIA_TYPES.get(args.file.suffix.lower(), "application/octet-stream"),
        )
    if action == "retry":
        return await services.documents.retry_document(session, args.document_id, args.version)
    if action == "load-demo":
        return await services.workflows.load_demo(
            session, with_embeddings=args.with_embeddings, with_graph=args.with_graph
        )
    if action == "build":
        if args.relations is not None and not args.with_graph:
            raise DomainError(
                "GRAPH_BUILD_REQUIRED", "提供 --relations 时必须同时使用 --with-graph。", 422
            )
        facts = (
            _relations(args.relations, services.documents.config.max_upload_bytes)
            if args.relations is not None
            else None
        )
        return await services.workflows.build(
            session, with_embeddings=args.with_embeddings, with_graph=args.with_graph, facts=facts
        )
    if action == "search":
        snapshot = services.workflows.snapshot(session, args.snapshot_id)
        result = await services.workflows.search(
            session, args.query, method=args.method, snapshot_id=snapshot["id"], limit=args.limit
        )
        return {
            "snapshot_id": snapshot["id"],
            "query": args.query,
            "method": args.method,
            "evidence": [item.as_dict() for item in result],
        }
    raise ValueError("未知的知识命令。")


async def execute(args: argparse.Namespace, services: Services) -> tuple[dict, int]:
    created = services.store.create_session(args.customer)
    session = services.store.authenticate(created["token"])
    if args.command == "knowledge":
        return await _knowledge(args, services, session), 0
    if args.command == "research":
        snapshot = services.workflows.snapshot(session, args.snapshot_id)
        request = ResearchRequest(snapshot_id=snapshot["id"], mode=args.mode)
        turn = await services.agent.ask(session, args.query, str(uuid4()), research=request)
        return {**turn, "session_id": session["id"], "snapshot_id": snapshot["id"]}, 0
    if args.command == "evaluate-knowledge":
        report = await evaluate_knowledge(
            services.workflows, session, snapshot_id=args.snapshot_id, methods=args.methods
        )
        return report, 0 if report["status"] == "complete" else 1
    raise ValueError("未知的知识或研究命令。")
