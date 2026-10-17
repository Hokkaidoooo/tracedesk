"""Document and research routes share the application's existing authentication."""

from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import Response
from pydantic import Field

from .errors import DomainError
from .evidence import GraphFact
from .knowledge_workflows import ResearchRequest
from .schemas import NewTurn, StrictInput


class RelationInput(StrictInput):
    subject: str = Field(min_length=1, max_length=200)
    predicate: str = Field(min_length=1, max_length=100)
    object: str = Field(min_length=1, max_length=200)
    source_ids: list[str] = Field(min_length=1, max_length=20)


class BuildKnowledge(StrictInput):
    with_embeddings: bool = False
    with_graph: bool = False
    relations: list[RelationInput] | None = Field(default=None, max_length=5000)


class LoadKnowledgeDemo(StrictInput):
    with_embeddings: bool = False
    with_graph: bool = True


class KnowledgeSearch(StrictInput):
    query: str = Field(min_length=1, max_length=2000)
    method: Literal[
        "lexical",
        "semantic",
        "hybrid",
        "graph",
        "text2cypher",
        "graphrag_local",
        "graphrag_global",
    ] = "lexical"
    snapshot_id: str | None = Field(default=None, min_length=1, max_length=100)
    limit: int = Field(default=5, ge=1, le=20)


class ResearchTurn(NewTurn):
    snapshot_id: str = Field(min_length=1, max_length=100)
    mode: Literal["single", "multi"] | None = None


def install_knowledge_routes(app: FastAPI, services: Any, authenticated: Any) -> None:
    session_dependency = Depends(authenticated)

    @app.get("/api/knowledge")
    def knowledge(session: Annotated[dict, session_dependency]) -> dict:
        return services.workflows.catalog(session)

    @app.post("/api/knowledge/demo", status_code=201)
    async def demo(body: LoadKnowledgeDemo, session: Annotated[dict, session_dependency]) -> dict:
        return await services.workflows.load_demo(
            session, with_embeddings=body.with_embeddings, with_graph=body.with_graph
        )

    @app.post("/api/knowledge/documents", status_code=201)
    async def upload(
        request: Request,
        session: Annotated[dict, session_dependency],
        filename: Annotated[str, Query(min_length=1, max_length=255)],
        title: Annotated[str, Query(min_length=1, max_length=255)],
    ) -> dict:
        maximum = services.documents.config.max_upload_bytes
        content = bytearray()
        async for part in request.stream():
            content.extend(part)
            if len(content) > maximum:
                raise DomainError("UPLOAD_TOO_LARGE", "文件超过配置的上传大小限制。", 413)
        return await services.documents.ingest(
            session,
            filename=filename,
            title=title,
            content=bytes(content),
            media_type=request.headers.get("content-type", "application/octet-stream").split(";")[
                0
            ],
        )

    @app.post("/api/knowledge/documents/{document_id}/versions/{version}/retry")
    async def retry(
        document_id: str, version: int, session: Annotated[dict, session_dependency]
    ) -> dict:
        return await services.documents.retry_document(session, document_id, version)

    @app.get("/api/knowledge/documents/{document_id}/versions/{version}/source")
    def original(
        document_id: str, version: int, session: Annotated[dict, session_dependency]
    ) -> Response:
        result = services.documents.original(session, document_id, version)
        return Response(
            result["content"],
            media_type=result["media_type"],
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(result['filename'], safe='')}",
                "X-Source-SHA256": result["sha256"],
            },
        )

    @app.post("/api/knowledge/snapshots", status_code=201)
    async def build(body: BuildKnowledge, session: Annotated[dict, session_dependency]) -> dict:
        try:
            facts = (
                tuple(
                    GraphFact(item.subject, item.predicate, item.object, tuple(item.source_ids))
                    for item in body.relations
                )
                if body.relations is not None
                else None
            )
        except ValueError as exc:
            raise DomainError("INVALID_RELATIONS", "关系必须包含不同的有效来源引用。", 422) from exc
        return await services.workflows.build(
            session, with_embeddings=body.with_embeddings, with_graph=body.with_graph, facts=facts
        )

    @app.get("/api/knowledge/snapshots/{snapshot_id}")
    def snapshot(snapshot_id: str, session: Annotated[dict, session_dependency]) -> dict:
        result = services.workflows.snapshot(session, snapshot_id)
        return {**result, "methods": list(services.workflows.methods(session, result))}

    @app.get("/api/knowledge/snapshots/{snapshot_id}/evidence/{source_id}")
    def evidence(
        snapshot_id: str, source_id: str, session: Annotated[dict, session_dependency]
    ) -> dict:
        result = next(
            (
                item
                for item in services.documents.evidence(session, snapshot_id)
                if item.source_id == source_id
            ),
            None,
        )
        if result is None:
            raise DomainError("SOURCE_NOT_FOUND", "该知识版本没有这条来源。", 404)
        return result.as_dict()

    @app.post("/api/knowledge/search")
    async def search(body: KnowledgeSearch, session: Annotated[dict, session_dependency]) -> dict:
        snapshot = services.workflows.snapshot(session, body.snapshot_id)
        found = await services.workflows.search(
            session, body.query, method=body.method, snapshot_id=snapshot["id"], limit=body.limit
        )
        return {
            "snapshot_id": snapshot["id"],
            "method": body.method,
            "evidence": [item.as_dict() for item in found],
        }

    @app.post("/api/sessions/{session_id}/research")
    async def research(
        session_id: str, body: ResearchTurn, session: Annotated[dict, session_dependency]
    ) -> dict:
        return await services.agent.ask(
            session,
            body.message,
            str(body.request_id),
            research=ResearchRequest(
                snapshot_id=body.snapshot_id,
                mode=services.settings.research.mode if body.mode is None else body.mode,
            ),
        )
