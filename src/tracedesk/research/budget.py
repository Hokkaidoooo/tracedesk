"""Per-answer reservations; workers never write a conversation or shared business state."""

import asyncio
import json
from collections.abc import Callable
from time import perf_counter
from types import TracebackType
from typing import Any, TypeVar

from ..errors import DomainError
from ..provider import ModelProvider, ModelReply
from ..trace import Trace
from .config import ResearchConfig

Result = TypeVar("Result")


class RunBudget:
    def __init__(self, config: ResearchConfig) -> None:
        self.config = config
        self.model_calls = 0
        self.retrieval_calls = 0
        self.context_chars = 0
        self._lock = asyncio.Lock()
        self.model_slots = asyncio.Semaphore(config.max_parallel)
        self._events: list[dict[str, Any]] = []

    async def model(self, role: str, step: str, task_id: str | None, chars: int) -> int:
        async with self._lock:
            self._check("max_model_calls", self.model_calls + 1)
            self._check("max_context_chars", self.context_chars + chars)
            self.model_calls += 1
            self.context_chars += chars
            return self._start(
                "model",
                role=role,
                step=step,
                task_id=task_id,
                context_chars=chars,
                total_tokens=None,
            )

    async def retrieval(self, task_id: str, query: str, method: str) -> int:
        async with self._lock:
            self._check("max_retrieval_calls", self.retrieval_calls + 1)
            self.retrieval_calls += 1
            return self._start("retrieval", task_id=task_id, query=query, method=method)

    def _check(self, name: str, requested: int) -> None:
        limit = getattr(self.config, name)
        if requested > limit:
            raise DomainError(
                "RESEARCH_BUDGET_EXCEEDED",
                "研究运行已达到共享预算，本次停止。",
                422,
                {"budget": name, "limit": limit, "requested": requested},
            )

    def _start(self, kind: str, **fields: Any) -> int:
        index = len(self._events)
        self._events.append(
            {
                "seq": index + 1,
                "kind": kind,
                "status": "running",
                "_started": perf_counter(),
                **fields,
            }
        )
        return index

    def finish(self, index: int, status: str, **fields: Any) -> None:
        event = self._events[index]
        event.update(
            status=status,
            duration_ms=round((perf_counter() - event["_started"]) * 1000, 3),
            **fields,
        )

    def report(self) -> dict[str, Any]:
        events = [
            {key: value for key, value in item.items() if not key.startswith("_")}
            for item in self._events
        ]
        roles = [item for item in events if item["kind"] == "model"]
        usages = [item["total_tokens"] for item in roles]
        total = sum(usages) if usages and all(value is not None for value in usages) else None
        return {
            "model_calls": self.model_calls,
            "retrieval_calls": self.retrieval_calls,
            "context_chars": self.context_chars,
            "total_tokens": total,
            "roles": roles,
            "retrievals": [item for item in events if item["kind"] == "retrieval"],
        }

    def emit_trace(self, trace: Trace) -> None:
        for event in self._events:
            detail = event.get("error_code") or event.get("step") or event["kind"]
            if event.get("task_id"):
                detail = f"{event['task_id']}: {detail}"
            trace.add(
                f"research.{event['kind']}",
                event.get("role", "检索服务"),
                event["status"],
                detail,
                perf_counter() - event.get("duration_ms", 0) / 1000,
            )


class BudgetedModelProvider:
    """Research roles and nested retrieval models share this single request boundary."""

    def __init__(
        self,
        provider: ModelProvider,
        budget: RunBudget,
        *,
        role: str,
        step: str,
        task_id: str | None = None,
    ) -> None:
        self.provider = provider
        self.budget = budget
        self.role = role
        self.step = step
        self.task_id = task_id
        self._active: set[asyncio.Task[Any]] = set()
        self._closed = False

    async def __aenter__(self) -> "BudgetedModelProvider":
        self._require_open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._closed = True
        active = tuple(self._active)
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
            if exc is None:
                raise DomainError(
                    "RESEARCH_MODEL_TASK_UNFINISHED",
                    "取证服务返回时仍有未结束的模型调用。",
                    502,
                    {"task_id": self.task_id},
                )

    def _require_open(self) -> None:
        if self._closed:
            raise DomainError(
                "RESEARCH_MODEL_SCOPE_CLOSED",
                "取证任务已结束，不能继续调用模型。",
                502,
                {"task_id": self.task_id},
            )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelReply:
        return await self.validated(messages, tools, lambda reply: reply)

    async def validated(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        validate: Callable[[ModelReply], Result],
    ) -> Result:
        self._require_open()
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Budgeted model calls require an asyncio task.")
        self._active.add(task)
        try:
            async with self.budget.model_slots:
                return await self._invoke(messages, tools, validate)
        finally:
            self._active.remove(task)

    async def _invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        validate: Callable[[ModelReply], Result],
    ) -> Result:
        chars = len(
            json.dumps(
                {"messages": messages, "tools": tools},
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
        index = await self.budget.model(self.role, self.step, self.task_id, chars)
        tokens: int | None = None
        try:
            reply = await self.provider.complete(messages, tools)
            if not isinstance(reply, ModelReply):
                raise DomainError("MODEL_PROTOCOL_ERROR", "模型没有返回标准 ModelReply。", 502)
            tokens = reply.total_tokens
            if tokens is not None and (type(tokens) is not int or tokens < 0):
                tokens = None
                raise DomainError("MODEL_PROTOCOL_ERROR", "模型返回了无效 token 用量。", 502)
            result = validate(reply)
        except asyncio.CancelledError:
            self.budget.finish(index, "cancelled", total_tokens=tokens)
            raise
        except DomainError as exc:
            self.budget.finish(index, "error", total_tokens=tokens, error_code=exc.code)
            raise
        except Exception as exc:
            self.budget.finish(
                index, "error", total_tokens=tokens, error_code="RESEARCH_MODEL_FAILURE"
            )
            raise DomainError(
                "RESEARCH_MODEL_FAILURE",
                "研究角色的模型调用失败。",
                502,
                {"role": self.role, "cause_type": type(exc).__name__},
            ) from exc
        self.budget.finish(index, "ok", total_tokens=tokens)
        return result
