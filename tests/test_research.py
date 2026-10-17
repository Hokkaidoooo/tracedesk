"""Protocol fixtures verify orchestration, not the quality of an unconfigured live model."""

import asyncio
import builtins
import json
from dataclasses import FrozenInstanceError, replace

import httpx
import pytest

from tracedesk.errors import DomainError
from tracedesk.evidence import Evidence
from tracedesk.provider import ChatCompletionsProvider, ModelReply, ToolCall, parse_reply
from tracedesk.research import ResearchConfig, ResearchOrchestrator
from tracedesk.settings import Settings
from tracedesk.trace import Trace

SOURCE = Evidence(
    source_id="returns-block",
    tenant_id="shop-a",
    snapshot_id="snapshot-1",
    document_id="returns",
    document_version=1,
    title="退货说明",
    text="签收后七天内可以申请退货，但必须保留包装。生鲜商品不适用。",
    source_sha256="a" * 64,
    page=2,
)
CLAIM = {
    "claim_id": "c1",
    "claim": "可在签收后七天内申请退货，必须保留包装，生鲜商品除外。",
    "source_id": SOURCE.source_id,
    "quote": SOURCE.text,
}
CONFIG = ResearchConfig(available_methods=("lexical",))


def task(name="t1", *, query="退货条件", method="lexical", dependencies=()):
    return {"id": name, "query": query, "method": method, "depends_on": list(dependencies)}


def verdict(claim_id="c1", supported=True):
    return {
        "claim_id": claim_id,
        "supported": supported,
        "reason": "原文明确包含该条件。" if supported else "结论扩大了原文适用范围。",
    }


def completion(tool_name, arguments, *, tokens=11, call_id="fixture-call"):
    body = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "PRIVATE_MODEL_CONTENT",
                    "reasoning_content": "PRIVATE_MODEL_REASONING",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(arguments, ensure_ascii=False),
                            },
                        }
                    ],
                },
            }
        ],
    }
    if tokens is not None:
        body["usage"] = {"total_tokens": tokens}
    return body


class ProtocolProvider:
    def __init__(self, *, plan=None, claims=None, verification=None, tokens=11, select=None):
        self.plan = [task()] if plan is None else plan
        self.claims = [CLAIM] if claims is None else claims
        self.verification = [verdict()] if verification is None else verification
        self.tokens = tokens
        self.select = select
        self.requests = []

    def response(self, messages, tools):
        self.requests.append({"messages": messages, "tools": tools})
        name = tools[0]["function"]["name"]
        payload = json.loads(messages[-1]["content"])
        if name == "submit_research_plan":
            result = {"tasks": self.plan}
        elif name == "select_research_query":
            result = (
                self.select(payload)
                if self.select
                else {
                    "query": payload["task"]["query"] + " 原文",
                    "method": payload["task"]["method"],
                }
            )
        elif name == "submit_research_claims":
            result = {"claims": self.claims}
        elif name == "verify_research_claims":
            result = {"verdicts": self.verification}
        elif name == "compile_read_query":
            result = {"query": "MATCH (n) RETURN n"}
        else:
            raise AssertionError(f"Unexpected model tool: {name}")
        tokens = self.tokens(len(self.requests)) if callable(self.tokens) else self.tokens
        return completion(name, result, tokens=tokens, call_id=f"call-{len(self.requests)}")

    async def complete(self, messages, tools):
        return parse_reply(json.dumps(self.response(messages, tools), ensure_ascii=False).encode())


async def retrieve_source(query, method, dependencies, provider):
    return (SOURCE,)


def answer(provider=None, *, retrieve=retrieve_source, config=CONFIG, history=()):
    actual_provider = ProtocolProvider() if provider is None else provider
    trace = Trace()
    result = asyncio.run(
        ResearchOrchestrator(actual_provider, retrieve, config).answer(
            "退货需要满足哪些条件？",
            history=history,
            trace=trace,
        )
    )
    return result, trace


def test_multi_uses_actual_role_protocol_and_authoritative_quote_spans():
    provider = ProtocolProvider()
    result, trace = answer(provider, history=[{"role": "user", "content": "我咨询普通商品。"}])
    assert result["status"] == "answered"
    assert CLAIM["claim"] in result["answer"]
    citation = result["citations"][0]
    assert citation["source_id"] == SOURCE.source_id
    assert citation["document_version"] == 1 and citation["page"] == 2
    assert citation["quote"] == SOURCE.text[citation["quote_start"] : citation["quote_end"]]
    metadata = result["research"]
    assert metadata["workflow"] == "langgraph"
    assert [role["role"] for role in metadata["roles"]] == [
        "planner",
        "retriever",
        "writer",
        "verifier",
    ]
    assert metadata["model_calls"] == 4 and metadata["retrieval_calls"] == 1
    assert metadata["total_tokens"] == 44 and metadata["context_chars"] > 0
    assert metadata["consultation_only"] is True
    assert metadata["completed_tasks"][0]["query"] == "退货条件 原文"
    assert all(event["status"] == "ok" for event in trace.events)
    serialized = json.dumps({"result": result, "trace": trace.events}, ensure_ascii=False)
    assert "PRIVATE_MODEL" not in serialized
    for request in provider.requests:
        assert len(request["tools"]) == 1
        assert request["tools"][0]["function"]["name"] in {
            "submit_research_plan",
            "select_research_query",
            "submit_research_claims",
            "verify_research_claims",
        }
    verification_payload = json.loads(provider.requests[-1]["messages"][-1]["content"])
    assert verification_payload["evidence"][0]["text"] == SOURCE.text
    assert verification_payload["claims"][0] == CLAIM


def test_single_is_one_researcher_with_continuous_tool_history_and_shared_verifier():
    provider = ProtocolProvider()
    result, _ = answer(provider, config=replace(CONFIG, mode="single"))
    assert result["research"]["role_design"] == "single_researcher_shared_verifier"
    assert [role["role"] for role in result["research"]["roles"]] == [
        "researcher",
        "researcher",
        "verifier",
    ]
    assert result["research"]["model_calls"] == 3
    draft_messages = provider.requests[1]["messages"]
    assert [item["role"] for item in draft_messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "user",
    ]
    tool_evidence = json.loads(draft_messages[3]["content"])["evidence"]
    assert tool_evidence[0]["text"] == SOURCE.text
    assert result["citations"][0]["source_id"] == SOURCE.source_id


def test_independent_tasks_overlap_and_dependencies_wait_for_immutable_evidence():
    async def run():
        both_started = asyncio.Event()
        started = set()
        events = []
        inputs = {}
        plan = [task("a"), task("b"), task("c", dependencies=("a", "b"))]

        def select(payload):
            name = payload["task"]["id"]
            inputs[name] = payload["dependency_evidence"]
            return {"query": name, "method": "lexical"}

        async def retrieve(query, method, dependencies, provider):
            events.append(f"{query}:start")
            if query in {"a", "b"}:
                assert dependencies == ()
                started.add(query)
                if len(started) == 2:
                    both_started.set()
                await asyncio.wait_for(both_started.wait(), 2)
            else:
                assert events.index("a:end") < events.index("c:start")
                assert events.index("b:end") < events.index("c:start")
                assert tuple(source.source_id for source in dependencies) == ("a", "b")
                with pytest.raises(FrozenInstanceError):
                    dependencies[0].text = "overwritten"
            events.append(f"{query}:end")
            return (replace(SOURCE, source_id=query),)

        provider = ProtocolProvider(plan=plan, claims=[{**CLAIM, "source_id": "c"}], select=select)
        result = await ResearchOrchestrator(provider, retrieve, CONFIG).answer(
            "退货条件", trace=Trace()
        )
        assert events.index("a:start") < events.index("b:end")
        assert events.index("b:start") < events.index("a:end")
        assert [source["source_id"] for source in inputs["c"]] == ["a", "b"]
        assert all(source["text"] == SOURCE.text for source in inputs["c"])
        assert [item["task_id"] for item in result["research"]["completed_tasks"]] == [
            "a",
            "b",
            "c",
        ]
        assert result["research"]["model_calls"] == 6

    asyncio.run(run())


@pytest.mark.parametrize(
    "plan",
    [
        [task("a"), task("a")],
        [task("a", dependencies=("missing",))],
        [task("a", dependencies=("a",))],
        [task("a", dependencies=("b",)), task("b", dependencies=("a",))],
        [task("a"), task("b", dependencies=("a", "a"))],
        [task(query="   ")],
    ],
)
def test_invalid_dependency_plans_stop_before_retrieval(plan):
    provider = ProtocolProvider(plan=plan)
    with pytest.raises(DomainError) as failure:
        answer(provider)
    assert failure.value.code == "RESEARCH_PLAN_INVALID"
    assert len(provider.requests) == 1
    assert failure.value.details["research"]["retrieval_calls"] == 0


def test_task_count_is_shared_budget_before_any_worker_starts():
    provider = ProtocolProvider(plan=[task("a"), task("b")])
    with pytest.raises(DomainError) as failure:
        answer(provider, config=replace(CONFIG, max_tasks=1))
    assert failure.value.code == "RESEARCH_BUDGET_EXCEEDED"
    assert failure.value.details["budget"] == "max_tasks"
    assert len(provider.requests) == 1


@pytest.mark.parametrize("select", [False, True])
def test_unavailable_methods_are_rejected_without_method_fallback(select):
    provider = ProtocolProvider(
        plan=[task(method="lexical" if select else "semantic")],
        select=(lambda payload: {"query": "退货", "method": "semantic"}) if select else None,
    )
    with pytest.raises(DomainError) as failure:
        answer(provider)
    assert failure.value.code == "RESEARCH_METHOD_UNAVAILABLE"
    assert failure.value.details["research"]["retrieval_calls"] == 0
    plan_schema = provider.requests[0]["tools"][0]["function"]["parameters"]
    assert plan_schema["$defs"]["ResearchTask"]["properties"]["method"]["enum"] == ["lexical"]


def test_missing_model_fails_without_extractive_answer():
    trace = Trace()
    with pytest.raises(DomainError) as failure:
        asyncio.run(
            ResearchOrchestrator(None, retrieve_source, CONFIG).answer("退货条件", trace=trace)
        )
    assert failure.value.code == "SERVICE_NOT_CONFIGURED"
    assert failure.value.details["service"] == "model"
    assert failure.value.details["research"]["model_calls"] == 0
    assert trace.events[-1]["status"] == "failed"


def test_missing_optional_langgraph_is_an_explicit_service_error(monkeypatch):
    original = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "langgraph.graph":
            raise ModuleNotFoundError("test missing langgraph")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    provider = ProtocolProvider()
    with pytest.raises(DomainError) as failure:
        answer(provider)
    assert failure.value.code == "SERVICE_NOT_CONFIGURED"
    assert failure.value.details["service"] == "langgraph"
    assert provider.requests == []


@pytest.mark.parametrize(
    "claims",
    [
        [{**CLAIM, "source_id": "invented"}],
        [{**CLAIM, "quote": "签收后三十天内可以申请退货"}],
        [{**CLAIM, "quote": "签收后 七天内可以申请退货"}],
        [{**CLAIM, "quote": " "}],
        [{**CLAIM, "claim": " "}],
        [CLAIM, CLAIM],
    ],
)
def test_wrong_sources_quotes_and_duplicate_claims_fail_before_semantic_verifier(claims):
    provider = ProtocolProvider(claims=claims)
    with pytest.raises(DomainError) as failure:
        answer(provider)
    assert failure.value.code == "RESEARCH_CITATION_INVALID"
    assert len(provider.requests) == 3
    assert failure.value.details["research"]["roles"][-1]["status"] == "error"


@pytest.mark.parametrize("mode", ["single", "multi"])
def test_existing_quote_is_not_semantic_support_when_verifier_rejects(mode):
    unsupported = {**CLAIM, "claim": "所有商品都可以无限期无条件退货。"}
    provider = ProtocolProvider(claims=[unsupported], verification=[verdict(supported=False)])
    result, trace = answer(provider, config=replace(CONFIG, mode=mode))
    assert result["status"] == "insufficient_evidence"
    assert result["citations"] == []
    assert unsupported["claim"] not in result["answer"]
    assert result["research"]["verification"][0]["supported"] is False
    assert trace.events[-1]["status"] == "ok"
    assert any(
        event["stage"] == "research.verification" and event["status"] == "rejected"
        for event in trace.events
    )


def test_partial_answer_contains_only_supported_claims_and_explicit_verdicts():
    rejected = {**CLAIM, "claim_id": "c2", "claim": "已替你完成退款。"}
    provider = ProtocolProvider(
        claims=[CLAIM, rejected],
        verification=[verdict(), verdict("c2", False)],
    )
    result, _ = answer(provider)
    assert result["status"] == "partially_supported"
    assert CLAIM["claim"] in result["answer"] and rejected["claim"] not in result["answer"]
    assert [item["claim_id"] for item in result["citations"]] == ["c1"]
    assert len(result["research"]["verification"]) == 2


@pytest.mark.parametrize("verification", [[], [verdict(), verdict()], [verdict("unknown")]])
def test_verifier_must_cover_every_claim_once(verification):
    with pytest.raises(DomainError) as failure:
        answer(ProtocolProvider(verification=verification))
    assert failure.value.code == "RESEARCH_VERIFICATION_INVALID"


def test_empty_draft_reports_insufficient_evidence_without_invented_answer():
    provider = ProtocolProvider(claims=[])
    result, _ = answer(provider)
    assert result["status"] == "insufficient_evidence" and result["citations"] == []
    assert result["research"]["verification"] == []
    assert len(provider.requests) == 3


def test_empty_retrieval_is_visible_and_stops_drafting():
    async def empty(query, method, dependencies, provider):
        return ()

    trace = Trace()
    provider = ProtocolProvider()
    with pytest.raises(DomainError) as failure:
        asyncio.run(ResearchOrchestrator(provider, empty, CONFIG).answer("退货", trace=trace))
    assert failure.value.code == "RESEARCH_NO_EVIDENCE"
    assert len(provider.requests) == 2
    assert any(event["detail"].endswith("RESEARCH_NO_EVIDENCE") for event in trace.events)


@pytest.mark.parametrize(
    "other,code",
    [
        (replace(SOURCE, text="同一来源被改写"), "RESEARCH_EVIDENCE_CONFLICT"),
        (replace(SOURCE, tenant_id="shop-b"), "RESEARCH_EVIDENCE_SCOPE_MISMATCH"),
        (replace(SOURCE, snapshot_id="snapshot-2"), "RESEARCH_EVIDENCE_SCOPE_MISMATCH"),
        (replace(SOURCE, source_id="second", source_sha256="b" * 64), "RESEARCH_EVIDENCE_CONFLICT"),
    ],
)
def test_parallel_results_cannot_mix_scope_or_change_source_identity(other, code):
    async def retrieve(query, method, dependencies, provider):
        return (SOURCE if query == "a" else other,)

    provider = ProtocolProvider(
        plan=[task("a"), task("b")],
        select=lambda payload: {"query": payload["task"]["id"], "method": "lexical"},
    )
    with pytest.raises(DomainError) as failure:
        answer(provider, retrieve=retrieve)
    assert failure.value.code == code
    assert all(
        request["tools"][0]["function"]["name"] != "submit_research_claims"
        for request in provider.requests
    )


def test_mutable_evidence_container_is_not_accepted():
    async def mutable(query, method, dependencies, provider):
        return [SOURCE]

    with pytest.raises(DomainError) as failure:
        answer(retrieve=mutable)
    assert failure.value.code == "RESEARCH_EVIDENCE_INVALID"


def test_http_provider_transport_obeys_research_function_calling_contract():
    async def run():
        fixture = ProtocolProvider()
        paths = []

        async def handle(request):
            paths.append(request.url.path)
            payload = json.loads(request.content)
            assert "authorization" not in request.headers
            assert payload["model"] == "research-protocol-fixture"
            assert payload["tool_choice"] == "required"
            return httpx.Response(200, json=fixture.response(payload["messages"], payload["tools"]))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            provider = ChatCompletionsProvider(
                Settings(
                    provider="llm",
                    model="research-protocol-fixture",
                    base_url="http://127.0.0.1/v1",
                ),
                client=client,
            )
            result = await ResearchOrchestrator(provider, retrieve_source, CONFIG).answer(
                "退货条件",
                trace=Trace(),
            )
        assert result["status"] == "answered"
        assert paths == ["/v1/chat/completions"] * 4
        assert result["research"]["total_tokens"] == 44

    asyncio.run(run())


def test_shared_model_budget_reserves_before_parallel_requests_and_cancels_sibling():
    async def run():
        blocked = asyncio.Event()
        cancelled = asyncio.Event()

        class BlockingProvider(ProtocolProvider):
            async def complete(self, messages, tools):
                if tools[0]["function"]["name"] == "select_research_query":
                    self.response(messages, tools)
                    try:
                        await blocked.wait()
                    finally:
                        cancelled.set()
                return await super().complete(messages, tools)

        provider = BlockingProvider(plan=[task("a"), task("b")])
        trace = Trace()
        orchestrator = ResearchOrchestrator(
            provider,
            retrieve_source,
            replace(CONFIG, max_model_calls=2, max_parallel=2),
        )
        with pytest.raises(DomainError) as failure:
            await orchestrator.answer("退货", trace=trace)
        assert failure.value.code == "RESEARCH_BUDGET_EXCEEDED"
        assert failure.value.details["budget"] == "max_model_calls"
        assert len(provider.requests) == 2
        assert failure.value.details["research"]["model_calls"] == 2
        assert failure.value.details["research"]["total_tokens"] is None
        assert cancelled.is_set()
        assert any(event["status"] == "cancelled" for event in trace.events)

    asyncio.run(run())


def test_context_budget_counts_cumulative_prompts_without_truncation():
    first, _ = answer()
    roles = first["research"]["roles"]
    limit = roles[0]["context_chars"] + roles[1]["context_chars"] - 1
    assert limit > max(roles[0]["context_chars"], roles[1]["context_chars"])
    provider = ProtocolProvider()
    with pytest.raises(DomainError) as failure:
        answer(provider, config=replace(CONFIG, max_context_chars=limit))
    assert failure.value.code == "RESEARCH_BUDGET_EXCEEDED"
    assert failure.value.details["budget"] == "max_context_chars"
    assert len(provider.requests) == 1
    assert failure.value.details["research"]["retrieval_calls"] == 0


def test_retrieval_call_budget_applies_to_all_tasks():
    calls = []

    async def retrieve(query, method, dependencies, provider):
        calls.append(query)
        return (SOURCE,)

    provider = ProtocolProvider(plan=[task("a"), task("b")])
    with pytest.raises(DomainError) as failure:
        answer(provider, retrieve=retrieve, config=replace(CONFIG, max_retrieval_calls=1))
    assert failure.value.code == "RESEARCH_BUDGET_EXCEEDED"
    assert failure.value.details["budget"] == "max_retrieval_calls"
    assert len(calls) == 1


@pytest.mark.parametrize("limit,expected", [(5, "answered"), (4, "RESEARCH_BUDGET_EXCEEDED")])
def test_nested_text2cypher_uses_same_model_budget_and_actual_usage(limit, expected):
    nested = []

    async def retrieve(query, method, dependencies, provider):
        nested.append(provider)
        reply = await provider.complete(
            [{"role": "user", "content": "{}"}],
            [{"type": "function", "function": {"name": "compile_read_query", "parameters": {}}}],
        )
        assert reply.calls[0].name == "compile_read_query"
        return (SOURCE,)

    provider = ProtocolProvider(plan=[task(method="text2cypher")])
    config = replace(CONFIG, available_methods=("text2cypher",), max_model_calls=limit)
    if expected == "answered":
        result, _ = answer(provider, retrieve=retrieve, config=config)
        assert result["status"] == expected
        metadata = result["research"]
        assert metadata["model_calls"] == 5 and metadata["total_tokens"] == 55
        assert any(role["step"] == "text2cypher" for role in metadata["roles"])
    else:
        with pytest.raises(DomainError) as failure:
            answer(provider, retrieve=retrieve, config=config)
        assert failure.value.code == expected
        assert failure.value.details["research"]["model_calls"] == 4
    assert len(provider.requests) == limit
    assert nested[0] is not provider


def test_nested_model_concurrency_uses_shared_slots_even_inside_one_retrieval():
    async def run():
        active = 0
        maximum = 0
        first_pair = asyncio.Event()

        class ConcurrentProvider(ProtocolProvider):
            async def complete(self, messages, tools):
                nonlocal active, maximum
                if tools[0]["function"]["name"] == "compile_read_query":
                    active += 1
                    maximum = max(maximum, active)
                    if active == 2:
                        first_pair.set()
                    try:
                        await asyncio.wait_for(first_pair.wait(), 2)
                        await asyncio.sleep(0)
                    finally:
                        active -= 1
                return await super().complete(messages, tools)

        async def retrieve(query, method, dependencies, provider):
            await asyncio.gather(
                *(
                    provider.complete(
                        [{"role": "user", "content": "{}"}],
                        [
                            {
                                "type": "function",
                                "function": {
                                    "name": "compile_read_query",
                                    "parameters": {},
                                },
                            }
                        ],
                    )
                    for _ in range(4)
                )
            )
            return (SOURCE,)

        provider = ConcurrentProvider()
        result = await ResearchOrchestrator(
            provider,
            retrieve,
            replace(CONFIG, max_parallel=2),
        ).answer("退货", trace=Trace())
        assert maximum == 2
        assert result["research"]["model_calls"] == 8
        assert result["research"]["total_tokens"] == 88

    asyncio.run(run())


@pytest.mark.parametrize("failure_kind", ["budget", "model"])
def test_nested_model_failure_joins_siblings_before_reporting_research_failure(failure_kind):
    async def run():
        started = asyncio.Event()
        stopped = asyncio.Event()
        nested_jobs = []

        class BlockingProvider(ProtocolProvider):
            async def complete(self, messages, tools):
                if tools[0]["function"]["name"] == "compile_read_query":
                    index = json.loads(messages[0]["content"])["index"]
                    if index == 0:
                        started.set()
                        try:
                            await asyncio.Event().wait()
                        finally:
                            stopped.set()
                    await started.wait()
                    raise RuntimeError("nested model request failed")
                return await super().complete(messages, tools)

        async def retrieve(query, method, dependencies, provider):
            nested_jobs.extend(
                asyncio.create_task(
                    provider.complete(
                        [{"role": "user", "content": json.dumps({"index": index})}],
                        [
                            {
                                "type": "function",
                                "function": {
                                    "name": "compile_read_query",
                                    "parameters": {},
                                },
                            }
                        ],
                    )
                )
                for index in range(2)
            )
            await asyncio.gather(*nested_jobs)
            return (SOURCE,)

        config = replace(
            CONFIG, max_parallel=2, max_model_calls=3 if failure_kind == "budget" else 8
        )
        trace = Trace()
        try:
            with pytest.raises(DomainError) as failure:
                await ResearchOrchestrator(BlockingProvider(), retrieve, config).answer(
                    "退货",
                    trace=trace,
                )
            expected = (
                "RESEARCH_BUDGET_EXCEEDED" if failure_kind == "budget" else "RESEARCH_MODEL_FAILURE"
            )
            assert failure.value.code == expected
            assert started.is_set() and stopped.is_set()
            assert all(job.done() for job in nested_jobs)
            assert all(
                role["status"] != "running" for role in failure.value.details["research"]["roles"]
            )
            assert any(
                event["stage"] == "research.model" and event["status"] == "cancelled"
                for event in trace.events
            )
        finally:
            for job in nested_jobs:
                job.cancel()
            await asyncio.gather(*nested_jobs, return_exceptions=True)

    asyncio.run(run())


def test_retrieval_cannot_return_success_with_an_unfinished_nested_model():
    async def run():
        started = asyncio.Event()
        stopped = asyncio.Event()
        nested_jobs = []

        class BlockingProvider(ProtocolProvider):
            async def complete(self, messages, tools):
                if tools[0]["function"]["name"] == "compile_read_query":
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        stopped.set()
                return await super().complete(messages, tools)

        async def retrieve(query, method, dependencies, provider):
            nested_jobs.append(
                asyncio.create_task(
                    provider.complete(
                        [{"role": "user", "content": "{}"}],
                        [
                            {
                                "type": "function",
                                "function": {
                                    "name": "compile_read_query",
                                    "parameters": {},
                                },
                            }
                        ],
                    )
                )
            )
            await asyncio.wait_for(started.wait(), 2)
            return (SOURCE,)

        try:
            with pytest.raises(DomainError) as failure:
                await ResearchOrchestrator(BlockingProvider(), retrieve, CONFIG).answer(
                    "退货",
                    trace=Trace(),
                )
            assert failure.value.code == "RESEARCH_MODEL_TASK_UNFINISHED"
            assert stopped.is_set() and all(job.done() for job in nested_jobs)
            assert all(
                role["role"] != "writer" for role in failure.value.details["research"]["roles"]
            )
        finally:
            for job in nested_jobs:
                job.cancel()
            await asyncio.gather(*nested_jobs, return_exceptions=True)

    asyncio.run(run())


def test_nested_provider_cannot_start_requests_after_its_retrieval_finishes():
    async def run():
        captured = []

        async def retrieve(query, method, dependencies, provider):
            captured.append(provider)
            return (SOURCE,)

        provider = ProtocolProvider()
        result = await ResearchOrchestrator(provider, retrieve, CONFIG).answer(
            "退货",
            trace=Trace(),
        )
        with pytest.raises(DomainError) as failure:
            await captured[0].complete(
                [{"role": "user", "content": "{}"}],
                [
                    {
                        "type": "function",
                        "function": {
                            "name": "compile_read_query",
                            "parameters": {},
                        },
                    }
                ],
            )
        assert failure.value.code == "RESEARCH_MODEL_SCOPE_CLOSED"
        assert len(provider.requests) == result["research"]["model_calls"] == 4

    asyncio.run(run())


def test_total_timeout_cancels_retrieval_and_preserves_failure_trace():
    async def run():
        stopped = asyncio.Event()

        async def retrieve(query, method, dependencies, provider):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        trace = Trace()
        with pytest.raises(DomainError) as failure:
            await ResearchOrchestrator(
                ProtocolProvider(),
                retrieve,
                replace(CONFIG, timeout_seconds=0.2),
            ).answer("退货", trace=trace)
        assert failure.value.code == "RESEARCH_TIMEOUT"
        assert failure.value.details["research"]["outcome"] == "timeout"
        assert stopped.is_set()
        assert any(event["status"] == "cancelled" for event in trace.events)
        assert trace.events[-1]["detail"] == "RESEARCH_TIMEOUT"

    asyncio.run(run())


def test_caller_cancellation_propagates_after_joining_workers():
    async def run():
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def retrieve(query, method, dependencies, provider):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        trace = Trace()
        job = asyncio.create_task(
            ResearchOrchestrator(
                ProtocolProvider(),
                retrieve,
                CONFIG,
            ).answer("退货", trace=trace)
        )
        await asyncio.wait_for(started.wait(), 2)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert stopped.is_set()
        assert trace.events[-1]["status"] == "cancelled"
        assert any(
            event["stage"] == "research.retrieval" and event["status"] == "cancelled"
            for event in trace.events
        )

    asyncio.run(run())


def test_retrieval_domain_error_is_preserved_without_another_method():
    calls = []

    async def unavailable(query, method, dependencies, provider):
        calls.append(method)
        raise DomainError("SERVICE_NOT_CONFIGURED", "缺少图服务。", 503, {"service": "neo4j"})

    provider = ProtocolProvider(plan=[task(method="text2cypher")])
    with pytest.raises(DomainError) as failure:
        answer(
            provider,
            retrieve=unavailable,
            config=replace(CONFIG, available_methods=("lexical", "text2cypher")),
        )
    assert failure.value.code == "SERVICE_NOT_CONFIGURED"
    assert failure.value.details["service"] == "neo4j"
    assert calls == ["text2cypher"]
    assert len(provider.requests) == 2


@pytest.mark.parametrize(
    "where,code",
    [
        ("model", "RESEARCH_MODEL_FAILURE"),
        ("retrieval", "RESEARCH_RETRIEVAL_FAILED"),
    ],
)
def test_unexpected_failures_remain_explicit_with_original_cause(where, code):
    class BrokenProvider:
        async def complete(self, messages, tools):
            raise RuntimeError("protocol transport broke")

    async def broken_retrieval(query, method, dependencies, provider):
        raise RuntimeError("retrieval broke")

    with pytest.raises(DomainError) as failure:
        answer(
            BrokenProvider() if where == "model" else ProtocolProvider(),
            retrieve=broken_retrieval if where == "retrieval" else retrieve_source,
        )
    assert failure.value.code == code
    assert failure.value.details["cause_type"] == "RuntimeError"
    assert failure.value.__cause__.__cause__ is not None
    assert failure.value.details["research"]["outcome"] == "failed"


@pytest.mark.parametrize(
    "tokens,expected", [(None, None), (0, 0), (lambda n: None if n == 2 else 11, None)]
)
def test_missing_usage_is_null_and_never_estimated(tokens, expected):
    result, _ = answer(ProtocolProvider(tokens=tokens))
    assert result["research"]["total_tokens"] == expected
    assert len(result["research"]["roles"]) == 4


def test_unknown_tool_cannot_execute_business_operations():
    class WrongTool:
        async def complete(self, messages, tools):
            return parse_reply(
                json.dumps(completion("prepare_refund", {"order_id": "TD-1001"})).encode()
            )

    with pytest.raises(DomainError) as failure:
        answer(WrongTool())
    assert failure.value.code == "MODEL_PROTOCOL_ERROR"
    assert failure.value.details["research"]["retrieval_calls"] == 0


def test_role_cannot_supply_scope_fields_in_retrieval_arguments():
    provider = ProtocolProvider(
        select=lambda payload: {
            "query": "退货",
            "method": "lexical",
            "tenant_id": "shop-b",
        }
    )
    with pytest.raises(DomainError) as failure:
        answer(provider)
    assert failure.value.code == "MODEL_PROTOCOL_ERROR"
    assert failure.value.details["research"]["retrieval_calls"] == 0


@pytest.mark.parametrize("value", [-1, True, "12"])
def test_invalid_injected_provider_usage_is_a_protocol_error(value):
    class InvalidUsage:
        async def complete(self, messages, tools):
            return ModelReply((ToolCall("x", "submit_research_plan", "{}"),), {}, value)

    with pytest.raises(DomainError) as failure:
        answer(InvalidUsage())
    assert failure.value.code == "MODEL_PROTOCOL_ERROR"
    assert failure.value.details["research"]["total_tokens"] is None


def test_same_orchestrator_has_no_cross_run_budget_or_history_state():
    provider = ProtocolProvider()
    orchestrator = ResearchOrchestrator(provider, retrieve_source, CONFIG)
    first = asyncio.run(
        orchestrator.answer(
            "第一题",
            history=[{"role": "user", "content": "HISTORY_ONLY_FIRST"}],
            trace=Trace(),
        )
    )
    second = asyncio.run(orchestrator.answer("第二题", trace=Trace()))
    assert first["research"]["model_calls"] == second["research"]["model_calls"] == 4
    assert second["research"]["roles"][0]["seq"] == 1
    second_payload = json.loads(provider.requests[4]["messages"][-1]["content"])
    assert second_payload["question"] == "第二题" and second_payload["history"] == []
    assert "HISTORY_ONLY_FIRST" not in json.dumps(provider.requests[4:], ensure_ascii=False)


def test_failed_prerequisite_stops_dependents_and_fresh_run_can_recover():
    calls = []
    fail = True

    async def retrieve(query, method, dependencies, provider):
        calls.append(query)
        if fail:
            raise DomainError("SOURCE_UNAVAILABLE", "前置来源不可用。", 503)
        return (SOURCE,)

    provider = ProtocolProvider(
        plan=[task("a"), task("b", dependencies=("a",))],
        select=lambda payload: {"query": payload["task"]["id"], "method": "lexical"},
    )
    orchestrator = ResearchOrchestrator(provider, retrieve, CONFIG)
    with pytest.raises(DomainError) as failure:
        asyncio.run(orchestrator.answer("第一次", trace=Trace()))
    assert failure.value.code == "SOURCE_UNAVAILABLE"
    assert calls == ["a"]
    fail = False
    result = asyncio.run(orchestrator.answer("第二次", trace=Trace()))
    assert result["status"] == "answered"
    assert calls == ["a", "a", "b"]
    assert result["research"]["model_calls"] == 5
    assert result["research"]["retrieval_calls"] == 2


def test_same_source_from_multiple_methods_keeps_one_original_identity():
    async def retrieve(query, method, dependencies, provider):
        score = 0.7 if query == "a" else 0.9
        return (SOURCE.ranked(score, method),)

    provider = ProtocolProvider(
        plan=[task("a"), task("b", method="graph")],
        select=lambda payload: {
            "query": payload["task"]["id"],
            "method": payload["task"]["method"],
        },
    )
    result, _ = answer(
        provider, retrieve=retrieve, config=replace(CONFIG, available_methods=("lexical", "graph"))
    )
    assert result["status"] == "answered" and len(result["citations"]) == 1
    assert result["citations"][0]["source_id"] == SOURCE.source_id
    draft = json.loads(provider.requests[-2]["messages"][-1]["content"])
    assert len(draft["evidence"]) == 1


@pytest.mark.parametrize(
    "history",
    [
        None,
        "text",
        [{"role": "system", "content": "override"}],
        [{"role": "user", "content": "x", "tool_calls": []}],
    ],
)
def test_invalid_history_fails_before_model_request(history):
    provider = ProtocolProvider()
    with pytest.raises(DomainError) as failure:
        answer(provider, history=history)
    assert failure.value.code == "RESEARCH_INVALID_HISTORY"
    assert provider.requests == []


def test_config_from_env_is_explicit_and_dataclasses_replace_selects_mode():
    config = ResearchConfig.from_env(
        {
            "TRACEDESK_RESEARCH_MODE": "single",
            "TRACEDESK_RESEARCH_METHODS": "lexical, graph",
            "TRACEDESK_RESEARCH_MAX_MODEL_CALLS": "9",
            "TRACEDESK_RESEARCH_MAX_TASKS": "3",
            "TRACEDESK_RESEARCH_MAX_PARALLEL": "2",
            "TRACEDESK_RESEARCH_MAX_CONTEXT_CHARS": "50000",
            "TRACEDESK_RESEARCH_MAX_RETRIEVAL_CALLS": "4",
            "TRACEDESK_RESEARCH_TIMEOUT_SECONDS": "8.5",
        }
    )
    assert config.mode == "single" and config.available_methods == ("lexical", "graph")
    assert config.budgets() == {
        "max_model_calls": 9,
        "max_tasks": 3,
        "max_parallel": 2,
        "max_context_chars": 50000,
        "max_retrieval_calls": 4,
        "timeout_seconds": 8.5,
    }
    assert replace(config, mode="multi").mode == "multi"
    assert ResearchConfig.from_env({}).available_methods == (
        "lexical",
        "semantic",
        "hybrid",
        "graph",
        "text2cypher",
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("MODE", "auto"),
        ("METHODS", ""),
        ("METHODS", "lexical,lexical"),
        ("METHODS", "internet"),
        ("METHODS", "graphrag_local"),
        ("MAX_MODEL_CALLS", "0"),
        ("MAX_TASKS", "-1"),
        ("MAX_PARALLEL", "1.5"),
        ("MAX_CONTEXT_CHARS", "unknown"),
        ("MAX_RETRIEVAL_CALLS", "0"),
        ("TIMEOUT_SECONDS", "nan"),
        ("TIMEOUT_SECONDS", "inf"),
        ("TIMEOUT_SECONDS", "0"),
    ],
)
def test_invalid_configuration_does_not_silently_choose_defaults(key, value):
    with pytest.raises(ValueError):
        ResearchConfig.from_env({f"TRACEDESK_RESEARCH_{key}": value})
