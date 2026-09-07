from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.platform.orbit_contract import (
    AdoptionManifest, CONTRACT_PATH, ENDPOINTS, validate_openapi_contract,
)
from app.release import CANONICAL_MIGRATION_HEAD

ROOT = Path(__file__).resolve().parents[1]


def manifest():
    return json.loads((ROOT / "orbit/adoption-manifest.json").read_text())


def schema():
    return {
        "info": {"x-migration-head": CANONICAL_MIGRATION_HEAD},
        "components": {"securitySchemes": {"BearerAuth": {"type": "http", "scheme": "bearer"}}},
        "paths": {e.path: {"get": {"security": [{"BearerAuth": []}]}} for e in ENDPOINTS},
    }


def test_manifest_declares_backend_contract_not_browser_or_deployment_completion():
    result = AdoptionManifest.model_validate(manifest())
    assert result.contractPath == CONTRACT_PATH
    assert result.blockers
    assert result.browserSessionsImplemented is False
    assert result.deploymentAuthorized is False
    assert result.externalEffectsAuthorized is False


@pytest.mark.parametrize("field,value", [
    ("browserSessionsImplemented", True), ("deploymentAuthorized", True),
    ("externalEffectsAuthorized", True), ("browserSessionOwner", "browser-local-storage"),
    ("contractPath", "/api/v1/fake-login"), ("blockers", []),
    ("status", "production-ready"), ("unexpectedCredential", "do-not-store"),
])
def test_manifest_rejects_unsafe_or_unimplemented_claims(field, value):
    payload = manifest()
    payload[field] = value
    with pytest.raises(ValidationError):
        AdoptionManifest.model_validate(payload)


@pytest.mark.parametrize("field", ["serviceAuthorization", "browserTokenResponseProhibited", "tenantCapabilities"])
def test_manifest_cannot_disable_backend_authorization(field):
    payload = manifest()
    payload["requirements"][field] = False
    with pytest.raises(ValidationError):
        AdoptionManifest.model_validate(payload)


def test_endpoint_names_and_paths_are_unique():
    assert len({e.name for e in ENDPOINTS}) == len(ENDPOINTS)
    assert len({(e.method, e.path) for e in ENDPOINTS}) == len(ENDPOINTS)
    validate_openapi_contract(schema())


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_contract_rejects_missing_or_public_endpoint(endpoint):
    source = schema()
    del source["paths"][endpoint.path]
    with pytest.raises(ValueError, match="missing_endpoint"):
        validate_openapi_contract(source)
    source = schema()
    source["paths"][endpoint.path]["get"]["security"] = []
    with pytest.raises(ValueError, match="unauthenticated_endpoint"):
        validate_openapi_contract(source)


def test_contract_rejects_stale_schema_and_invalid_security_scheme():
    for path, value in (("migration", "old-head"), ("security", {"type": "apiKey"})):
        source = deepcopy(schema())
        if path == "migration":
            source["info"]["x-migration-head"] = value
        else:
            source["components"]["securitySchemes"]["BearerAuth"] = value
        with pytest.raises(ValueError):
            validate_openapi_contract(source)
