"""Reproducible retrieval ablations and real-service workflow regression cases."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib.resources import as_file
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from .bootstrap import Services, create_services, load_knowledge, resource
from .errors import DomainError
from .retrieval_eval import evaluate_retrieval
from .settings import Settings

REFERENCE_TIME = datetime(2026, 10, 16, 8, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class ConversationCase:
    id: str
    name: str
    questions: tuple[str, ...]
    status: str
    order_id: str | None = None
    source_id: str | None = None


CONVERSATIONS = (
    ConversationCase(
        "w01", "当前退货政策", ("签收后多久可以退货？",), "answered", source_id="returns@2"
    ),
    ConversationCase("w02", "本人订单查询", ("查询订单 TD-1001",), "answered", order_id="TD-1001"),
    ConversationCase(
        "w03",
        "准备符合条件的申请",
        ("申请退货 TD-1001",),
        "needs_confirmation",
        "TD-1001",
        "returns@2",
    ),
    ConversationCase(
        "w04", "超过政策期限", ("申请退款 TD-1002",), "handoff", "TD-1002", "returns@2"
    ),
    ConversationCase(
        "w05", "数字商品不适用", ("申请退款 TD-1003",), "handoff", "TD-1003", "returns@2"
    ),
    ConversationCase("w06", "尚未签收", ("申请退款 TD-1004",), "handoff", "TD-1004", "returns@2"),
    ConversationCase("w07", "其他客户的订单不可见", ("查询订单 TD-2001",), "handoff"),
    ConversationCase("w08", "先补充订单号", ("我想申请退款",), "needs_information"),
    ConversationCase(
        "w09",
        "多轮引用上一订单",
        ("查询订单 TD-1001", "这个订单可以退吗？"),
        "needs_confirmation",
        "TD-1001",
        "returns@2",
    ),
    ConversationCase(
        "w10", "否定操作不创建申请", ("查询订单 TD-1001", "先别退款"), "needs_information"
    ),
    ConversationCase("w11", "无证据问题", ("木星卫星上能种植哪种蘑菇？",), "handoff"),
    ConversationCase("w12", "一次处理一个订单", ("查询 TD-1001 和 TD-1002",), "needs_information"),
)


def _setup(path: Path, current_time: list[datetime]) -> tuple[Services, dict[str, Any]]:
    service = create_services(Settings(database_path=path), clock=lambda: current_time[0])
    created = service.store.create_session("customer-a")
    return service, service.store.authenticate(created["token"])


async def _conversation(case: ConversationCase, path: Path) -> dict[str, Any]:
    service, session = _setup(path, [REFERENCE_TIME])
    turn: dict[str, Any] = {}
    for question in case.questions:
        turn = await service.agent.ask(session, question, str(uuid4()))
    observed_order = turn["order"]["id"] if turn["order"] else None
    sources = [item["source_id"] for item in turn["citations"]]
    correct_action = (turn["action"] is not None) == (case.status == "needs_confirmation")
    passed = turn["status"] == case.status and observed_order == case.order_id and correct_action
    if case.source_id:
        passed = passed and case.source_id in sources
    return {
        "id": case.id,
        "name": case.name,
        "passed": passed,
        "expected": f"{case.status}; order={case.order_id}; source={case.source_id}",
        "actual": f"{turn['status']}; order={observed_order}; sources={','.join(sources)}",
    }


async def _transaction_case(name: str, path: Path) -> dict[str, Any]:
    clock = [REFERENCE_TIME]
    service, session = _setup(path, clock)
    request_id = str(uuid4())
    turn = await service.agent.ask(session, "申请退款 TD-1001", request_id)
    action = turn["action"]
    if action is None:
        return {
            "id": name,
            "name": name,
            "passed": False,
            "expected": "准备申请成功",
            "actual": turn["status"],
        }
    if name == "w13":
        replay = await service.agent.ask(session, "申请退款 TD-1001", request_id)
        return {
            "id": name,
            "name": "对话网络重试幂等",
            "passed": turn == replay,
            "expected": "返回完全相同的 turn",
            "actual": "same turn" if turn == replay else "different turn",
        }
    if name == "w14":
        first = service.refunds.confirm(
            session, action["id"], action["confirmation_token"], str(uuid4())
        )
        second = service.refunds.confirm(
            session, action["id"], action["confirmation_token"], str(uuid4())
        )
        passed = first["receipt_id"] == second["receipt_id"] and second["replayed"]
        return {
            "id": name,
            "name": "重复确认只产生一张回执",
            "passed": passed,
            "expected": "same receipt; replayed=true",
            "actual": f"same_receipt={first['receipt_id'] == second['receipt_id']}; replayed={second['replayed']}",
        }
    if name == "w15":
        service.refunds.cancel(session, action["id"])
        expected_state, title = "cancelled", "取消后不可提交"
    else:
        clock[0] += timedelta(seconds=service.settings.action_ttl_seconds + 1)
        expected_state, title = "expired", "过期后不可提交"
    try:
        service.refunds.confirm(session, action["id"], action["confirmation_token"], str(uuid4()))
    except DomainError as exc:
        state = service.refunds.current_action(session, action["id"])["state"]
        passed = (
            state == expected_state
            and service.store.get_order(session, "TD-1001")["status"] == "delivered"
        )
        return {
            "id": name,
            "name": title,
            "passed": passed,
            "expected": expected_state,
            "actual": f"{state}; {exc.code}",
        }
    return {
        "id": name,
        "name": title,
        "passed": False,
        "expected": expected_state,
        "actual": "unexpectedly submitted",
    }


async def evaluate_workflows() -> dict[str, Any]:
    with TemporaryDirectory(prefix="tracedesk-evaluation-") as temporary:
        root = Path(temporary)
        cases = [await _conversation(case, root / f"{case.id}.db") for case in CONVERSATIONS]
        cases.extend(
            [
                await _transaction_case(case_id, root / f"{case_id}.db")
                for case_id in ("w13", "w14", "w15", "w16")
            ]
        )
    return {"total": len(cases), "passed": sum(case["passed"] for case in cases), "cases": cases}


def evaluate_all() -> dict[str, Any]:
    with as_file(resource("retrieval_cases.jsonl")) as cases_path:
        retrieval = evaluate_retrieval(load_knowledge(), cases_path)
    workflows = asyncio.run(evaluate_workflows())
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "固定合成知识上的检索消融与提取式工作流回归；不代表真实客户评价或线上模型准确率。",
        "reference_time": REFERENCE_TIME.isoformat(),
        "dataset_sha256": retrieval["dataset_sha256"],
        "knowledge_sha256": retrieval["knowledge_sha256"],
        "configuration": retrieval["configuration"],
        "retrieval": retrieval["methods"],
        "workflow": workflows,
        "live_model": {
            "status": "not_run",
            "reason": "由使用者配置模型服务后执行 probe-model；此报告没有真实模型成绩。",
        },
    }
