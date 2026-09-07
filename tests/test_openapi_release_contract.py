from __future__ import annotations

import inspect

from fastapi.testclient import TestClient

from app.api import router as core_router
from app.api_extended import router as extended_router
from app.compliance.api import router as compliance_router
from app.config import Settings
from app.integrations.api import router as integration_router
from app.integrations.health_api import router as integration_health_router
from app.integrations_main import app as integration_app
from app.platform.router import router as platform_router
from app.portals.admin_api import router as portal_admin_router
from app.portals.carrier_api import router as carrier_portal_router
from app.portals.customer_api import router as customer_portal_router
from app.portals.operations_api import router as portal_operations_router
from app.portals.review_api import router as portal_review_router
from app.production_v4 import app
from app.release import (
    BACKEND_SERVICE_NAME,
    CANONICAL_MIGRATION_HEAD,
    INTEGRATION_SERVICE_NAME,
)


_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}
_LEGACY_IDENTITY_PATHS = {
    "/api/v1/admin/users",
    "/api/v1/admin/roles",
    "/api/v1/admin/permissions",
    "/api/v1/admin/capabilities",
    "/api/v1/admin/capabilities/{code}",
}
_PRODUCTION_ROUTERS = (
    platform_router,
    core_router,
    extended_router,
    integration_health_router,
    integration_router,
    compliance_router,
    portal_admin_router,
    portal_review_router,
    portal_operations_router,
    customer_portal_router,
    carrier_portal_router,
)


def _operations(schema: dict) -> list[tuple[str, str, dict]]:
    result: list[tuple[str, str, dict]] = []
    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if method.lower() in _HTTP_METHODS:
                result.append((path, method.lower(), operation))
    return result


def _method_paths(schema: dict) -> set[tuple[str, str]]:
    return {
        (method.upper(), path)
        for path, method, _operation in _operations(schema)
    }


def test_release_identity_is_consistent_across_entrypoints() -> None:
    backend = TestClient(app).get("/health/version")
    integrations = TestClient(integration_app).get("/health/version")

    assert backend.status_code == 200
    assert integrations.status_code == 200
    assert backend.json()["name"] == BACKEND_SERVICE_NAME
    assert integrations.json()["name"] == INTEGRATION_SERVICE_NAME
    assert backend.json()["migration_head"] == CANONICAL_MIGRATION_HEAD
    assert integrations.json()["migration_head"] == CANONICAL_MIGRATION_HEAD
    assert backend.json()["version"] == app.version
    assert integrations.json()["version"] == integration_app.version
    assert app.version == integration_app.version


def test_stale_migration_identity_is_rejected() -> None:
    try:
        Settings(migration_head="0002_identity_tenancy")
    except ValueError as exc:
        assert "MIGRATION_HEAD" in str(exc)
    else:
        raise AssertionError("stale migration identity was accepted")


def test_legacy_admin_placeholders_are_not_registered() -> None:
    paths = {str(getattr(route, "path", "")) for route in extended_router.routes}
    assert not paths & _LEGACY_IDENTITY_PATHS

    schema = app.openapi()
    assert "list_users" in schema["paths"]["/api/v1/admin/users"]["get"]["operationId"]
    assert "list_roles" in schema["paths"]["/api/v1/admin/roles"]["get"]["operationId"]
    assert "list_permissions" in schema["paths"]["/api/v1/admin/permissions"]["get"]["operationId"]


def test_registered_router_methods_are_unique() -> None:
    owners: dict[tuple[str, str], str] = {}
    duplicates: list[tuple[tuple[str, str], str, str]] = []

    for router in _PRODUCTION_ROUTERS:
        for route in router.routes:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None) or set()
            endpoint = getattr(route, "endpoint", None)
            owner = (
                f"{getattr(endpoint, '__module__', 'unknown')}."
                f"{getattr(endpoint, '__name__', 'unknown')}"
            )
            if not path:
                continue
            for method in methods:
                key = (str(method).upper(), str(path))
                previous = owners.setdefault(key, owner)
                if previous != owner:
                    duplicates.append((key, previous, owner))

    assert not duplicates, duplicates


def test_registered_handlers_do_not_ship_not_implemented_responses() -> None:
    findings: list[str] = []
    forbidden = ("status_code=501", "HTTP_501_NOT_IMPLEMENTED", "NOT_IMPLEMENTED")

    for router in _PRODUCTION_ROUTERS:
        for route in router.routes:
            endpoint = getattr(route, "endpoint", None)
            if endpoint is None:
                continue
            try:
                source = inspect.getsource(endpoint)
            except (OSError, TypeError):
                continue
            if any(marker in source for marker in forbidden):
                findings.append(
                    f"{getattr(endpoint, '__module__', 'unknown')}."
                    f"{getattr(endpoint, '__name__', 'unknown')}"
                )

    assert not findings, findings


def test_openapi_has_unique_operation_ids_and_canonical_metadata() -> None:
    schema = app.openapi()
    operations = _operations(schema)
    operation_ids = [operation["operationId"] for _, _, operation in operations]

    assert operation_ids
    assert len(operation_ids) == len(set(operation_ids))
    assert schema["info"]["version"] == app.version
    assert schema["info"]["x-service-name"] == BACKEND_SERVICE_NAME
    assert schema["info"]["x-migration-head"] == CANONICAL_MIGRATION_HEAD


def test_openapi_documents_standard_bearer_security() -> None:
    schema = app.openapi()
    bearer = schema["components"]["securitySchemes"]["BearerAuth"]
    assert bearer["type"] == "http"
    assert bearer["scheme"] == "bearer"

    protected = schema["paths"]["/api/v1/admin/users"]["get"]
    assert protected["security"] == [{"BearerAuth": []}]
    assert not any(
        parameter.get("in") == "header"
        and str(parameter.get("name", "")).lower() == "authorization"
        for parameter in protected.get("parameters", [])
    )

    signed_webhook = schema["paths"][
        "/api/v1/integrations/{webhook_slug}/webhooks/{provider}"
    ]["post"]
    assert "security" not in signed_webhook


def test_production_openapi_contains_exact_portal_and_integration_contract() -> None:
    operations = _method_paths(app.openapi())
    required = {
        ("GET", "/api/v1/admin/integrations/health"),
        ("GET", "/api/v1/admin/integrations"),
        ("POST", "/api/v1/admin/integrations"),
        ("GET", "/api/v1/admin/compliance/policies"),
        ("POST", "/api/v1/admin/compliance/policies"),
        ("GET", "/api/v1/admin/portal-bindings"),
        ("POST", "/api/v1/admin/portal-bindings"),
        ("GET", "/api/v1/admin/portal-reviews/claims"),
        ("PATCH", "/api/v1/admin/portal-reviews/claims/{submission_id}"),
        ("GET", "/api/v1/admin/portal-reviews/carrier-evidence"),
        ("PATCH", "/api/v1/admin/portal-reviews/carrier-evidence/{submission_id}"),
        ("GET", "/api/v1/operations/control-tower"),
        ("GET", "/api/v1/portals/customer/context"),
        ("GET", "/api/v1/portals/customer/documents"),
        ("POST", "/api/v1/portals/customer/quotes/{quote_id}/decision"),
        ("GET", "/api/v1/portals/carrier/context"),
        ("GET", "/api/v1/portals/carrier/documents"),
        ("POST", "/api/v1/portals/carrier/tenders/{tender_id}/response"),
        ("POST", "/api/v1/portals/carrier/loads/{load_id}/tracking"),
        ("POST", "/api/v1/integrations/{webhook_slug}/webhooks/{provider}"),
    }
    forbidden = {
        ("POST", "/api/v1/admin/portal-reviews/claims/{submission_id}/decision"),
        ("POST", "/api/v1/admin/portal-reviews/carrier-evidence/{submission_id}/decision"),
    }

    assert required <= operations
    assert operations.isdisjoint(forbidden)
