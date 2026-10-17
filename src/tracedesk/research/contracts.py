"""One structured response contract per model role and shared evidence checks."""

from collections.abc import Iterable
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ..errors import DomainError
from ..evidence import Evidence
from .config import ResearchConfig

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")]
Query = Annotated[str, StringConstraints(min_length=1, max_length=2000)]
Method = Literal[
    "lexical",
    "semantic",
    "hybrid",
    "graph",
    "text2cypher",
    "graphrag_local",
    "graphrag_global",
]


class ResponseContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ResearchTask(ResponseContract):
    id: Identifier
    query: Query
    method: Method
    depends_on: tuple[Identifier, ...]


class ResearchPlan(ResponseContract):
    tasks: tuple[ResearchTask, ...] = Field(min_length=1)


class RetrievalSelection(ResponseContract):
    query: Query
    method: Method


class ResearchClaim(ResponseContract):
    claim_id: Identifier
    claim: str = Field(min_length=1, max_length=1600)
    source_id: str = Field(min_length=1, max_length=512)
    quote: str = Field(min_length=1, max_length=4000)


class ResearchDraft(ResponseContract):
    claims: tuple[ResearchClaim, ...] = Field(max_length=24)


class ClaimVerdict(ResponseContract):
    claim_id: Identifier
    supported: bool
    reason: str = Field(min_length=1, max_length=300)


class ResearchVerification(ResponseContract):
    verdicts: tuple[ClaimVerdict, ...] = Field(max_length=24)


def validate_method(method: str, config: ResearchConfig) -> None:
    if method not in config.available_methods:
        raise DomainError(
            "RESEARCH_METHOD_UNAVAILABLE",
            "所选检索方法未在当前快照启用。",
            409,
            {"method": method, "available_methods": list(config.available_methods)},
        )


def validate_plan(plan: ResearchPlan, config: ResearchConfig) -> None:
    if len(plan.tasks) > config.max_tasks:
        raise DomainError(
            "RESEARCH_BUDGET_EXCEEDED",
            "研究计划超出任务数量预算。",
            422,
            {"budget": "max_tasks", "limit": config.max_tasks, "requested": len(plan.tasks)},
        )
    ids = {task.id for task in plan.tasks}
    if len(ids) != len(plan.tasks):
        raise DomainError("RESEARCH_PLAN_INVALID", "研究计划包含重复任务编号。", 502)
    for task in plan.tasks:
        validate_method(task.method, config)
        if not task.query.strip():
            raise DomainError("RESEARCH_PLAN_INVALID", "研究任务的查询不能为空白。", 502)
        if len(set(task.depends_on)) != len(task.depends_on):
            raise DomainError("RESEARCH_PLAN_INVALID", "研究任务包含重复依赖。", 502)
        if set(task.depends_on) - ids:
            raise DomainError("RESEARCH_PLAN_INVALID", "研究任务引用了未知依赖。", 502)
    completed: set[str] = set()
    while len(completed) < len(plan.tasks):
        ready = {
            task.id
            for task in plan.tasks
            if task.id not in completed and set(task.depends_on) <= completed
        }
        if not ready:
            raise DomainError("RESEARCH_PLAN_INVALID", "研究任务的依赖图存在循环。", 502)
        completed.update(ready)


def merge_evidence(groups: Iterable[tuple[Evidence, ...]]) -> tuple[Evidence, ...]:
    """Keep first retrieval order while enforcing one immutable source identity."""
    sources: dict[str, Evidence] = {}
    scope: tuple[str, str] | None = None
    document_hashes: dict[tuple[str, int], str] = {}
    for group in groups:
        if not isinstance(group, tuple):
            raise DomainError("RESEARCH_EVIDENCE_INVALID", "检索服务必须返回不可变证据元组。", 502)
        for source in group:
            if not isinstance(source, Evidence) or not isinstance(source.graph_path, tuple):
                raise DomainError("RESEARCH_EVIDENCE_INVALID", "检索服务返回了无效证据。", 502)
            if any(not isinstance(part, str) for part in source.graph_path):
                raise DomainError("RESEARCH_EVIDENCE_INVALID", "证据的图路径必须不可变。", 502)
            current_scope = (source.tenant_id, source.snapshot_id)
            if scope is not None and current_scope != scope:
                raise DomainError(
                    "RESEARCH_EVIDENCE_SCOPE_MISMATCH", "研究证据不能跨商店或跨快照混用。", 502
                )
            scope = current_scope
            key = (source.document_id, source.document_version)
            known_hash = document_hashes.setdefault(key, source.source_sha256)
            if known_hash != source.source_sha256:
                raise DomainError("RESEARCH_EVIDENCE_CONFLICT", "同一文档版本对应了不同原件。", 502)
            previous = sources.get(source.source_id)
            if previous is not None and _identity(previous) != _identity(source):
                raise DomainError("RESEARCH_EVIDENCE_CONFLICT", "同一来源编号对应了不同原文。", 502)
            sources.setdefault(source.source_id, source)
    return tuple(sources.values())


def _identity(source: Evidence) -> tuple:
    return (
        source.tenant_id,
        source.snapshot_id,
        source.document_id,
        source.document_version,
        source.title,
        source.text,
        source.source_sha256,
        source.page,
        source.kind,
    )


def validate_draft(draft: ResearchDraft, sources: tuple[Evidence, ...]) -> None:
    by_id = {source.source_id: source for source in sources}
    claim_ids = {claim.claim_id for claim in draft.claims}
    if len(claim_ids) != len(draft.claims):
        raise DomainError("RESEARCH_CITATION_INVALID", "研究结论包含重复编号。", 502)
    for claim in draft.claims:
        source = by_id.get(claim.source_id)
        if (
            source is None
            or not claim.claim.strip()
            or not claim.quote.strip()
            or claim.quote not in source.text
        ):
            raise DomainError(
                "RESEARCH_CITATION_INVALID",
                "结论必须引用本轮来源中的逐字原文。",
                502,
                {"claim_id": claim.claim_id},
            )


def validate_verification(
    verification: ResearchVerification,
    draft: ResearchDraft,
) -> None:
    expected = {claim.claim_id for claim in draft.claims}
    actual = {verdict.claim_id for verdict in verification.verdicts}
    if (
        expected != actual
        or len(actual) != len(verification.verdicts)
        or any(not verdict.reason.strip() for verdict in verification.verdicts)
    ):
        raise DomainError(
            "RESEARCH_VERIFICATION_INVALID", "核验角色必须对每条结论给出一次明确判断。", 502
        )
