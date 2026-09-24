"""FastAPI application: JSON API (/api), operations + management console (/ui), health and metrics."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

from sanctions_agent.api.auth import Principal, current_principal
from sanctions_agent.api.common import DbJSONResponse
from sanctions_agent.api.routes import core, manage
from sanctions_agent.db.engine import fetch_val, tx
from sanctions_agent.logs import configure_logging, log_context
from sanctions_agent.ops.metrics import DbCollector
from sanctions_agent.pipeline.runs import RunAlreadyActive
from sanctions_agent.sources.config_service import ConfigConflict, PermissionDenied, ValidationFailed

WEB = Path(__file__).resolve().parents[1] / "web"

PAGES = {
    "overview": ("Overview", "overview.html"),
    "runs": ("Runs", "runs.html"),
    "quality": ("Data quality", "quality.html"),
    "enrichment": ("Enrichment & evidence", "enrichment.html"),
    "agent": ("Agent activity", "agent.html"),
    "review": ("Review queue", "review.html"),
    "ask": ("Ask", "ask.html"),
    "manage": ("Manage sources", "manage/sources.html"),
}


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title="Sanctions ingestion agent", version="0.1.0", default_response_class=DbJSONResponse)
    templates = Jinja2Templates(directory=str(WEB / "templates"))
    registry = CollectorRegistry()
    registry.register(DbCollector())

    @app.middleware("http")
    async def request_id(request: Request, call_next: Any) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        with log_context(request_id=rid):
            response: Response = await call_next(request)
        response.headers["x-request-id"] = rid
        response.headers["x-content-type-options"] = "nosniff"
        response.headers["referrer-policy"] = "no-referrer"
        if request.url.path.startswith("/ui"):
            response.headers["content-security-policy"] = (
                "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
                "connect-src 'self'; frame-ancestors 'none'"
            )
        return response

    def _err(status: int, e: Exception) -> DbJSONResponse:
        return DbJSONResponse({"error": type(e).__name__, "detail": str(e)}, status_code=status)

    app.add_exception_handler(ConfigConflict, lambda r, e: _err(409, e))  # type: ignore[arg-type]
    app.add_exception_handler(RunAlreadyActive, lambda r, e: _err(409, e))  # type: ignore[arg-type]
    app.add_exception_handler(PermissionDenied, lambda r, e: _err(403, e))  # type: ignore[arg-type]
    app.add_exception_handler(ValidationFailed, lambda r, e: _err(422, e))  # type: ignore[arg-type]
    app.add_exception_handler(KeyError, lambda r, e: _err(404, e))  # type: ignore[arg-type]

    app.include_router(core.router)
    app.include_router(manage.router)
    app.mount("/static", StaticFiles(directory=str(WEB / "static")), name="static")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> Any:
        try:
            with tx() as conn:
                version = fetch_val(conn, "SELECT version_num FROM public.alembic_version")
            return {"status": "ready", "schema": version}
        except psycopg.Error as e:
            raise HTTPException(503, f"database not ready: {e}") from e

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/")
    def root() -> RedirectResponse:
        return RedirectResponse("/ui")

    def page(name: str, request: Request, p: Principal, **ctx: Any) -> HTMLResponse:
        title, template = PAGES[name]
        return templates.TemplateResponse(
            request, template, {"page": name, "title": title, "user": p, "pages": PAGES, **ctx}
        )

    @app.get("/ui", response_class=HTMLResponse)
    def ui_overview(request: Request, p: Principal = Depends(current_principal)) -> HTMLResponse:
        return page("overview", request, p)

    @app.get("/ui/manage/sources/{source_id}", response_class=HTMLResponse)
    def ui_source(
        source_id: str, request: Request, p: Principal = Depends(current_principal)
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "manage/source_edit.html",
            {
                "page": "manage",
                "title": f"Source {source_id}",
                "user": p,
                "pages": PAGES,
                "source_id": source_id,
            },
        )

    @app.get("/ui/manage/new", response_class=HTMLResponse)
    def ui_new_source(request: Request, p: Principal = Depends(current_principal)) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "manage/add_source.html",
            {"page": "manage", "title": "Add source", "user": p, "pages": PAGES},
        )

    @app.get("/ui/manage/settings", response_class=HTMLResponse)
    def ui_settings(request: Request, p: Principal = Depends(current_principal)) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "manage/settings.html",
            {"page": "manage", "title": "Global settings", "user": p, "pages": PAGES},
        )

    @app.get("/ui/runs/{run_id}", response_class=HTMLResponse)
    def ui_run(run_id: str, request: Request, p: Principal = Depends(current_principal)) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "run_detail.html",
            {"page": "runs", "title": "Run detail", "user": p, "pages": PAGES, "run_id": run_id},
        )

    @app.get("/ui/{name}", response_class=HTMLResponse)
    def ui_page(name: str, request: Request, p: Principal = Depends(current_principal)) -> HTMLResponse:
        if name not in PAGES:
            raise HTTPException(404, "page not found")
        return page(name, request, p)

    return app
