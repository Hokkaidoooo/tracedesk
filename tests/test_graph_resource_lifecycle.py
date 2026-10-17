"""A failed close cannot prevent closing another owned graph resource."""

import asyncio

import pytest

from tracedesk.graphs import GraphService
from tracedesk.graphs.neo4j import Neo4jProjection


class Resource:
    def __init__(self, name, events, *, fails=False):
        self.name = name
        self.events = events
        self.fails = fails

    async def close(self):
        self.events.append(self.name)
        if self.fails:
            raise RuntimeError(f"{self.name} close failed")


@pytest.mark.parametrize("writer_fails", [False, True])
def test_reader_close_failure_still_closes_the_owned_writer(writer_fails):
    events = []
    reader = Resource("reader", events, fails=True)
    writer = Resource("writer", events, fails=writer_fails)
    backend = Neo4jProjection(reader_driver=reader, writer_driver=writer, owns_drivers=True)
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(backend.close())
    assert events == ["reader", "writer"]
    if writer_fails:
        assert str(failure.value) == "writer close failed"
        assert str(failure.value.__context__) == "reader close failed"
    else:
        assert str(failure.value) == "reader close failed"


def test_a_shared_owned_driver_is_closed_once():
    events = []
    resource = Resource("shared", events)
    backend = Neo4jProjection(reader_driver=resource, writer_driver=resource, owns_drivers=True)
    asyncio.run(backend.close())
    assert events == ["shared"]


def test_graph_service_joins_all_owned_closures_after_neo4j_fails(monkeypatch):
    events = []
    neo4j = Resource("neo4j", events, fails=True)
    graphrag = Resource("graphrag", events)
    monkeypatch.setattr(
        "tracedesk.graphs.service.Neo4jProjection.from_config", lambda config: neo4j
    )
    monkeypatch.setattr("tracedesk.graphs.microsoft.MicrosoftGraphRag", lambda config: graphrag)
    service = GraphService()
    service._neo4j()
    service._graphrag()
    with pytest.raises(RuntimeError, match="neo4j close failed"):
        asyncio.run(service.close())
    assert events == ["neo4j", "graphrag"]


def test_injected_graph_resources_remain_owned_by_the_caller():
    events = []
    reader, writer = Resource("reader", events), Resource("writer", events)
    backend = Neo4jProjection(reader_driver=reader, writer_driver=writer)
    asyncio.run(backend.close())
    assert events == []
    service = GraphService(neo4j=Resource("neo4j", events), graphrag=Resource("graphrag", events))
    asyncio.run(service.close())
    assert events == []
