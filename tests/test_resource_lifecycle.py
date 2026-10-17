"""Application shutdown preserves every owned resource failure in the exception chain."""

import asyncio
from types import SimpleNamespace

import pytest

from tracedesk.bootstrap import Services
from tracedesk.provider import ChatCompletionsProvider


def test_shutdown_preserves_document_graph_and_provider_close_failures():
    events = []

    async def close_documents():
        events.append("documents")
        raise RuntimeError("documents close failed")

    async def close_graphs():
        events.append("graphs")
        raise RuntimeError("graphs close failed")

    class Provider(ChatCompletionsProvider):
        def __init__(self):
            pass

        async def close(self):
            events.append("provider")
            raise RuntimeError("provider close failed")

    services = Services(
        settings=None,
        store=None,
        knowledge=None,
        refunds=None,
        agent=None,
        provider=Provider(),
        documents=SimpleNamespace(close=close_documents),
        graphs=SimpleNamespace(close=close_graphs),
        workflows=None,
    )
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(services.close())
    assert events == ["documents", "graphs", "provider"]
    messages = []
    error = failure.value
    while error is not None:
        messages.append(str(error))
        error = error.__context__
    assert messages == ["provider close failed", "graphs close failed", "documents close failed"]
