"""Regressions for composing the release, governance and recovery branches."""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from app.api import router as core_router
from app.api_extended import router as extended_router
from app.gateway.middleware import SecurityHeadersMiddleware
from app.operations.replay_api import replay_dead_letter, router as recovery_router
from app.production_v4 import app


def test_all_replacements_survive_the_combined_branch():
    core_paths = {getattr(route, "path", None) for route in core_router.routes}
    extended_paths = {getattr(route, "path", None) for route in extended_router.routes}
    assert "/api/v1/carriers/{carrier_id}/compliance" not in core_paths
    assert "/api/v1/integrations/tracking/{provider}/webhooks" not in extended_paths
    assert "/api/v1/operations/dead-letters/{message_id}/replay" not in extended_paths
    handlers = [route for route in recovery_router.routes
                if getattr(route, "path", None) == "/api/v1/operations/dead-letters/{message_id}/replay"]
    assert len(handlers) == 1
    assert handlers[0].endpoint is replay_dead_letter
    assert "post" in app.openapi()["paths"]["/api/v1/operations/dead-letters/{message_id}/replay"]


@pytest.mark.parametrize("path", [
    "/api/v1/admin/integrations/health",
    "/api/v1/auth/context",
    "/api/v1/operations/control-tower",
])
def test_real_requests_reject_missing_identity_instead_of_disappearing(path):
    with TestClient(app) as client:
        response = client.get(path)
    assert response.status_code == 401, response.text
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "server" not in response.headers


def test_recovery_command_rejects_unauthenticated_requests():
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/operations/dead-letters/{uuid4()}/replay",
            json={"reason": "Synthetic contract request, no provider contacted."},
        )
    assert response.status_code == 401, response.text


@pytest.mark.parametrize("headers", [{}, {"Server": "do-not-expose"}, {"server": "do-not-expose"}])
def test_security_middleware_handles_present_and_absent_server_headers(headers):
    isolated = FastAPI()
    isolated.add_middleware(SecurityHeadersMiddleware)

    @isolated.get("/test")
    async def endpoint():
        return JSONResponse({"ok": True}, headers=headers)

    with TestClient(isolated) as client:
        response = client.get("/test")
    assert response.status_code == 200
    assert "server" not in response.headers
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Cache-Control"] == "no-store"
