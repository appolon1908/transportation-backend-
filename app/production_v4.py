"""Compliance- and gateway-hardened production ASGI entrypoint."""

import json
import re

from fastapi import Request
from sqlalchemy.exc import IntegrityError
from starlette.responses import JSONResponse

from app.compliance.api import router as compliance_router
from app.config import get_settings
from app.gateway.middleware import SecurityHeadersMiddleware, TrustedGatewayMiddleware
from app.production import app as app

settings = get_settings()
if not getattr(app.state, "freight_compliance_router_registered", False):
    app.include_router(compliance_router)
    app.state.freight_compliance_router_registered = True
    app.openapi_schema = None

middleware_names = {item.cls.__name__ for item in app.user_middleware}
if "SecurityHeadersMiddleware" not in middleware_names:
    app.add_middleware(SecurityHeadersMiddleware)
if "TrustedGatewayMiddleware" not in middleware_names:
    app.add_middleware(TrustedGatewayMiddleware)


async def _integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    detail = str(getattr(exc, "orig", exc))
    if "CARRIER_NOT_READY" in detail:
        reasons: list[str] = []
        match = re.search(r"DETAIL:\s*(\[[^\n]+\])", detail)
        if match:
            try:
                parsed = json.loads(match.group(1))
                if isinstance(parsed, list):
                    reasons = [str(item) for item in parsed]
            except json.JSONDecodeError:
                pass
        return JSONResponse(
            status_code=409,
            content={
                "detail": {
                    "code": "CARRIER_NOT_READY",
                    "message": "Carrier does not satisfy the current compliance policy.",
                    "reasons": reasons,
                    "correlation_id": getattr(request.state, "correlation_id", None),
                }
            },
        )
    return JSONResponse(
        status_code=409,
        content={
            "detail": {
                "code": "DATA_INTEGRITY_CONFLICT",
                "message": "The requested write conflicts with a data integrity rule.",
                "correlation_id": getattr(request.state, "correlation_id", None),
            }
        },
    )


app.add_exception_handler(IntegrityError, _integrity_error_handler)
app.title = settings.app_name
app.version = settings.app_version
