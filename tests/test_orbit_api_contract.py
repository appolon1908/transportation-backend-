from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from fastapi.testclient import TestClient

from app.platform.orbit_contract import CONTRACT_PATH, validate_openapi_contract
from app.production_v4 import app
from app.security import Actor, get_actor


def test_composed_api_matches_all_orbit_declared_endpoints():
    validate_openapi_contract(app.openapi())


def test_contract_is_not_an_anonymous_configuration_endpoint():
    with TestClient(app) as client:
        response = client.get(CONTRACT_PATH)
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"


def test_contract_uses_authorized_actor_scope_and_never_issues_tokens():
    actor = Actor(
        subject="synthetic-subject", issuer="https://identity.example.test", principal_id=uuid4(),
        membership_id=uuid4(), tenant_id=uuid4(), organization_id=None, customer_id=None,
        carrier_id=None, principal_type="USER", permissions=frozenset(), roles=frozenset(),
    )
    previous = app.dependency_overrides.copy()
    try:
        app.dependency_overrides[get_actor] = lambda: actor
        with TestClient(app) as client:
            first = client.get(CONTRACT_PATH, headers={"X-Tenant-Id": str(uuid4())})
            actor = replace(actor, tenant_id=uuid4())
            second = client.get(CONTRACT_PATH)
        assert first.status_code == second.status_code == 200
        assert first.json()["tenant_id"] != second.json()["tenant_id"]
        assert second.json()["tenant_id"] == str(actor.tenant_id)
        for response in (first, second):
            body = response.json()
            assert body["safety"]["descriptor_grants_access"] is False
            assert body["safety"]["activation_authorized_by_contract"] is False
            assert body["authentication"]["session_endpoints_implemented_here"] is False
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["X-Correlation-Id"]
            assert "set-cookie" not in response.headers
            assert not any(token in response.text for token in ("access_token", "refresh_token", "id_token", "client_secret"))
            assert "synthetic-subject" not in response.text
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
