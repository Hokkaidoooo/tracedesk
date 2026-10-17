"""Exercise real Neo4j with synthetic sources, then delete only this run's scopes."""

import argparse
import asyncio
import hashlib
import ipaddress
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4


def validate_uri(uri: str) -> str:
    parsed = urlsplit(uri)
    if (
        parsed.scheme not in {"bolt", "bolt+s", "bolt+ssc"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path
    ):
        raise ValueError("请显式提供不含凭据、路径或查询参数的 loopback Bolt URL。")
    if parsed.hostname != "localhost":
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError as exc:
            raise ValueError("仅允许 loopback 地址的隔离 Neo4j 验证实例。") from exc
        if not loopback:
            raise ValueError("仅允许 loopback 地址的隔离 Neo4j 验证实例。")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Neo4j 验证端口无效。")
    return uri


def _corpus(tenant_id: str, snapshot_id: str, edges: tuple[tuple[str, str, str], ...]):
    from tracedesk.evidence import Evidence, GraphFact

    evidence, facts = [], []
    for index, (subject, predicate, target) in enumerate(edges):
        text = f"{subject} {predicate} {target}。这是一条隔离验证用的人工关系。"
        source_id = hashlib.sha256(f"{tenant_id}:{snapshot_id}:{index}".encode()).hexdigest()
        evidence.append(
            Evidence(
                source_id,
                tenant_id,
                snapshot_id,
                f"synthetic-{index}",
                1,
                f"Neo4j 隔离验证资料 {index}",
                text,
                hashlib.sha256(text.encode()).hexdigest(),
            )
        )
        facts.append(GraphFact(subject, predicate, target, (source_id,)))
    return tuple(evidence), tuple(facts)


async def _wait_ready(driver, database: str, timeout: float) -> None:
    from neo4j.exceptions import ServiceUnavailable, SessionExpired

    deadline = time.monotonic() + timeout
    while True:
        try:
            result = await driver.execute_query("RETURN 1 AS ok", database_=database, routing_="r")
            if len(result.records) != 1 or result.records[0]["ok"] != 1:
                raise RuntimeError("Neo4j 健康查询没有返回预期结果。")
            return
        except (ServiceUnavailable, SessionExpired):
            if time.monotonic() >= deadline:
                raise
            await asyncio.sleep(0.5)


DELETE_SCOPE = (
    "MATCH ()-[r:TD_REL {tenant_id:$tenant_id,snapshot_id:$snapshot_id}]->() DELETE r",
    "MATCH (n:TDEntity {tenant_id:$tenant_id,snapshot_id:$snapshot_id}) DELETE n",
    "MATCH (s:TDGraphSnapshot {tenant_id:$tenant_id,snapshot_id:$snapshot_id}) DELETE s",
)
CHECK_SCOPE = (
    "MATCH ()-[r:TD_REL {tenant_id:$tenant_id,snapshot_id:$snapshot_id}]->() RETURN count(r) AS remaining",
    "MATCH (n:TDEntity {tenant_id:$tenant_id,snapshot_id:$snapshot_id}) RETURN count(n) AS remaining",
    "MATCH (s:TDGraphSnapshot {tenant_id:$tenant_id,snapshot_id:$snapshot_id}) RETURN count(s) AS remaining",
)


async def _cleanup(writer, database: str, scopes: list[dict[str, str]]) -> dict:
    counters = {"nodes_deleted": 0, "relationships_deleted": 0, "scopes": len(scopes)}
    for scope in scopes:
        async with writer.session(database=database, default_access_mode="WRITE") as session:
            async with await session.begin_transaction(timeout=15) as transaction:
                for statement in DELETE_SCOPE:
                    summary = await (await transaction.run(statement, scope)).consume()
                    counters["nodes_deleted"] += summary.counters.nodes_deleted
                    counters["relationships_deleted"] += summary.counters.relationships_deleted
                await transaction.commit()
            async with await session.begin_transaction(timeout=15) as transaction:
                for statement in CHECK_SCOPE:
                    rows = await (await transaction.run(statement, scope)).data()
                    if len(rows) != 1 or rows[0]["remaining"] != 0:
                        raise RuntimeError("Neo4j 本轮自有作用域清理后仍有残留。")
    return {**counters, "verified_empty": True}


async def validate(uri: str, database: str, timeout: float) -> dict:
    from neo4j import AsyncGraphDatabase

    from tracedesk.graphs import GraphConfig, GraphService, Neo4jProjection

    validate_uri(uri)
    reader = AsyncGraphDatabase.driver(
        uri, auth=None, connection_timeout=5, max_transaction_retry_time=0
    )
    writer = AsyncGraphDatabase.driver(
        uri, auth=None, connection_timeout=5, max_transaction_retry_time=0
    )
    projection = Neo4jProjection(
        reader_driver=reader,
        writer_driver=writer,
        database=database,
        timeout_seconds=15,
        owns_drivers=True,
    )
    service = GraphService(GraphConfig(backend="neo4j", query_timeout_seconds=15), neo4j=projection)
    nonce = uuid4().hex
    tenant = f"tracedesk-ci-{nonce}"
    snapshot = f"snapshot-{nonce}"
    corpora = (
        _corpus(tenant, snapshot, (("Alpha", "USES", "Beta"), ("Beta", "FOLLOWS", "Gamma"))),
        _corpus(tenant, "other-" + snapshot, (("Alpha", "USES", "Delta"),)),
        _corpus("other-" + tenant, snapshot, (("Alpha", "USES", "Epsilon"),)),
    )
    owned_scopes = []
    artifacts = []
    results = []
    try:
        await asyncio.wait_for(_wait_ready(reader, database, timeout), timeout=timeout)
        server_agent = (await reader.get_server_info()).agent
        for evidence, facts in corpora:
            owned_scopes.append(
                {"tenant_id": evidence[0].tenant_id, "snapshot_id": evidence[0].snapshot_id}
            )
            artifacts.append(await service.prepare(evidence, facts=facts))
        for (evidence, _), artifact in zip(corpora, artifacts, strict=True):
            found = await service.search("Alpha", evidence, artifact, method="graph", limit=5)
            if {item.source_id for item in found} != {item.source_id for item in evidence}:
                raise RuntimeError("Neo4j 检索混入其他作用域或遗漏本轮图路径来源。")
            if any(
                item.tenant_id != evidence[0].tenant_id
                or item.snapshot_id != evidence[0].snapshot_id
                for item in found
            ):
                raise RuntimeError("Neo4j 检索返回了不同租户或快照。")
            results.append(
                [
                    {
                        "source_id": item.source_id,
                        "source_sha256": item.source_sha256,
                        "graph_path": list(item.graph_path),
                    }
                    for item in found
                ]
            )
        if not any(
            len(item["graph_path"]) == 5 and item["graph_path"][-1] == "Gamma"
            for item in results[0]
        ):
            raise RuntimeError("Neo4j 没有实际返回 Alpha 到 Gamma 的两跳有向路径。")
    finally:
        try:
            cleanup = await _cleanup(writer, database, owned_scopes)
        finally:
            await service.close()
    return {
        "schema": 1,
        "status": "passed",
        "uri": uri,
        "database": database,
        "server_agent": server_agent,
        "synthetic_data": True,
        "chat_model_calls": 0,
        "health_query_executed": True,
        "directed_multihop_verified": True,
        "tenant_and_snapshot_isolation_verified": True,
        "scopes": owned_scopes,
        "results": results,
        "cleanup": cleanup,
        "authorization_scope": "使用无认证的隔离实例；READ 路由不作为数据库只读账号权限证明。",
    }


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")
    parser = argparse.ArgumentParser(description="真实验证显式指定的 loopback Neo4j 隔离实例。")
    parser.add_argument("--uri", required=True)
    parser.add_argument("--database", default="neo4j")
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        validate_uri(args.uri)
    except ValueError as exc:
        parser.error(str(exc))
    if not 0 < args.timeout <= 120:
        parser.error("--timeout 必须在 (0, 120] 秒内。")
    try:
        report = asyncio.run(validate(args.uri, args.database, args.timeout))
    except Exception as exc:
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "status": "failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        raise
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
