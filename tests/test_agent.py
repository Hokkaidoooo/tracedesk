import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from tracedesk.bootstrap import create_services
from tracedesk.errors import DomainError
from tracedesk.provider import ModelReply, ToolCall
from tracedesk.settings import Settings
from tracedesk.tools import ToolExecutor
from tracedesk.trace import Trace

NOW = datetime(2026, 10, 16, 8, tzinfo=timezone.utc)


class ScriptedProvider:
    """An explicit protocol fixture, never used by the product or its evaluation scores."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    async def complete(self, messages, tools):
        self.requests.append((list(messages), tools))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def reply(*calls):
    parsed = tuple(
        ToolCall(str(uuid4()), name, json.dumps(arguments, ensure_ascii=False))
        for name, arguments in calls
    )
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in parsed
        ],
    }
    return ModelReply(parsed, message, 25)


def make_service(tmp_path, *, provider=None, **overrides):
    settings = Settings(database_path=tmp_path / "agent.db")
    if provider:
        settings = replace(settings, provider="llm", base_url="http://127.0.0.1:1/v1")
    settings = replace(settings, **overrides)
    service = create_services(settings, provider=provider, clock=lambda: NOW)
    created = service.store.create_session("customer-a")
    return service, service.store.authenticate(created["token"])


def ask(service, session, question, request_id=None):
    return asyncio.run(service.agent.ask(session, question, request_id or str(uuid4())))


def executor_for(service, session):
    run = service.store.start_run(session, "test tools", str(uuid4()), service.settings.provider)
    return ToolExecutor(
        service.store,
        service.knowledge,
        service.refunds,
        service.settings,
        session,
        run["id"],
        Trace(),
    )


def test_extractive_answer_uses_complete_current_policy(tmp_path):
    service, session = make_service(tmp_path)
    result = ask(service, session, "签收后多久可以退货？")
    policy = service.knowledge.get("returns", tenant_id="demo-shop", as_of=NOW.date())
    assert result["answer"] == policy.text
    assert result["citations"][0]["source_id"] == "returns@2"
    assert result["runtime"] == "extractive"
    assert not any(event["stage"] == "model" for event in result["trace"])


def test_legitimate_negative_payment_text_is_not_rejected(tmp_path):
    service, session = make_service(tmp_path)
    result = ask(service, session, "退款到账需要多久")
    assert result["status"] == "answered"
    assert result["citations"]
    assert result["action"] is None


def test_followup_uses_persisted_order_and_no_money_is_moved(tmp_path):
    service, session = make_service(tmp_path)
    ask(service, session, "查询 TD-1001")
    result = ask(service, session, "这个订单可以退吗？")
    assert result["action"]["order_id"] == "TD-1001"
    assert result["action"]["amount_minor"] == 12900
    assert result["status"] == "needs_confirmation"
    assert service.store.get_order(session, "TD-1001")["status"] == "delivered"


@pytest.mark.parametrize("question", ["查询 TD-2001", "申请退款 TD-2001"])
def test_foreign_order_produces_no_order_or_action(tmp_path, question):
    service, session = make_service(tmp_path)
    result = ask(service, session, question)
    assert result["status"] == "handoff"
    assert result["order"] is None and result["action"] is None
    assert "无线鼠标" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize("question", ["先别退款", "不要退货", "取消退款"])
def test_negated_request_never_prepares_action(tmp_path, question):
    service, session = make_service(tmp_path)
    ask(service, session, "查询 TD-1001")
    result = ask(service, session, question)
    assert result["action"] is None
    assert result["status"] == "needs_information"


def test_tool_parameters_cannot_change_customer_or_amount(tmp_path):
    service, session = make_service(tmp_path)
    tools = executor_for(service, session)
    result = tools.execute("lookup_order", {"order_id": "TD-1001", "customer_id": "customer-b"})
    assert result["error"]["code"] == "INVALID_TOOL_ARGUMENTS"
    result = tools.execute(
        "prepare_refund", {"order_id": "TD-1001", "reason": "退货", "amount_minor": 1}
    )
    assert result["error"]["code"] == "INVALID_TOOL_ARGUMENTS"
    assert tools.action is None


def test_unknown_tools_and_invented_citations_are_rejected(tmp_path):
    service, session = make_service(tmp_path)
    tools = executor_for(service, session)
    assert tools.execute("confirm_refund", {})["error"]["code"] == "TOOL_NOT_ALLOWED"
    result = tools.execute(
        "final_answer",
        {
            "status": "answered",
            "citations": [{"source_id": "invented@1", "quote": "这是一条没有来源的虚构退款政策。"}],
        },
    )
    assert result["error"]["code"] == "UNKNOWN_CITATION"
    assert tools.final is None


def test_model_cannot_inject_a_success_claim_or_chop_policy_conditions(tmp_path):
    service, session = make_service(tmp_path)
    tools = executor_for(service, session)
    documents = tools.execute("search_knowledge", {"query": "签收后退货条件"})["documents"]
    document = next(item for item in documents if item["source_id"] == "returns@2")
    bad = tools.execute(
        "final_answer", {"status": "answered", "answer": "已提交退款申请", "citations": []}
    )
    assert bad["error"]["code"] == "INVALID_TOOL_ARGUMENTS"
    partial = document["quote"].split("。", 1)[0]
    good = tools.execute(
        "final_answer",
        {
            "status": "answered",
            "citations": [{"source_id": document["source_id"], "quote": partial}],
        },
    )
    assert good["ok"]
    assert tools.final["answer"] == document["quote"]
    assert tools.action is None


def test_duplicate_prepare_reuses_action_and_locks_order_context(tmp_path):
    service, session = make_service(tmp_path)
    tools = executor_for(service, session)
    arguments = {"order_id": "TD-1001", "reason": "用户申请"}
    first = tools.execute("prepare_refund", arguments)
    second = tools.execute("prepare_refund", arguments)
    assert first["action"]["id"] == second["action"]["id"]
    assert second["replayed"] is True
    assert "confirmation_token" not in json.dumps(first)
    other = tools.execute("lookup_order", {"order_id": "TD-1002"})
    assert other["error"]["code"] == "ORDER_CONTEXT_LOCKED"
    assert tools.order["id"] == tools.action["order_id"]


def test_historical_policy_cannot_replace_action_policy_evidence(tmp_path):
    service, session = make_service(tmp_path)
    tools = executor_for(service, session)
    old = tools.execute("search_knowledge", {"query": "签收后退货条件", "as_of": "2025-10-16"})
    policy = next(item for item in old["documents"] if item["source_id"] == "returns@1")
    assert tools.execute("prepare_refund", {"order_id": "TD-1001", "reason": "用户申请"})["ok"]
    result = tools.execute(
        "final_answer",
        {"status": "answered", "citations": [{"source_id": "returns@1", "quote": policy["quote"]}]},
    )
    assert result["ok"]
    assert [item["source_id"] for item in tools.final["citations"]] == ["returns@2"]


def test_model_failure_is_persisted_and_cancels_undelivered_action(tmp_path):
    scripted = ScriptedProvider(
        [
            reply(("prepare_refund", {"order_id": "TD-1001", "reason": "用户申请"})),
            DomainError("MODEL_UNAVAILABLE", "连接失败", 502),
        ]
    )
    service, session = make_service(tmp_path, provider=scripted)
    with pytest.raises(DomainError, match="连接失败") as failure:
        ask(service, session, "申请退款 TD-1001")
    record = service.store.run_details(session, failure.value.details["run_id"])
    assert record["status"] == "failed" and record["turn"] is None
    with service.store.transaction() as connection:
        states = [row[0] for row in connection.execute("SELECT state FROM actions")]
    assert states == ["cancelled"]
    assert service.store.list_turns(session) == []


def test_tool_output_budget_never_commits_a_failed_final_answer(tmp_path):
    scripted = ScriptedProvider(
        [reply(("final_answer", {"status": "needs_information", "clarification": "order_id"}))]
    )
    service, session = make_service(tmp_path, provider=scripted, max_tool_output_chars=1)
    with pytest.raises(DomainError) as failure:
        ask(service, session, "我要退款")
    assert failure.value.code == "TOOL_OUTPUT_BUDGET"
    record = service.store.run_details(session, failure.value.details["run_id"])
    assert record["status"] == "failed" and record["turn"] is None


def test_model_budget_exhaustion_is_not_an_extractive_fallback(tmp_path):
    scripted = ScriptedProvider([reply(("lookup_order", {"order_id": "TD-1001"}))])
    service, session = make_service(tmp_path, provider=scripted, max_model_steps=1)
    with pytest.raises(DomainError) as failure:
        ask(service, session, "查询 TD-1001")
    assert failure.value.code == "MODEL_BUDGET_EXCEEDED"
    assert service.store.list_turns(session) == []


def test_llm_runs_registered_tools_with_server_compiled_answer(tmp_path):
    scripted = ScriptedProvider(
        [
            reply(("lookup_order", {"order_id": "TD-1001"})),
            reply(("final_answer", {"status": "answered"})),
        ]
    )
    service, session = make_service(tmp_path, provider=scripted)
    result = ask(service, session, "查询 TD-1001")
    assert result["runtime"] == "llm" and result["status"] == "answered"
    assert "129.00" in result["answer"]
    schemas = scripted.requests[0][1]
    assert {item["function"]["name"] for item in schemas} == {
        "lookup_order",
        "search_knowledge",
        "prepare_refund",
        "final_answer",
    }
    assert "customer_id" not in json.dumps(schemas)
