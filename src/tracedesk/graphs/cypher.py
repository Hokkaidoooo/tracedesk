"""Parser and compiler for a deliberately small, read-only Cypher language.

Only this grammar is accepted; submitted text is never sent to the database.
MATCH p = (a:Entity {name: STRING})-[:TYPE[|TYPE]*[ *MIN..MAX ]]->(b:Entity)
RETURN p LIMIT INTEGER
The target may have a name map, and incoming/undirected edges are supported.
"""

import json
import re
from dataclasses import dataclass
from typing import Any

from ..errors import DomainError
from .config import GraphConfig
from .paths import walk
from .projection import Projection


@dataclass(frozen=True)
class CypherPlan:
    start: str
    predicates: tuple[str, ...]
    end: str | None = None
    min_hops: int = 1
    max_hops: int = 1
    direction: str = "out"
    limit: int = 5


@dataclass(frozen=True)
class Token:
    kind: str
    value: str


def _invalid(message: str) -> DomainError:
    return DomainError("cypher_invalid", message, 422)


def _tokens(query: str) -> tuple[Token, ...]:
    if not isinstance(query, str) or not query.strip() or len(query) > 8000:
        raise _invalid("查询文本为空或超过受限语法长度。")
    result: list[Token] = []
    offset = 0
    while offset < len(query):
        char = query[offset]
        if char.isspace():
            offset += 1
            continue
        pair = query[offset : offset + 2]
        if pair in {"->", "<-", ".."}:
            result.append(Token(pair, pair))
            offset += 2
            continue
        if char in "(){}[]:=|*,-":
            result.append(Token(char, char))
            offset += 1
            continue
        if char in {'"', "'"}:
            value, offset = _string(query, offset)
            result.append(Token("STRING", value))
            continue
        if char == "`":
            close = query.find("`", offset + 1)
            if close < 0 or close == offset + 1:
                raise _invalid("关系标识符未闭合或为空。")
            value = query[offset + 1 : close]
            if any(ord(c) < 32 for c in value) or len(value) > 256:
                raise _invalid("关系标识符无效。")
            result.append(Token("QUOTED_ID", value))
            offset = close + 1
            continue
        number = re.match(r"[0-9]+", query[offset:])
        if number:
            if len(number.group()) > 6:
                raise _invalid("查询整数超过允许的长度。")
            result.append(Token("INTEGER", number.group()))
            offset += len(number.group())
            continue
        identifier = re.match(r"(?:[^\W\d]|_)\w*", query[offset:], re.UNICODE)
        if identifier:
            result.append(Token("ID", identifier.group()))
            offset += len(identifier.group())
            continue
        raise _invalid(f"受限语法不接受位置 {offset} 的符号。")
    return tuple(result)


def _string(query: str, start: int) -> tuple[str, int]:
    quote = query[start]
    if quote == '"':
        try:
            value, consumed = json.JSONDecoder().raw_decode(query[start:])
        except json.JSONDecodeError as exc:
            raise _invalid("查询字符串未正确闭合。") from exc
        return value, start + consumed
    chars: list[str] = []
    offset = start + 1
    while offset < len(query):
        char = query[offset]
        if char == "'":
            return "".join(chars), offset + 1
        if char == "\\":
            offset += 1
            if offset >= len(query) or query[offset] not in {"'", "\\"}:
                raise _invalid("单引号字符串仅支持引号或反斜杠转义。")
            char = query[offset]
        if ord(char) < 32:
            raise _invalid("查询字符串含控制字符。")
        chars.append(char)
        offset += 1
    raise _invalid("查询字符串未正确闭合。")


class _Parser:
    def __init__(self, query: str) -> None:
        self.tokens = _tokens(query)
        self.offset = 0

    def take(self, kind: str, value: str | None = None) -> str:
        if self.offset >= len(self.tokens):
            raise _invalid(f"查询结束前缺少 {value or kind}。")
        token = self.tokens[self.offset]
        if token.kind != kind or (value is not None and token.value.upper() != value.upper()):
            raise _invalid(f"此处只允许 {value or kind}。")
        self.offset += 1
        return token.value

    def peek(self, kind: str) -> bool:
        return self.offset < len(self.tokens) and self.tokens[self.offset].kind == kind

    def node(self) -> tuple[str, str | None]:
        self.take("(")
        variable = self.take("ID")
        self.take(":")
        label = self.take("ID")
        if label != "Entity":
            raise _invalid("仅允许 Entity 节点标签。")
        name = None
        if self.peek("{"):
            self.take("{")
            if self.take("ID") != "name":
                raise _invalid("仅允许 name 节点属性。")
            self.take(":")
            name = self.take("STRING")
            self.take("}")
        self.take(")")
        return variable, name

    def relation(self) -> str:
        kind = "QUOTED_ID" if self.peek("QUOTED_ID") else "ID"
        return self.take(kind)

    def parse(self) -> CypherPlan:
        self.take("ID", "MATCH")
        path = self.take("ID")
        self.take("=")
        start_variable, start = self.node()
        incoming = self.peek("<-")
        self.take("<-" if incoming else "-")
        self.take("[")
        self.take(":")
        predicates = [self.relation()]
        while self.peek("|"):
            self.take("|")
            predicates.append(self.relation())
        low = high = 1
        if self.peek("*"):
            self.take("*")
            low = int(self.take("INTEGER"))
            self.take("..")
            high = int(self.take("INTEGER"))
        self.take("]")
        if incoming:
            self.take("-")
            direction = "in"
        elif self.peek("->"):
            self.take("->")
            direction = "out"
        else:
            self.take("-")
            direction = "both"
        end_variable, end = self.node()
        self.take("ID", "RETURN")
        returned = self.take("ID")
        self.take("ID", "LIMIT")
        limit = int(self.take("INTEGER"))
        if self.offset != len(self.tokens) or len({path, start_variable, end_variable}) != 3:
            raise _invalid("查询包含多余子句或重复变量。")
        if returned != path or start is None:
            raise _invalid("查询必须绑定起点名称并只返回已匹配的路径。")
        return CypherPlan(start, tuple(predicates), end, low, high, direction, limit)


def parse_cypher(query: str) -> CypherPlan:
    return _Parser(query).parse()


def validate_plan(plan: CypherPlan, projection: Projection, config: GraphConfig) -> None:
    if not isinstance(plan, CypherPlan):
        raise _invalid("查询必须先解析为 CypherPlan。")
    if plan.direction not in {"out", "in", "both"}:
        raise _invalid("查询方向无效。")
    if any(type(value) is not int for value in (plan.min_hops, plan.max_hops, plan.limit)):
        raise _invalid("路径长度和行数必须是整数。")
    if not 1 <= plan.min_hops <= plan.max_hops <= config.max_hops:
        raise _invalid("路径长度超过受限查询预算。")
    if not 1 <= plan.limit <= config.max_rows:
        raise _invalid("返回行数超过受限查询预算。")
    if plan.start not in projection.nodes or (
        plan.end is not None and plan.end not in projection.nodes
    ):
        raise _invalid("查询实体不属于当前快照。")
    if (
        not isinstance(plan.predicates, tuple)
        or not plan.predicates
        or any(not isinstance(predicate, str) for predicate in plan.predicates)
        or len(set(plan.predicates)) != len(plan.predicates)
        or any(predicate not in projection.predicates for predicate in plan.predicates)
    ):
        raise _invalid("查询关系不属于当前快照或重复。")
    # The projection bounds the actual search space before any database request.
    walk(
        projection,
        (plan.start,),
        config=config,
        max_hops=plan.max_hops,
        min_hops=plan.min_hops,
        predicates=plan.predicates,
        direction=plan.direction,
        end=plan.end,
    )


def compile_cypher(
    plan: CypherPlan,
    projection: Projection,
    config: GraphConfig,
) -> tuple[str, dict[str, Any]]:
    validate_plan(plan, projection, config)
    left, right = {"out": ("-", "->"), "in": ("<-", "-"), "both": ("-", "-")}[plan.direction]
    relationship = f"{left}[:TD_REL*{plan.min_hops}..{plan.max_hops}]{right}"
    statement = (
        "MATCH p=(a:TDEntity {tenant_id:$tenant_id, snapshot_id:$snapshot_id, name:$start_name})"
        + relationship
        + "(b:TDEntity) "
        "WHERE all(n IN nodes(p) WHERE n.tenant_id=$tenant_id AND n.snapshot_id=$snapshot_id) "
        "AND all(r IN relationships(p) WHERE r.tenant_id=$tenant_id AND r.snapshot_id=$snapshot_id "
        "AND r.predicate IN $predicates) "
        "AND ($end_name IS NULL OR b.name=$end_name) "
        "RETURN [n IN nodes(p) | n.name] AS nodes, "
        "[r IN relationships(p) | r.fact_id] AS fact_ids, "
        "[r IN relationships(p) | r.source_ids] AS source_ids "
        "ORDER BY size(nodes), nodes, fact_ids LIMIT $limit"
    )
    return statement, {
        **projection.scope(),
        "start_name": plan.start,
        "end_name": plan.end,
        "predicates": list(plan.predicates),
        "limit": plan.limit,
    }
