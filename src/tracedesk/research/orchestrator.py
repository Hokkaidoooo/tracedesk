"""LangGraph coordinates model roles; an explicit DAG bounds independent retrievals."""

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Generic, TypedDict, TypeVar

from pydantic import ValidationError

from ..errors import DomainError
from ..evidence import Evidence
from ..provider import MAX_TOOL_ARGUMENT_CHARS, ModelProvider, ModelReply, ToolCall
from ..trace import Trace
from . import prompts
from .budget import BudgetedModelProvider, RunBudget
from .config import ResearchConfig
from .contracts import (
    ResearchDraft,
    ResearchPlan,
    ResearchTask,
    ResearchVerification,
    ResponseContract,
    RetrievalSelection,
    merge_evidence,
    validate_draft,
    validate_method,
    validate_plan,
    validate_verification,
)

Retrieve = Callable[
    [str, str, tuple[Evidence, ...], ModelProvider], Awaitable[tuple[Evidence, ...]]
]
Contract = TypeVar("Contract", bound=ResponseContract)


@dataclass(frozen=True)
class _Role:
    name: str
    step: str
    tool_name: str
    prompt: str


PLANNER = _Role("planner", "plan", "submit_research_plan", prompts.PLANNER)
RETRIEVER = _Role("retriever", "retrieve", "select_research_query", prompts.RETRIEVER)
WRITER = _Role("writer", "draft", "submit_research_claims", prompts.WRITER)
VERIFIER = _Role("verifier", "verify", "verify_research_claims", prompts.VERIFIER)
SINGLE_RETRIEVER = _Role(
    "researcher", "retrieve", "select_research_query", prompts.SINGLE_RESEARCHER
)
SINGLE_WRITER = _Role("researcher", "draft", "submit_research_claims", prompts.SINGLE_RESEARCHER)


@dataclass(frozen=True)
class _RoleReply(Generic[Contract]):
    value: Contract
    call_id: str
    conversation: tuple[str, ...]


@dataclass(frozen=True)
class _TaskResult:
    task_id: str
    query: str
    method: str
    evidence: tuple[Evidence, ...]
    conversation: tuple[str, ...]


class _State(TypedDict, total=False):
    plan_result: ResearchPlan
    tasks: tuple[_TaskResult, ...]
    sources: tuple[Evidence, ...]
    draft_result: ResearchDraft
    verification: ResearchVerification


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _source_payload(sources: tuple[Evidence, ...]) -> list[dict[str, Any]]:
    # Full text is included once; quote is a draft-specific span, not a second source body.
    return [
        {key: value for key, value in source.as_dict().items() if key != "quote"}
        for source in sources
    ]


def _history_payload(history: Sequence[Mapping[str, str]]) -> tuple[str, ...]:
    if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
        raise DomainError("RESEARCH_INVALID_HISTORY", "研究历史必须是消息序列。")
    messages = []
    for item in history:
        if (
            not isinstance(item, Mapping)
            or set(item) != {"role", "content"}
            or not isinstance(item["role"], str)
            or item["role"] not in {"user", "assistant"}
            or not isinstance(item["content"], str)
        ):
            raise DomainError(
                "RESEARCH_INVALID_HISTORY", "研究历史只接受 user/assistant 文本消息。"
            )
        messages.append(_json(dict(item)))
    return tuple(messages)


def _schema(contract: type[ResponseContract], methods: tuple[str, ...]) -> dict[str, Any]:
    schema = contract.model_json_schema()

    def restrict(node: Any) -> None:
        if isinstance(node, dict):
            properties = node.get("properties", {})
            if "method" in properties:
                properties["method"]["enum"] = list(methods)
            for child in node.values():
                restrict(child)
        elif isinstance(node, list):
            for child in node:
                restrict(child)

    restrict(schema)
    return schema


class ResearchOrchestrator:
    def __init__(
        self,
        provider: ModelProvider,
        retrieve: Retrieve,
        config: ResearchConfig,
    ) -> None:
        self.provider = provider
        self.retrieve = retrieve
        self.config = config

    async def answer(
        self,
        question: str,
        *,
        history: Sequence[Mapping[str, str]] = (),
        trace: Trace,
    ) -> dict[str, Any]:
        run = _Workflow(self.provider, self.retrieve, self.config)
        try:
            run.set_input(question, history)
            graph = run.graph()
            remaining = self.config.timeout_seconds - (perf_counter() - run.started)
            if remaining <= 0:
                raise asyncio.TimeoutError
            state = await asyncio.wait_for(graph.ainvoke({}), remaining)
            run.outcome = "completed"
            return run.result(state)
        except asyncio.TimeoutError as exc:
            run.outcome = "timeout"
            error = DomainError("RESEARCH_TIMEOUT", "研究运行超过总时限，本次已停止。", 504)
            raise run.error(error) from exc
        except asyncio.CancelledError:
            run.outcome = "cancelled"
            raise
        except DomainError as exc:
            run.outcome = "failed"
            raise run.error(exc) from exc
        except Exception as exc:
            run.outcome = "failed"
            error = DomainError(
                "RESEARCH_EXECUTION_FAILED",
                "研究执行发生错误，本次未生成可信回答。",
                500,
                {"cause_type": type(exc).__name__},
            )
            raise run.error(error) from exc
        finally:
            run.budget.emit_trace(trace)
            run.emit_verification_trace(trace)
            trace.add(
                "research",
                "研究流程",
                "ok" if run.outcome == "completed" else run.outcome,
                run.error_code or run.outcome,
                run.started,
            )


class _Workflow:
    def __init__(
        self,
        provider: ModelProvider,
        retrieve: Retrieve,
        config: ResearchConfig,
    ) -> None:
        self.provider = provider
        self.retrieve = retrieve
        self.config = config
        self.budget = RunBudget(config)
        self.started = perf_counter()
        self.outcome = "running"
        self.error_code: str | None = None
        self.question = ""
        self.history: tuple[str, ...] = ()
        self._plan: ResearchPlan | None = None
        self._completed: dict[str, _TaskResult] = {}
        self._verification: ResearchVerification | None = None

    def set_input(self, question: str, history: Sequence[Mapping[str, str]]) -> None:
        if self.provider is None:
            raise DomainError(
                "SERVICE_NOT_CONFIGURED",
                "知识研究需要已配置的模型服务。",
                503,
                {"service": "model"},
            )
        if not isinstance(question, str) or not question.strip():
            raise DomainError("RESEARCH_INVALID_QUESTION", "研究问题不能为空。")
        self.question = question
        self.history = _history_payload(history)

    def graph(self) -> Any:
        try:
            from langgraph.graph import END, START, StateGraph
        except ImportError as exc:
            raise DomainError(
                "SERVICE_NOT_CONFIGURED",
                "知识研究需要安装 tracedesk[research] 依赖。",
                503,
                {"service": "langgraph"},
            ) from exc
        builder = StateGraph(_State)
        builder.add_node("plan", self.plan)
        builder.add_node("execute", self.execute)
        builder.add_node("draft", self.draft)
        builder.add_node("verify", self.verify)
        builder.add_edge(START, "plan")
        builder.add_edge("plan", "execute")
        builder.add_edge("execute", "draft")
        builder.add_edge("draft", "verify")
        builder.add_edge("verify", END)
        return builder.compile()

    def _input(self, **extra: Any) -> dict[str, Any]:
        return {
            "question": self.question,
            "history": [json.loads(item) for item in self.history],
            "available_methods": list(self.config.available_methods),
            **extra,
        }

    def _messages(self, role: _Role, payload: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {"role": "system", "content": role.prompt},
            {"role": "user", "content": _json(payload)},
        ]

    async def _ask(
        self,
        role: _Role,
        messages: list[dict[str, Any]],
        contract: type[Contract],
        *,
        task_id: str | None = None,
        validate: Callable[[Contract], None] | None = None,
    ) -> _RoleReply[Contract]:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": role.tool_name,
                    "description": "提交本角色的结构化决策；不执行任何业务动作。",
                    "parameters": _schema(contract, self.config.available_methods),
                },
            }
        ]
        conversation = tuple(_json(item) for item in messages)

        def parse(reply: ModelReply) -> _RoleReply[Contract]:
            value, call_id, response = _parse(reply, role.tool_name, contract)
            if validate is not None:
                validate(value)
            return _RoleReply(value, call_id, (*conversation, _json(response)))

        provider = BudgetedModelProvider(
            self.provider,
            self.budget,
            role=role.name,
            step=role.step,
            task_id=task_id,
        )
        return await provider.validated(messages, tools, parse)

    async def plan(self, state: _State) -> _State:
        if self.config.mode == "single":
            plan = ResearchPlan(
                tasks=(
                    ResearchTask(
                        id="single",
                        query=self.question,
                        method=self.config.available_methods[0],
                        depends_on=(),
                    ),
                )
            )
            validate_plan(plan, self.config)
        else:
            reply = await self._ask(
                PLANNER,
                self._messages(PLANNER, self._input(max_tasks=self.config.max_tasks)),
                ResearchPlan,
                validate=lambda plan: validate_plan(plan, self.config),
            )
            plan = reply.value
        self._plan = plan
        return {"plan_result": plan}

    async def execute(self, state: _State) -> _State:
        plan = state["plan_result"]
        remaining = list(plan.tasks)
        completed: dict[str, _TaskResult] = {}
        pending: dict[asyncio.Task[_TaskResult], str] = {}
        order = {task.id: index for index, task in enumerate(plan.tasks)}
        try:
            while remaining or pending:
                ready = [task for task in remaining if set(task.depends_on) <= completed.keys()]
                for task in ready[: self.config.max_parallel - len(pending)]:
                    dependencies = merge_evidence(
                        completed[key].evidence for key in task.depends_on
                    )
                    future = asyncio.create_task(self._retrieve_task(task, dependencies))
                    pending[future] = task.id
                    remaining.remove(task)
                if not pending:
                    raise DomainError("RESEARCH_PLAN_INVALID", "研究依赖无法继续执行。", 502)
                done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for future in sorted(done, key=lambda value: order[pending[value]]):
                    task_id = pending.pop(future)
                    result = future.result()
                    merge_evidence(
                        (*(item.evidence for item in completed.values()), result.evidence)
                    )
                    completed[task_id] = result
                    self._completed[task_id] = result
        finally:
            # On a failed prerequisite or caller cancellation, join every sibling before returning.
            for future in pending:
                future.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        tasks = tuple(completed[task.id] for task in plan.tasks)
        sources = merge_evidence(task.evidence for task in tasks)
        return {"tasks": tasks, "sources": sources}

    async def _retrieve_task(
        self,
        task: ResearchTask,
        dependencies: tuple[Evidence, ...],
    ) -> _TaskResult:
        role = SINGLE_RETRIEVER if self.config.mode == "single" else RETRIEVER
        payload = self._input(
            task=task.model_dump(mode="json"),
            dependency_evidence=_source_payload(dependencies),
        )
        reply = await self._ask(
            role,
            self._messages(role, payload),
            RetrievalSelection,
            task_id=task.id,
            validate=self._validate_selection,
        )
        selection = reply.value
        index = await self.budget.retrieval(task.id, selection.query, selection.method)
        try:
            async with BudgetedModelProvider(
                self.provider,
                self.budget,
                role=role.name,
                step=selection.method,
                task_id=task.id,
            ) as nested_provider:
                evidence = await self.retrieve(
                    selection.query,
                    selection.method,
                    dependencies,
                    nested_provider,
                )
            evidence = merge_evidence((evidence,))
            if not evidence:
                raise DomainError(
                    "RESEARCH_NO_EVIDENCE",
                    "研究任务未找到可引用的原文证据。",
                    422,
                    {"task_id": task.id, "method": selection.method},
                )
            merge_evidence((dependencies, evidence))
        except asyncio.CancelledError:
            self.budget.finish(index, "cancelled")
            raise
        except DomainError as exc:
            self.budget.finish(index, "error", error_code=exc.code)
            raise
        except Exception as exc:
            self.budget.finish(index, "error", error_code="RESEARCH_RETRIEVAL_FAILED")
            raise DomainError(
                "RESEARCH_RETRIEVAL_FAILED",
                "研究取证服务发生错误。",
                502,
                {"task_id": task.id, "method": selection.method, "cause_type": type(exc).__name__},
            ) from exc
        self.budget.finish(index, "ok", source_ids=[source.source_id for source in evidence])
        tool_result = _json(
            {
                "role": "tool",
                "tool_call_id": reply.call_id,
                "content": _json({"evidence": _source_payload(evidence)}),
            }
        )
        return _TaskResult(
            task.id,
            selection.query,
            selection.method,
            evidence,
            (*reply.conversation, tool_result),
        )

    def _validate_selection(self, selection: RetrievalSelection) -> None:
        validate_method(selection.method, self.config)
        if not selection.query.strip():
            raise DomainError("RESEARCH_QUERY_INVALID", "检索角色必须提供非空查询。", 502)

    async def draft(self, state: _State) -> _State:
        role = SINGLE_WRITER if self.config.mode == "single" else WRITER
        payload = self._input(evidence=_source_payload(state["sources"]))
        if self.config.mode == "single":
            messages = [json.loads(item) for item in state["tasks"][0].conversation]
            messages.append(
                {
                    "role": "user",
                    "content": _json(
                        {
                            "step": "draft",
                            "instruction": "使用已返回的原文提交逐条结论及逐字引用。",
                        }
                    ),
                }
            )
        else:
            messages = self._messages(role, payload)
        reply = await self._ask(
            role,
            messages,
            ResearchDraft,
            validate=lambda draft: validate_draft(draft, state["sources"]),
        )
        return {"draft_result": reply.value}

    async def verify(self, state: _State) -> _State:
        draft = state["draft_result"]
        if not draft.claims:
            verification = ResearchVerification(verdicts=())
        else:
            payload = self._input(
                claims=draft.model_dump(mode="json")["claims"],
                evidence=_source_payload(state["sources"]),
            )
            reply = await self._ask(
                VERIFIER,
                self._messages(VERIFIER, payload),
                ResearchVerification,
                validate=lambda value: validate_verification(value, draft),
            )
            verification = reply.value
        self._verification = verification
        return {"verification": verification}

    def result(self, state: _State) -> dict[str, Any]:
        draft = state["draft_result"]
        # The same deterministic gate is used after the final role in both modes.
        validate_draft(draft, state["sources"])
        validate_verification(state["verification"], draft)
        verdicts = {item.claim_id: item for item in state["verification"].verdicts}
        claims = [claim for claim in draft.claims if verdicts[claim.claim_id].supported]
        sources = {source.source_id: source for source in state["sources"]}
        citations = []
        for claim in claims:
            source = sources[claim.source_id]
            start = source.text.index(claim.quote)
            citations.append(
                {
                    **source.as_dict(),
                    "claim_id": claim.claim_id,
                    "quote": claim.quote,
                    "quote_start": start,
                    "quote_end": start + len(claim.quote),
                }
            )
        status = "answered" if len(claims) == len(draft.claims) else "partially_supported"
        if not claims:
            status = "insufficient_evidence"
        answer = (
            "\n\n".join(
                f"{index}. {claim.claim} [{claim.source_id}]"
                for index, claim in enumerate(claims, start=1)
            )
            if claims
            else "现有来源不足以支持可核验的结论。"
        )
        return {
            "answer": answer,
            "status": status,
            "citations": citations,
            "research": self.metadata(),
        }

    def error(self, error: DomainError) -> DomainError:
        self.error_code = error.code
        return DomainError(
            error.code,
            error.message,
            error.status_code,
            {**error.details, "research": self.metadata()},
        )

    def emit_verification_trace(self, trace: Trace) -> None:
        if self._verification is None:
            return
        verdicts = self._verification.verdicts
        accepted = sum(verdict.supported for verdict in verdicts)
        rejected = len(verdicts) - accepted
        status = "ok" if accepted and not rejected else "partial" if accepted else "rejected"
        trace.add(
            "research.verification",
            "逐项证据核验",
            status,
            f"支持 {accepted} 条；拒绝 {rejected} 条。" if verdicts else "没有可提交核验的结论。",
            perf_counter(),
        )

    def metadata(self) -> dict[str, Any]:
        plan = self._plan.model_dump(mode="json")["tasks"] if self._plan else []
        completed = []
        for task in plan:
            result = self._completed.get(task["id"])
            if result is not None:
                completed.append(
                    {
                        "task_id": result.task_id,
                        "query": result.query,
                        "method": result.method,
                        "source_ids": [source.source_id for source in result.evidence],
                    }
                )
        return {
            "mode": self.config.mode,
            "workflow": "langgraph",
            "role_design": "single_researcher_shared_verifier"
            if self.config.mode == "single"
            else "planner_retrievers_writer_verifier",
            "outcome": self.outcome,
            "consultation_only": True,
            "plan": plan,
            "completed_tasks": completed,
            "verification": self._verification.model_dump(mode="json")["verdicts"]
            if self._verification
            else [],
            "available_methods": list(self.config.available_methods),
            "budgets": self.config.budgets(),
            "duration_ms": round((perf_counter() - self.started) * 1000, 3),
            **self.budget.report(),
        }


def _parse(
    reply: ModelReply,
    tool_name: str,
    contract: type[Contract],
) -> tuple[Contract, str, dict[str, Any]]:
    if (
        not isinstance(reply.calls, tuple)
        or len(reply.calls) != 1
        or not isinstance(reply.calls[0], ToolCall)
        or reply.calls[0].name != tool_name
    ):
        raise DomainError(
            "MODEL_PROTOCOL_ERROR", "研究角色必须且只能调用当前的结构化响应工具。", 502
        )
    call = reply.calls[0]
    if (
        not isinstance(call.arguments, str)
        or len(call.arguments) > MAX_TOOL_ARGUMENT_CHARS
        or not isinstance(call.id, str)
        or not call.id
        or len(call.id) > 200
    ):
        raise DomainError("MODEL_PROTOCOL_ERROR", "研究角色返回了无效或过大的工具参数。", 502)
    try:
        value = contract.model_validate_json(call.arguments)
    except ValidationError as exc:
        raise DomainError(
            "MODEL_PROTOCOL_ERROR",
            "研究角色的结构化输出不符合当前响应契约。",
            502,
            {"tool": tool_name},
        ) from exc
    response = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
        ],
    }
    return value, call.id, response
