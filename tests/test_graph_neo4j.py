import asyncio

import pytest

from tracedesk.errors import DomainError
from tracedesk.evidence import Evidence, GraphFact
from tracedesk.graphs import CypherPlan, GraphConfig, Neo4jProjection
from tracedesk.graphs.neo4j import BEGIN_IMPORT, FINISH_IMPORT, IMPORT_FACTS, READ_SNAPSHOT
from tracedesk.graphs.projection import build_projection, fact_id


def projection():
    evidence = (Evidence("s", "tenant-a", "v1", "d", 1, "说明", "Alpha USES Beta", "a" * 64),)
    return build_projection(evidence, (GraphFact("Alpha", "USES", "Beta", ("s",)),), GraphConfig())


class Result:
    def __init__(self, rows):
        self.rows = rows

    async def data(self):
        return self.rows

    async def consume(self):
        return None


class Transaction:
    def __init__(self, driver):
        self.driver = driver
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        if not self.committed:
            self.driver.rollbacks += 1

    async def run(self, statement, parameters=None):
        self.driver.calls.append((statement, parameters))
        if statement == self.driver.fail_at:
            raise RuntimeError("injected write failure")
        if statement in {BEGIN_IMPORT, READ_SNAPSHOT}:
            state = "building" if statement == BEGIN_IMPORT else "ready"
            return Result([{**self.driver.snapshot, "state": state}])
        if statement.startswith("MATCH p="):
            return Result(self.driver.rows)
        return Result([])

    async def commit(self):
        self.committed = True
        self.driver.commits += 1


class Session:
    def __init__(self, driver):
        self.driver = driver

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def begin_transaction(self, *, timeout):
        self.driver.timeouts.append(timeout)
        return Transaction(self.driver)


class Driver:
    """Checks the driver contract; it does not claim a live database test."""

    def __init__(self, graph, *, fail_at=None):
        self.snapshot = {
            "source_digest": graph.source_digest,
            "projection_digest": graph.projection_digest,
        }
        self.rows = [
            {
                "nodes": ["Alpha", "Beta"],
                "fact_ids": [fact_id(graph.facts[0])],
                "source_ids": [["s"]],
            }
        ]
        self.calls = []
        self.modes = []
        self.timeouts = []
        self.commits = 0
        self.rollbacks = 0
        self.fail_at = fail_at
        self.closed = False

    def session(self, *, database, default_access_mode):
        assert database == "isolated"
        self.modes.append(default_access_mode)
        return Session(self)

    async def close(self):
        self.closed = True


def test_writer_and_reader_are_separate_and_import_is_versioned():
    graph = projection()
    reader, writer = Driver(graph), Driver(graph)
    backend = Neo4jProjection(
        reader_driver=reader, writer_driver=writer, database="isolated", timeout_seconds=3
    )
    artifact = asyncio.run(backend.import_projection(graph))
    result = asyncio.run(backend.execute(CypherPlan("Alpha", ("USES",)), graph))
    assert artifact["state"] == "ready" and result[0].facts == graph.facts
    assert writer.modes == ["WRITE"] and reader.modes == ["READ"]
    assert writer.commits == 3
    assert all(timeout == 3 for timeout in reader.timeouts + writer.timeouts)
    for statement, parameters in reader.calls + writer.calls:
        assert "tenant-a" not in statement and "Alpha" not in statement
        if parameters is not None:
            assert parameters["tenant_id"] == "tenant-a" and parameters["snapshot_id"] == "v1"


def test_failed_import_rolls_back_before_ready_state():
    graph = projection()
    writer = Driver(graph, fail_at=IMPORT_FACTS)
    backend = Neo4jProjection(
        reader_driver=Driver(graph), writer_driver=writer, database="isolated"
    )
    with pytest.raises(RuntimeError, match="injected write failure"):
        asyncio.run(backend.import_projection(graph))
    assert writer.commits == 2 and writer.rollbacks == 1
    assert FINISH_IMPORT not in [statement for statement, _ in writer.calls]


def test_conflicting_snapshot_digest_prevents_path_query():
    graph = projection()
    reader = Driver(graph)
    reader.snapshot["source_digest"] = "different"
    backend = Neo4jProjection(
        reader_driver=reader, writer_driver=Driver(graph), database="isolated"
    )
    with pytest.raises(DomainError) as error:
        asyncio.run(backend.execute(CypherPlan("Alpha", ("USES",)), graph))
    assert error.value.code == "graph_projection_conflict"
    assert len(reader.calls) == 1


@pytest.mark.parametrize(
    "row",
    [
        {"nodes": ["Alpha", "Other"], "fact_ids": ["unknown"], "source_ids": [["s"]]},
        {"nodes": ["Alpha", "Beta"], "fact_ids": None, "source_ids": [["s"]]},
        {"nodes": ["Alpha", "Beta"], "fact_ids": [[]], "source_ids": [["s"]]},
        {"nodes": ["Beta", "Alpha"], "fact_ids": "valid", "source_ids": [["s"]]},
        {"nodes": ["Alpha", "Beta"], "fact_ids": "valid", "source_ids": [["other-tenant-source"]]},
    ],
)
def test_database_results_are_validated_against_canonical_facts(row):
    graph = projection()
    reader = Driver(graph)
    if row["fact_ids"] == "valid":
        row = {**row, "fact_ids": [fact_id(graph.facts[0])]}
    reader.rows = [row]
    backend = Neo4jProjection(
        reader_driver=reader, writer_driver=Driver(graph), database="isolated"
    )
    with pytest.raises(DomainError) as error:
        asyncio.run(backend.execute(CypherPlan("Alpha", ("USES",)), graph))
    assert error.value.code == "graph_result_invalid"


def test_only_owned_drivers_are_closed():
    graph = projection()
    reader, writer = Driver(graph), Driver(graph)
    borrowed = Neo4jProjection(reader_driver=reader, writer_driver=writer, database="isolated")
    asyncio.run(borrowed.close())
    assert not reader.closed and not writer.closed
    owned = Neo4jProjection(
        reader_driver=reader, writer_driver=writer, database="isolated", owns_drivers=True
    )
    asyncio.run(owned.close())
    assert reader.closed and writer.closed
