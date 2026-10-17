"""Versioned Neo4j projection using explicit writer and reader connections.

READ routing selects a server; it does not grant database read-only privileges.
Deployments must configure privileges for the reader identity in Neo4j itself.
"""

import asyncio
from contextlib import AsyncExitStack
from typing import Any

from ..errors import DomainError
from ..resources import close_on_exit
from .config import GraphConfig
from .cypher import CypherPlan, compile_cypher
from .paths import GraphPath
from .projection import Projection, fact_id

SNAPSHOT_CONSTRAINT = (
    "CREATE CONSTRAINT tracedesk_snapshot_scope_v1 IF NOT EXISTS "
    "FOR (s:TDGraphSnapshot) REQUIRE (s.tenant_id,s.snapshot_id) IS UNIQUE"
)
ENTITY_CONSTRAINT = (
    "CREATE CONSTRAINT tracedesk_entity_scope_v1 IF NOT EXISTS "
    "FOR (n:TDEntity) REQUIRE (n.tenant_id,n.snapshot_id,n.name) IS UNIQUE"
)
READ_SNAPSHOT = (
    "MATCH (s:TDGraphSnapshot {tenant_id:$tenant_id,snapshot_id:$snapshot_id}) "
    "RETURN s.source_digest AS source_digest,s.projection_digest AS projection_digest,s.state AS state"
)
BEGIN_IMPORT = (
    "MERGE (s:TDGraphSnapshot {tenant_id:$tenant_id,snapshot_id:$snapshot_id}) "
    "ON CREATE SET s.source_digest=$source_digest,s.projection_digest=$projection_digest,s.state='building' "
    "RETURN s.source_digest AS source_digest,s.projection_digest AS projection_digest,s.state AS state"
)
IMPORT_NODES = (
    "UNWIND $nodes AS name "
    "MERGE (n:TDEntity {tenant_id:$tenant_id,snapshot_id:$snapshot_id,name:name})"
)
IMPORT_FACTS = (
    "UNWIND $facts AS fact "
    "MATCH (a:TDEntity {tenant_id:$tenant_id,snapshot_id:$snapshot_id,name:fact.subject}) "
    "MATCH (b:TDEntity {tenant_id:$tenant_id,snapshot_id:$snapshot_id,name:fact.object}) "
    "MERGE (a)-[r:TD_REL {tenant_id:$tenant_id,snapshot_id:$snapshot_id,fact_id:fact.fact_id}]->(b) "
    "SET r.predicate=fact.predicate,r.source_ids=fact.source_ids"
)
FINISH_IMPORT = (
    "MATCH (s:TDGraphSnapshot {tenant_id:$tenant_id,snapshot_id:$snapshot_id}) "
    "SET s.state='ready',s.fact_count=$fact_count"
)


class Neo4jProjection:
    def __init__(
        self,
        *,
        reader_driver: Any,
        writer_driver: Any,
        database: str = "neo4j",
        timeout_seconds: float = 10.0,
        owns_drivers: bool = False,
    ) -> None:
        if reader_driver is None or writer_driver is None:
            raise ValueError("Neo4j reader and writer drivers must be explicit.")
        self.reader_driver = reader_driver
        self.writer_driver = writer_driver
        self.database = database
        self.timeout_seconds = timeout_seconds
        self.owns_drivers = owns_drivers

    @classmethod
    def from_config(cls, config: GraphConfig) -> "Neo4jProjection":
        credentials = (
            config.neo4j_uri,
            config.neo4j_reader_user,
            config.neo4j_reader_password,
            config.neo4j_writer_user,
            config.neo4j_writer_password,
        )
        if any(not value for value in credentials):
            raise DomainError(
                "neo4j_unavailable", "Neo4j 需要明确配置连接地址、读取身份和写入身份。", 503
            )
        try:
            from neo4j import AsyncGraphDatabase
        except ImportError as exc:
            raise DomainError("neo4j_unavailable", "尚未安装 Neo4j Python 驱动。", 503) from exc
        reader = AsyncGraphDatabase.driver(
            config.neo4j_uri, auth=(config.neo4j_reader_user, config.neo4j_reader_password)
        )
        writer = AsyncGraphDatabase.driver(
            config.neo4j_uri, auth=(config.neo4j_writer_user, config.neo4j_writer_password)
        )
        return cls(
            reader_driver=reader,
            writer_driver=writer,
            database=config.neo4j_database,
            timeout_seconds=config.query_timeout_seconds,
            owns_drivers=True,
        )

    async def import_projection(self, projection: Projection) -> dict[str, Any]:
        async with self.writer_driver.session(
            database=self.database, default_access_mode="WRITE"
        ) as session:
            for constraint in (SNAPSHOT_CONSTRAINT, ENTITY_CONSTRAINT):
                async with await session.begin_transaction(
                    timeout=self.timeout_seconds
                ) as transaction:
                    await (await transaction.run(constraint)).consume()
                    await transaction.commit()
            async with await session.begin_transaction(timeout=self.timeout_seconds) as transaction:
                params = {
                    **projection.scope(),
                    "source_digest": projection.source_digest,
                    "projection_digest": projection.projection_digest,
                }
                state = await (await transaction.run(BEGIN_IMPORT, params)).data()
                _check_snapshot(state, projection, allow_building=True)
                if state[0]["state"] != "ready":
                    await (
                        await transaction.run(
                            IMPORT_NODES, {**params, "nodes": list(projection.nodes)}
                        )
                    ).consume()
                    facts = [
                        {**fact.as_dict(), "fact_id": fact_id(fact)} for fact in projection.facts
                    ]
                    await (
                        await transaction.run(IMPORT_FACTS, {**params, "facts": facts})
                    ).consume()
                    await (
                        await transaction.run(FINISH_IMPORT, {**params, "fact_count": len(facts)})
                    ).consume()
                await transaction.commit()
        return {
            "state": "ready",
            "database": self.database,
            "projection_digest": projection.projection_digest,
        }

    async def execute(
        self,
        plan: CypherPlan,
        projection: Projection,
        config: GraphConfig | None = None,
    ) -> tuple[GraphPath, ...]:
        limits = config or GraphConfig(query_timeout_seconds=self.timeout_seconds)
        statement, parameters = compile_cypher(plan, projection, limits)
        try:
            rows = await asyncio.wait_for(
                self._read(statement, parameters, projection),
                timeout=self.timeout_seconds + 1,
            )
        except asyncio.TimeoutError as exc:
            raise DomainError("graph_query_timeout", "Neo4j 查询超过时间预算。", 504) from exc
        return _checked_paths(rows, plan, projection)

    async def _read(
        self,
        statement: str,
        parameters: dict[str, Any],
        projection: Projection,
    ) -> list[dict[str, Any]]:
        async with self.reader_driver.session(
            database=self.database, default_access_mode="READ"
        ) as session:
            async with await session.begin_transaction(timeout=self.timeout_seconds) as transaction:
                state = await (await transaction.run(READ_SNAPSHOT, projection.scope())).data()
                _check_snapshot(state, projection, allow_building=False)
                return await (await transaction.run(statement, parameters)).data()

    async def close(self) -> None:
        if not self.owns_drivers:
            return
        async with AsyncExitStack() as closing:
            if self.writer_driver is not self.reader_driver:
                await closing.enter_async_context(close_on_exit(self.writer_driver.close))
            await closing.enter_async_context(close_on_exit(self.reader_driver.close))


def _check_snapshot(
    rows: list[dict[str, Any]], projection: Projection, *, allow_building: bool
) -> None:
    if len(rows) != 1:
        raise DomainError("graph_projection_missing", "Neo4j 中没有唯一的目标快照。", 409)
    expected = {
        "source_digest": projection.source_digest,
        "projection_digest": projection.projection_digest,
    }
    if any(rows[0].get(key) != value for key, value in expected.items()):
        raise DomainError("graph_projection_conflict", "Neo4j 投影与知识快照内容不一致。", 409)
    states = {"ready", "building"} if allow_building else {"ready"}
    if rows[0].get("state") not in states:
        raise DomainError("graph_projection_unready", "Neo4j 投影尚未完成。", 409)


def _checked_paths(
    rows: list[dict[str, Any]],
    plan: CypherPlan,
    projection: Projection,
) -> tuple[GraphPath, ...]:
    if len(rows) > plan.limit:
        raise DomainError("graph_result_invalid", "Neo4j 返回行数超过查询预算。", 502)
    facts_by_id = {fact_id(fact): fact for fact in projection.facts}
    paths: list[GraphPath] = []
    for row in rows:
        if set(row) != {"nodes", "fact_ids", "source_ids"}:
            raise DomainError("graph_result_invalid", "Neo4j 路径字段与查询契约不一致。", 502)
        nodes, ids, sources = row["nodes"], row["fact_ids"], row["source_ids"]
        if (
            not isinstance(nodes, list)
            or not isinstance(ids, list)
            or not isinstance(sources, list)
            or len(nodes) != len(ids) + 1
            or len(sources) != len(ids)
            or any(not isinstance(node, str) for node in nodes)
            or any(not isinstance(identifier, str) for identifier in ids)
            or not plan.min_hops <= len(ids) <= plan.max_hops
            or len(ids) != len(set(ids))
            or any(identifier not in facts_by_id for identifier in ids)
            or nodes[0] != plan.start
            or (plan.end is not None and nodes[-1] != plan.end)
        ):
            raise DomainError("graph_result_invalid", "Neo4j 返回了目标快照之外的路径。", 502)
        facts = tuple(facts_by_id[identifier] for identifier in ids)
        for index, fact in enumerate(facts):
            forward = nodes[index] == fact.subject and nodes[index + 1] == fact.object
            backward = nodes[index] == fact.object and nodes[index + 1] == fact.subject
            valid_direction = (plan.direction in {"out", "both"} and forward) or (
                plan.direction in {"in", "both"} and backward
            )
            if (
                not valid_direction
                or fact.predicate not in plan.predicates
                or sources[index] != list(fact.source_ids)
            ):
                raise DomainError(
                    "graph_result_invalid", "Neo4j 路径方向、关系或来源与原始投影不一致。", 502
                )
        paths.append(GraphPath(tuple(nodes), facts))
    return tuple(paths)
