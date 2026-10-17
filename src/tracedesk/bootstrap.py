"""Assembly root; dependencies are injected into the agent and domain services."""

import json
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib.resources import as_file, files
from typing import Any

from .agent import SupportAgent
from .graphs import GraphService
from .graphs.examples import SemanticCypherExamples
from .knowledge import KnowledgeService
from .knowledge_workflows import KnowledgeWorkflows
from .provider import ChatCompletionsProvider, ModelProvider
from .refunds import RefundService
from .resources import close_on_exit
from .retrieval import KnowledgeBase
from .settings import Settings
from .store import Store


def resource(name: str) -> Any:
    return files("tracedesk").joinpath("data", name)


def load_knowledge() -> KnowledgeBase:
    with as_file(resource("knowledge.json")) as path:
        return KnowledgeBase.from_path(path)


@dataclass(frozen=True)
class Services:
    settings: Settings
    store: Store
    knowledge: KnowledgeBase
    refunds: RefundService
    agent: SupportAgent
    provider: ModelProvider | None
    documents: KnowledgeService
    graphs: GraphService
    workflows: KnowledgeWorkflows

    async def close(self) -> None:
        async with AsyncExitStack() as closing:
            if isinstance(self.provider, ChatCompletionsProvider):
                await closing.enter_async_context(close_on_exit(self.provider.close))
            await closing.enter_async_context(close_on_exit(self.graphs.close))
            await closing.enter_async_context(close_on_exit(self.documents.close))


def create_services(
    settings: Settings,
    *,
    provider: ModelProvider | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Services:
    now = clock or (lambda: datetime.now(timezone.utc))
    store = Store(settings.database_path, clock=now, run_lease_seconds=settings.run_lease_seconds)
    data = json.loads(resource("orders.json").read_text(encoding="utf-8"))
    reference_time = now()
    orders = []
    for source in data["orders"]:
        days = source["delivered_days_ago"]
        order = {key: value for key, value in source.items() if key != "delivered_days_ago"}
        order["delivered_at"] = (
            (reference_time - timedelta(days=days)).isoformat() if days is not None else None
        )
        orders.append(order)
    store.initialize(data["customers"], orders)
    knowledge = load_knowledge()
    refunds = RefundService(store, knowledge, action_ttl_seconds=settings.action_ttl_seconds)
    if settings.provider == "llm" and provider is None:
        provider = ChatCompletionsProvider(settings)
    documents = KnowledgeService(store, settings.knowledge)
    examples = SemanticCypherExamples(documents._embedder, settings.knowledge)
    graphs = GraphService(settings.graphs, provider=provider, examples=examples)
    workflows = KnowledgeWorkflows(
        store,
        documents,
        graphs,
        provider,
        settings.research,
        history_limit=settings.max_history_turns,
    )
    agent = SupportAgent(store, knowledge, refunds, settings, provider, workflows)
    return Services(
        settings, store, knowledge, refunds, agent, provider, documents, graphs, workflows
    )
