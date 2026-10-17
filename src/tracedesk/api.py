"""HTTP boundary for the same services used by the CLI and workflow evaluation."""

import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from . import __version__
from .bootstrap import create_services, resource
from .errors import DomainError
from .knowledge_api import install_knowledge_routes
from .provider import ModelProvider
from .schemas import ConfirmAction, NewSession, NewTurn
from .settings import Settings


def create_app(
    settings: Settings | None = None,
    *,
    provider: ModelProvider | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    configured = settings or Settings.from_env()
    services = create_services(configured, provider=provider, clock=clock)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            await services.close()

    app = FastAPI(
        title="TraceDesk API", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None
    )
    app.state.services = services

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse({"error": exc.as_dict()}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [".".join(str(part) for part in item["loc"]) for item in exc.errors()]
        return JSONResponse(
            {
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "请求参数不符合接口契约。",
                    "details": {"fields": fields},
                }
            },
            status_code=422,
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": "HTTP_ERROR", "message": str(exc.detail)}},
            status_code=exc.status_code,
        )

    @app.middleware("http")
    async def response_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def authenticated(
        request: Request, authorization: Annotated[str | None, Header()] = None
    ) -> dict[str, Any]:
        if not authorization or not authorization.startswith("Bearer "):
            raise DomainError("UNAUTHENTICATED", "请先创建会话。", 401)
        return services.store.authenticate(authorization[7:], request.path_params.get("session_id"))

    @app.get("/api/info")
    def info() -> dict[str, Any]:
        return {
            "name": "TraceDesk",
            "version": __version__,
            "runtime": configured.provider,
            "model": configured.model if configured.provider == "llm" else None,
            "demo": True,
            "retrieval_method": configured.retrieval_method,
            "business_date": services.store.now().date().isoformat(),
            "knowledge": True,
        }

    @app.get("/api/demo/customers")
    def customers() -> dict[str, Any]:
        return {"customers": services.store.customer_list()}

    @app.post("/api/sessions", status_code=201)
    def new_session(body: NewSession) -> dict[str, Any]:
        return services.store.create_session(body.customer_id)

    @app.get("/api/sessions/{session_id}")
    def get_session(
        session_id: str, session: Annotated[dict[str, Any], Depends(authenticated)]
    ) -> dict[str, Any]:
        turns = services.store.list_turns(session)
        refreshed = [present_turn(session, turn) for turn in turns]
        return {
            "id": session_id,
            "customer": {"id": session["customer_id"], "name": session["customer_name"]},
            "turns": refreshed,
        }

    def present_turn(session: dict[str, Any], turn: dict[str, Any]) -> dict[str, Any]:
        action = turn.get("action")
        if not action:
            return turn
        current = services.refunds.current_action(session, action["id"])
        # Preserve the recorded turn and overlay current action state consistently at the HTTP boundary.
        current_action = {**action, **current}
        if current_action["state"] != "pending":
            current_action.pop("confirmation_token", None)
        return {**turn, "action": current_action}

    @app.post("/api/sessions/{session_id}/turns")
    async def new_turn(
        session_id: str, body: NewTurn, session: Annotated[dict[str, Any], Depends(authenticated)]
    ) -> dict[str, Any]:
        turn = await services.agent.ask(session, body.message, str(body.request_id))
        return present_turn(session, turn)

    @app.get("/api/runs/{run_id}")
    def get_run(
        run_id: str, session: Annotated[dict[str, Any], Depends(authenticated)]
    ) -> dict[str, Any]:
        return services.store.run_details(session, run_id)

    @app.post("/api/actions/{action_id}/confirm")
    def confirm(
        action_id: str,
        body: ConfirmAction,
        session: Annotated[dict[str, Any], Depends(authenticated)],
    ) -> dict[str, Any]:
        return services.refunds.confirm(
            session, action_id, body.confirmation_token, str(body.request_id)
        )

    @app.post("/api/actions/{action_id}/cancel")
    def cancel(
        action_id: str, session: Annotated[dict[str, Any], Depends(authenticated)]
    ) -> dict[str, Any]:
        return services.refunds.cancel(session, action_id)

    @app.get("/api/evaluation")
    def evaluation() -> dict[str, Any]:
        report = resource("evaluation.json")
        if not report.is_file():
            raise DomainError(
                "REPORT_NOT_GENERATED", "尚未生成评测报告，请运行 tracedesk evaluate。", 404
            )
        return json.loads(report.read_text(encoding="utf-8"))

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__}

    install_knowledge_routes(app, services, authenticated)

    web_root = Path(str(files("tracedesk").joinpath("web")))
    if (web_root / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=web_root / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        index_path = web_root / "index.html"
        if not index_path.is_file():
            raise DomainError(
                "UI_NOT_BUILT", "前端尚未构建。请在 web 目录运行 npm ci 和 npm run build。", 503
            )
        return FileResponse(index_path)

    return app
