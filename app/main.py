from __future__ import annotations

import logging
import re
import time
from uuid import uuid4

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api import router as core_router
from app.api_extended import router as extended_router
from app.config import get_settings
from app.db import SessionLocal
from app.documents.api import router as documents_router
from app.openapi_contract import install_openapi_contract
from app.operations.replay_api import router as operations_replay_router
from app.platform.router import router as platform_router
from app.release import BACKEND_SERVICE_NAME, release_identity

settings = get_settings()
logger = logging.getLogger("freight.api")
_correlation_pattern = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
_SUPERSEDED_CORE_ROUTE_PATHS = {
    "/carriers/{carrier_id}/compliance",
}
_SUPERSEDED_EXTENDED_ROUTE_PATHS = {
    "/admin/users",
    "/admin/roles",
    "/admin/permissions",
    "/admin/capabilities",
    "/admin/capabilities/{code}",
    "/integrations/tracking/{provider}/webhooks",
    "/operations/dead-letters/{message_id}/replay",
    "/documents/upload-sessions",
    "/documents/{document_id}/confirm",
    "/loads/{load_id}/documents",
    "/loads/{load_id}/pod",
}


def _remove_superseded_routes(router: APIRouter, relative_paths: set[str]) -> None:
    """Keep one authoritative owner for every production method/path pair."""

    retained = []
    for route in router.routes:
        path = str(getattr(route, "path", ""))
        relative_path = path.removeprefix("/api/v1")
        if relative_path in relative_paths:
            continue
        retained.append(route)
    router.routes[:] = retained


# Preserve the newer compliance/webhook owners and the command-backed recovery
# owner together. Removing just the identity placeholders revives unsafe handlers.
_remove_superseded_routes(core_router, _SUPERSEDED_CORE_ROUTE_PATHS)
_remove_superseded_routes(extended_router, _SUPERSEDED_EXTENDED_ROUTE_PATHS)

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Multi-tenant freight brokerage / 3PL API",
    docs_url="/docs" if settings.enable_api_docs else None,
    redoc_url="/redoc" if settings.enable_api_docs else None,
)

app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_host_list)
if settings.cors_origin_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "If-Match",
            "X-Correlation-Id",
            "X-Tenant-Id",
        ],
        expose_headers=["ETag", "X-Correlation-Id", "Retry-After"],
    )


@app.middleware("http")
async def request_contract_middleware(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > settings.max_request_body_bytes:
                return JSONResponse(
                    status_code=413,
                    content={
                        "code": "REQUEST_TOO_LARGE",
                        "message": "Request body exceeds the configured limit.",
                    },
                )
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={
                    "code": "INVALID_CONTENT_LENGTH",
                    "message": "Content-Length is invalid.",
                },
            )

    supplied = request.headers.get("X-Correlation-Id", "")
    correlation_id = supplied if _correlation_pattern.fullmatch(supplied) else uuid4().hex
    request.state.correlation_id = correlation_id
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1_000
    response.headers["X-Correlation-Id"] = correlation_id
    response.headers["Server-Timing"] = f"app;dur={elapsed_ms:.2f}"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(self)"
    response.headers["Cache-Control"] = (
        "no-store" if request.url.path.startswith("/api/") else "no-cache"
    )
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    detail = (
        exc.detail
        if isinstance(exc.detail, dict)
        else {"code": "HTTP_ERROR", "message": str(exc.detail)}
    )
    detail.setdefault("correlation_id", getattr(request.state, "correlation_id", None))
    return JSONResponse(status_code=exc.status_code, content=detail, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "code": "VALIDATION_ERROR",
            "message": "Request validation failed.",
            "correlation_id": getattr(request.state, "correlation_id", None),
            "fields": exc.errors(),
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception(
        "unhandled_request_error",
        extra={"correlation_id": getattr(request.state, "correlation_id", None)},
    )
    return JSONResponse(
        status_code=500,
        content={
            "code": "INTERNAL_ERROR",
            "message": "An unexpected error occurred.",
            "correlation_id": getattr(request.state, "correlation_id", None),
        },
    )


@app.get("/health/live", tags=["health"])
async def health_live():
    return {"status": "live"}


@app.get("/health/ready", tags=["health"])
async def health_ready():
    try:
        async with SessionLocal() as db:
            await db.execute(text("SELECT 1"))
        return {"status": "ready", "dependencies": {"database": "ready"}}
    except Exception:
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "dependencies": {"database": "unavailable"},
            },
        )


@app.get("/health/version", tags=["health"])
async def health_version():
    return release_identity(
        service_name=BACKEND_SERVICE_NAME,
        version=settings.app_version,
        migration_head=settings.migration_head,
    )


# Persistent identity and command-backed recovery precede foundation routes.
app.include_router(documents_router)
app.include_router(platform_router)
app.include_router(operations_replay_router)
app.include_router(core_router)
app.include_router(extended_router)
install_openapi_contract(
    app,
    service_name=BACKEND_SERVICE_NAME,
    migration_head=settings.migration_head,
)
