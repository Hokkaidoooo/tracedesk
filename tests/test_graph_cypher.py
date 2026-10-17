import asyncio
import json

import pytest

from tracedesk.errors import DomainError
from tracedesk.evidence import Evidence, GraphFact
from tracedesk.graphs import (
    CypherExample,
    GraphConfig,
    GraphService,
    Text2Cypher,
    compile_cypher,
    parse_cypher,
    validate_plan,
)
from tracedesk.graphs.paths import walk
from tracedesk.graphs.projection import build_projection
from tracedesk.provider import ModelReply, ToolCall

VALID = 'MATCH p=(a:Entity {name: "Alpha"})-[:USES|FOLLOWS*1..2]->(b:Entity) RETURN p LIMIT 5'


def projection():
    source = (
        Evidence("s", "t", "v", "d", 1, "说明", "Alpha USES Beta. Beta FOLLOWS Gamma.", "a" * 64),
    )
    facts = (
        GraphFact("Alpha", "USES", "Beta", ("s",)),
        GraphFact("Beta", "FOLLOWS", "Gamma", ("s",)),
    )
    return build_projection(source, facts, GraphConfig())


@pytest.mark.parametrize(
    "query",
    [
        VALID + "; MATCH (n) RETURN n",
        VALID + " UNION MATCH (n) RETURN n",
        VALID + " // comment",
        VALID + " /* comment */",
        "CALL db.labels()",
        "CREATE (n)",
        "MERGE (n)",
        "LOAD CSV FROM 'x' AS row RETURN row",
        VALID.replace("RETURN p", "DELETE a RETURN p"),
        VALID.replace("RETURN p", "SET a.name='evil' RETURN p"),
        VALID.replace("Entity", "Secret"),
        VALID.replace("name:", "tenant_id:"),
        VALID.replace("*1..2", "*"),
        VALID.replace("*1..2", "*1..99"),
        VALID.replace("LIMIT 5", "LIMIT 0"),
        VALID.replace("LIMIT 5", "LIMIT 999"),
        VALID.replace("USES|FOLLOWS", "UNKNOWN"),
        VALID.replace('"Alpha"', '"missing"'),
        VALID.replace("RETURN p", "RETURN a.password"),
        VALID.replace("RETURN p", "WITH p RETURN p"),
        VALID.replace("LIMIT 5", "LIMIT -5"),
        VALID.replace("LIMIT 5", "LIMIT " + "9" * 5000),
        VALID.replace('{name: "Alpha"}', '{name: "Alpha", snapshot_id: "other"}'),
        VALID.replace("p=(", "a=("),
        VALID.replace("(b:Entity)", "(a:Entity)"),
        "EXPLAIN " + VALID,
        "PROFILE " + VALID,
    ],
)
def test_unsafe_or_out_of_schema_queries_never_compile(query):
    with pytest.raises(DomainError):
        compile_cypher(parse_cypher(query), projection(), GraphConfig())


def test_parser_recompiles_literals_and_forces_snapshot_scope():
    plan = parse_cypher(VALID)
    statement, parameters = compile_cypher(plan, projection(), GraphConfig())
    assert "Alpha" not in statement and "USES" not in statement
    assert parameters == {
        "tenant_id": "t",
        "snapshot_id": "v",
        "start_name": "Alpha",
        "end_name": None,
        "predicates": ["USES", "FOLLOWS"],
        "limit": 5,
    }
    assert "all(n IN nodes(p)" in statement and "all(r IN relationships(p)" in statement
    assert "LIMIT $limit" in statement


def test_write_keyword_inside_a_real_entity_is_safe_data():
    name = "DELETE 'x' MATCH (secret)"
    evidence = (Evidence("s", "t", "v", "d", 1, "记录", name + " USES Beta", "a" * 64),)
    graph = build_projection(evidence, (GraphFact(name, "USES", "Beta", ("s",)),), GraphConfig())
    query = f"MATCH p=(a:Entity {{name: {json.dumps(name)}}})-[:USES]->(b:Entity) RETURN p LIMIT 1"
    statement, parameters = compile_cypher(parse_cypher(query), graph, GraphConfig())
    assert name not in statement
    assert parameters["start_name"] == name


def test_incoming_and_undirected_ast_preserve_direction():
    incoming = parse_cypher(
        'MATCH p=(a:Entity {name: "Gamma"})<-[:FOLLOWS*1..1]-(b:Entity) RETURN p LIMIT 2'
    )
    validate_plan(incoming, projection(), GraphConfig())
    assert incoming.direction == "in"
    statement, _ = compile_cypher(incoming, projection(), GraphConfig())
    assert "<-[:TD_REL*1..1]-" in statement
    both = parse_cypher(VALID.replace("->", "-"))
    assert both.direction == "both"


class ScriptedProvider:
    def __init__(self, queries):
        self.queries = iter(queries)
        self.calls = 0

    async def complete(self, messages, tools):
        self.calls += 1
        arguments = json.dumps({"cypher": next(self.queries)})
        call = ToolCall(str(self.calls), "submit_cypher", arguments)
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": arguments},
                },
            ],
        }
        return ModelReply((call,), message)


class ProjectionFixture:
    """A protocol fixture, not a live Neo4j server."""

    def __init__(self):
        self.executed = []
        self.closed = False

    async def import_projection(self, graph):
        return {"state": "ready", "projection_digest": graph.projection_digest}

    async def execute(self, plan, graph, config):
        self.executed.append(plan)
        return walk(
            graph, (plan.start,), config=config, max_hops=plan.max_hops, predicates=plan.predicates
        )

    async def close(self):
        self.closed = True


class ExampleFixture:
    async def retrieve(self, query, *, limit):
        return (CypherExample("Alpha 的关联", VALID),)


def test_corrected_model_query_is_revalidated_before_one_database_call_and_uses_override():
    graph = projection()
    fixture = ProjectionFixture()
    original = ScriptedProvider([])
    provider = ScriptedProvider(["CREATE (n)", VALID])
    service = GraphService(
        GraphConfig(backend="neo4j"), provider=original, neo4j=fixture, examples=ExampleFixture()
    )
    artifact = asyncio.run(service.prepare(graph.evidence, facts=graph.facts))
    result = asyncio.run(
        service.search("Alpha", graph.evidence, artifact, method="text2cypher", provider=provider)
    )
    assert result[0].source_id == "s"
    assert len(fixture.executed) == 1 and provider.calls == 2 and original.calls == 0
    asyncio.run(service.close())
    assert not fixture.closed
    asyncio.run(fixture.close())
    assert fixture.closed


def test_invalid_correction_cannot_enter_execute_state():
    graph = projection()
    fixture = ProjectionFixture()
    provider = ScriptedProvider(["CREATE (n)", VALID.replace("LIMIT 5", "LIMIT 999")])
    service = GraphService(
        GraphConfig(backend="neo4j"), provider=provider, neo4j=fixture, examples=ExampleFixture()
    )
    artifact = asyncio.run(service.prepare(graph.evidence, facts=graph.facts))
    with pytest.raises(DomainError) as error:
        asyncio.run(service.search("Alpha", graph.evidence, artifact, method="text2cypher"))
    assert error.value.code == "cypher_validation_failed"
    assert fixture.executed == [] and provider.calls == 2


def test_few_shot_examples_are_requested_from_injected_semantic_retriever():
    class Retriever:
        queries = []

        async def retrieve(self, query, *, limit):
            self.queries.append((query, limit))
            return (CypherExample("Alpha 的关系", VALID),)

    retriever = Retriever()
    generator = Text2Cypher(ScriptedProvider([VALID]), GraphConfig(), examples=retriever)
    asyncio.run(generator.plan("Alpha", projection()))
    assert retriever.queries == [("Alpha", 3)]


def test_search_budget_is_checked_before_database_execution():
    graph = projection()
    with pytest.raises(DomainError) as error:
        compile_cypher(parse_cypher(VALID), graph, GraphConfig(max_expansions=1))
    assert error.value.code == "graph_query_budget"
