"""Console entry points for serving, evaluation, and an explicit live-model probe."""

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from .bootstrap import create_services
from .errors import DomainError
from .knowledge_cli import EXTENDED_COMMANDS, add_commands, execute
from .settings import Settings


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def _probe(settings: Settings) -> dict:
    if settings.provider != "llm":
        raise ValueError("probe-model 需要 TRACEDESK_PROVIDER=llm 及完整模型配置。")
    with TemporaryDirectory(prefix="tracedesk-probe-") as temporary:
        service = create_services(replace(settings, database_path=Path(temporary) / "probe.db"))
        try:
            created = service.store.create_session("customer-a")
            session = service.store.authenticate(created["token"])
            turn = await service.agent.ask(
                session, "签收后多久可以退货？请给出政策依据。", str(uuid4())
            )
            return {key: turn[key] for key in ("status", "runtime", "answer", "citations", "trace")}
        finally:
            await service.close()


async def _extended(args: argparse.Namespace, settings: Settings) -> tuple[dict, int]:
    services = create_services(settings)
    try:
        return await execute(args, services)
    finally:
        await services.close()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")
    parser = argparse.ArgumentParser(
        prog="tracedesk", description="有证据和确认流程的演示客服 Agent"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="启动完整服务")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--database", type=Path)
    serve.add_argument(
        "--allow-public-demo", action="store_true", help="显式允许对外提供演示身份接口"
    )
    evaluate = commands.add_parser("evaluate", help="运行固定用例的检索消融和工作流回归")
    evaluate.add_argument("--output", type=Path, default=Path("reports/evaluation.json"))
    commands.add_parser("probe-model", help="对配置的真实模型执行一次带来源的工具调用")
    add_commands(commands)
    args = parser.parse_args()
    try:
        if args.command == "evaluate":
            from .evaluation import evaluate_all

            report = evaluate_all()
            _write_json(args.output, report)
            print(
                json.dumps(
                    {
                        "report": str(args.output.resolve()),
                        "workflow": f"{report['workflow']['passed']}/{report['workflow']['total']}",
                        "live_model": "not_run",
                    },
                    ensure_ascii=False,
                )
            )
            return 0 if report["workflow"]["passed"] == report["workflow"]["total"] else 1
        if (
            args.command == "evaluate-knowledge"
            and args.output.resolve().name.casefold() == "evaluation.json"
        ):
            raise ValueError("知识评测不能覆盖原 evaluation.json，请使用独立输出文件名。")
        settings = Settings.from_env()
        if getattr(args, "database", None) is not None:
            settings = replace(settings, database_path=args.database)
        if args.command in EXTENDED_COMMANDS:
            data, code = asyncio.run(_extended(args, settings))
            if args.command == "evaluate-knowledge":
                _write_json(args.output, data)
                data = {
                    "report": str(args.output.resolve()),
                    "status": data["status"],
                    "snapshot_id": data["snapshot"]["id"],
                    "metrics": data["metrics"],
                }
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return code
        if args.command == "probe-model":
            print(json.dumps(asyncio.run(_probe(settings)), ensure_ascii=False, indent=2))
            return 0
        if args.host not in {"127.0.0.1", "localhost", "::1"} and not args.allow_public_demo:
            raise ValueError(
                "公开监听须显式加 --allow-public-demo；此版本仅提供演示身份，不适合真实客户数据。"
            )
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(settings), host=args.host, port=args.port)
        return 0
    except (ValueError, DomainError, OSError) as exc:
        print(f"TraceDesk 配置或执行错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
