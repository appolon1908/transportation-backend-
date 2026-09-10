"""Backend-only Orbit contracts. This module grants no access or activation."""
from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.release import BACKEND_SERVICE_NAME, CANONICAL_MIGRATION_HEAD
from app.repository_identity import REPOSITORY, REPOSITORY_ID

CONTRACT_PATH = "/api/v1/platform/contract"
CONTRACT_VERSION = "1.0.0"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Requirements(ContractModel):
    renderedInterface: Literal[False]
    sharedHeaderFooter: Literal[False]
    loginLogoutPages: Literal[False]
    browserSessionContract: Literal[True]
    serviceAuthorization: Literal[True]
    browserTokenResponseProhibited: Literal[True]
    tenantCapabilities: Literal[True]
    idempotency: Literal[True]
    correlationId: Literal[True]
    audit: Literal[True]
    rollback: Literal[True]


class AdoptionManifest(ContractModel):
    schemaVersion: Literal["2.0.0"]
    repository: Literal["appolon1908-hue/freight-platform-backend"]
    classification: Literal["backend-api"]
    targetBranch: Literal["development"]
    adoptionMode: Literal["contract-only"]
    domain: None
    domainStatus: Literal["not-applicable"]
    requirements: Requirements
    status: Literal["contract-implemented-deployment-gated"]
    apiContractVersion: Literal["1.0.0"]
    contractPath: Literal["/api/v1/platform/contract"]
    browserSessionOwner: Literal["portal-bff"]
    browserSessionsImplemented: Literal[False]
    deploymentAuthorized: Literal[False]
    externalEffectsAuthorized: Literal[False]
    blockers: list[str] = Field(min_length=1)


class EndpointContract(ContractModel):
    name: str
    method: Literal["GET"] = "GET"
    path: str


ENDPOINTS = (
    EndpointContract(name="backend_contract", path=CONTRACT_PATH),
    EndpointContract(name="identity_context", path="/api/v1/auth/context"),
    EndpointContract(name="permissions", path="/api/v1/me/permissions"),
    EndpointContract(name="tenant_capabilities", path="/api/v1/capabilities"),
    EndpointContract(name="integration_health", path="/api/v1/admin/integrations/health"),
    EndpointContract(name="operations", path="/api/v1/operations/control-tower"),
    EndpointContract(name="customer_context", path="/api/v1/portals/customer/context"),
    EndpointContract(name="carrier_context", path="/api/v1/portals/carrier/context"),
)


class AuthenticationContract(ContractModel):
    scheme: Literal["BearerAuth"] = "BearerAuth"
    issuer: str
    tenant_header: Literal["X-Tenant-Id"] = "X-Tenant-Id"
    identity_context_path: Literal["/api/v1/auth/context"] = "/api/v1/auth/context"
    browser_session_owner: Literal["portal-bff"] = "portal-bff"
    browser_tokens_returned: Literal[False] = False
    session_endpoints_implemented_here: Literal[False] = False


class RequestContract(ContractModel):
    correlation_header: Literal["X-Correlation-Id"] = "X-Correlation-Id"
    command_idempotency_header: Literal["Idempotency-Key"] = "Idempotency-Key"
    permissions_path: Literal["/api/v1/me/permissions"] = "/api/v1/me/permissions"
    capabilities_path: Literal["/api/v1/capabilities"] = "/api/v1/capabilities"
    authorization: Literal["membership-permission-capability-and-resource-scope"] = (
        "membership-permission-capability-and-resource-scope"
    )


class SafetyContract(ContractModel):
    descriptor_grants_access: Literal[False] = False
    activation_authorized_by_contract: Literal[False] = False
    production_certification: Literal["not-asserted"] = "not-asserted"


class BackendContract(ContractModel):
    contract_version: Literal["1.0.0"] = "1.0.0"
    service_name: Literal["freight-platform-backend"] = BACKEND_SERVICE_NAME
    repository: str = REPOSITORY
    repository_id: str = REPOSITORY_ID
    application_version: str
    migration_head: str = CANONICAL_MIGRATION_HEAD
    tenant_id: UUID
    authentication: AuthenticationContract
    request: RequestContract = Field(default_factory=RequestContract)
    endpoints: tuple[EndpointContract, ...] = ENDPOINTS
    safety: SafetyContract = Field(default_factory=SafetyContract)


def validate_openapi_contract(schema: dict[str, Any]) -> None:
    """Reject absent or unauthenticated routes, rather than promising fake APIs."""
    security = schema.get("components", {}).get("securitySchemes", {}).get("BearerAuth", {})
    if security.get("type") != "http" or security.get("scheme") != "bearer":
        raise ValueError("orbit_contract_requires_oidc_bearer_scheme")
    if schema.get("info", {}).get("x-migration-head") != CANONICAL_MIGRATION_HEAD:
        raise ValueError("orbit_contract_migration_head_mismatch")
    for endpoint in ENDPOINTS:
        operation = schema.get("paths", {}).get(endpoint.path, {}).get(endpoint.method.lower())
        if not operation:
            raise ValueError(f"orbit_contract_missing_endpoint:{endpoint.name}")
        if operation.get("security") != [{"BearerAuth": []}]:
            raise ValueError(f"orbit_contract_unauthenticated_endpoint:{endpoint.name}")
