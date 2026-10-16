"""Assembly root; dependencies are injected into the agent and domain services."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib.resources import as_file, files
from typing import Any

from .agent import SupportAgent
from .provider import ChatCompletionsProvider, ModelProvider
from .refunds import RefundService
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
    agent = SupportAgent(store, knowledge, refunds, settings, provider)
    return Services(settings, store, knowledge, refunds, agent, provider)
