"""FastAPI application factory, routers, error handlers and lifecycle hooks.

Run with:  uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.api import (
    routes_agents,
    routes_approvals,
    routes_ask,
    routes_dashboard,
    routes_dev,
    routes_engine,
    routes_health,
    routes_plans,
    routes_providers,
    routes_tools,
    routes_workflows,
)
from app.config import PROJECT_ROOT, get_settings
from app.core.errors import AppError
from app.core.logging import request_id_var, setup_logging
from app.db.session import SessionLocal, engine
from app.engine import Engine
from app.llm import get_llm_client
from app.tools.startup import log_tools_config

logger = logging.getLogger("app")


def error_response(
    status_code: int, code: str, message: str, details: Any | None = None
) -> JSONResponse:
    """Build the uniform error envelope: {"error": {"code", "message"[, "details"]}}."""
    body: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    return JSONResponse(status_code=status_code, content=jsonable_encoder({"error": body}))


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error("request.app_error code=%s message=%s", exc.code, exc.message)
        return error_response(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"loc": list(err.get("loc", ())), "msg": err.get("msg"), "type": err.get("type")}
            for err in exc.errors()
        ]
        summary = "; ".join(
            f"{'.'.join(str(p) for p in d['loc'])}: {d['msg']}" for d in details
        )
        # Literal 422: the Starlette constant name differs across versions.
        return error_response(422, "validation_error", summary, details)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "not_found" if exc.status_code == 404 else f"http_{exc.status_code}"
        return error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("request.unhandled_error error=%s", type(exc).__name__)
        return error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error", "Internal server error"
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logger.info(
        "app.startup env=%s version=%s gemini_configured=%s engine_enabled=%s",
        settings.app_env,
        __version__,
        settings.gemini_api_key is not None,  # never log the key itself
        settings.engine_enabled,
    )
    if settings.llm_provider == "fake":
        logger.warning("=" * 72)
        logger.warning("LLM_PROVIDER=fake: NO REAL MODEL CALLS. Agents return canned demo output "
                       "after %.1fs.", settings.fake_llm_delay_seconds)
        logger.warning("=" * 72)
    log_tools_config()  # Part 9: a bad tools.yaml stops startup; disabled tools are logged once
    app.state.engine = None
    if settings.engine_enabled:
        llm = get_llm_client()
        if hasattr(llm, "ensure_started"):  # validate models.yaml + build providers before serving
            await llm.ensure_started()
        app.state.engine = Engine(SessionLocal, llm, settings)
        await app.state.engine.start()
    yield
    if app.state.engine is not None:
        await app.state.engine.stop()  # graceful: drain, then cancel (tasks stay RUNNING)
    await engine.dispose()
    logger.info("app.shutdown")


_SAFE_RID = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")


async def request_id_middleware(request: Request, call_next):  # noqa: ANN001, ANN201
    """X-Request-ID: keep a safe incoming id, else generate one; every log line of this request carries it."""
    incoming = request.headers.get("x-request-id", "")
    rid = incoming if _SAFE_RID.match(incoming) else uuid.uuid4().hex[:16]
    token = request_id_var.set(rid)
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    response.headers["X-Request-ID"] = rid
    return response


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level)

    app = FastAPI(
        title="AgentFlow",
        version=__version__,
        description="Multi-agent orchestration engine.",
        lifespan=lifespan,
    )
    register_exception_handlers(app)
    app.middleware("http")(request_id_middleware)
    app.include_router(routes_health.router)
    app.include_router(routes_workflows.router)
    app.include_router(routes_agents.router)
    app.include_router(routes_plans.router)
    app.include_router(routes_engine.router)
    app.include_router(routes_ask.router)
    app.include_router(routes_providers.router)
    app.include_router(routes_tools.router)
    app.include_router(routes_approvals.router)
    app.include_router(routes_dashboard.router)
    app.include_router(routes_dev.router)
    # Dashboard assets (self-hosted fonts); the page itself is GET /dashboard.
    app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "app" / "static"), name="static")
    return app


app = create_app()
