from __future__ import annotations

from pathlib import Path

import pytest

from app.compliance.identifiers import hash_identifier, validate_evidence_hash
from app.production_v4 import app

ROOT = Path(__file__).resolve().parents[1]


def test_identifier_hash_is_normalized_and_peppered(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("COMPLIANCE_IDENTIFIER_PEPPER", "pepper-one")
    first = hash_identifier(" MC 12 34 ")
    second = hash_identifier("mc1234")
    assert first == second
    assert len(first) == 64

    monkeypatch.setenv("COMPLIANCE_IDENTIFIER_PEPPER", "pepper-two")
    assert hash_identifier("mc1234") != first


def test_production_identifier_hash_requires_pepper(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("COMPLIANCE_IDENTIFIER_PEPPER", raising=False)
    with pytest.raises(RuntimeError, match="required in production"):
        hash_identifier("MC1234")


def test_evidence_hash_accepts_only_sha256() -> None:
    digest = "ab" * 32
    assert validate_evidence_hash(f"sha256:{digest}") == digest
    with pytest.raises(ValueError):
        validate_evidence_hash("not-a-sha256")


def test_hardened_openapi_contains_compliance_and_integration_surfaces() -> None:
    paths = app.openapi()["paths"]
    assert "/api/v1/admin/compliance/policies" in paths
    assert "/api/v1/carriers/{carrier_id}/readiness/evaluate" in paths
    assert "/api/v1/admin/integrations/health" in paths
    assert "/api/v1/integrations/{webhook_slug}/webhooks/{provider}" in paths


def test_compliance_migration_enforces_database_boundary() -> None:
    migration = (
        ROOT / "compliance_migrations/versions/0001_carrier_readiness.py"
    ).read_text()
    assert "freight_carrier_readiness" in migration
    assert "enforce_freight_carrier_readiness" in migration
    assert "trg_tenders_carrier_readiness" in migration
    assert "trg_assignments_carrier_readiness" in migration
    assert "trg_loads_dispatch_readiness" in migration
    assert "OUT_OF_SERVICE" in migration
    assert "UNSATISFACTORY_SAFETY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "TO freight_api" in migration
    assert "TENANT_CONTEXT_MISMATCH" in migration
    assert "0005_portal_workflows" in migration
    assert "0001_integrations_durability" not in migration
    assert "ADD CONSTRAINT IF NOT EXISTS" not in migration


def test_kong_template_removes_spoofable_context_and_uses_redis_limits() -> None:
    template = (ROOT / "deploy/gateway/kong.yml.template").read_text()
    for header in (
        "X-Freight-Gateway-Proof",
        "X-Tenant-ID",
        "X-Organization-ID",
        "X-Actor-ID",
        "X-Permissions",
    ):
        assert header in template
    assert "policy: redis" in template
    assert "fault_tolerant: false" in template
    assert "request-size-limiting" in template
    assert "correlation-id" in template
    assert "${GATEWAY_SHARED_SECRET}" in template


def test_caddy_is_the_only_public_gateway_and_limits_bodies() -> None:
    caddy = (ROOT / "deploy/gateway/Caddyfile").read_text()
    compose = (ROOT / "deploy/gateway/compose.gateway.yaml").read_text()
    assert "max_size 1MB" in caddy
    assert "max_size 2MB" in caddy
    assert "Strict-Transport-Security" in caddy
    assert "reverse_proxy kong:8000" in caddy
    assert "published: 443" in compose
    assert "KONG_ADMIN_LISTEN: \"off\"" in compose
    assert "gateway_internal:\n    internal: true" in compose
    redis_block = compose.split("  redis:", 1)[1].split("  kong:", 1)[0]
    assert "redis:" in compose and "ports:" not in redis_block


def test_backend_entrypoint_migrates_one_canonical_chain_before_compliance() -> None:
    entrypoint = (ROOT / "deploy/backend/entrypoint-v4.sh").read_text()
    core = entrypoint.index("alembic upgrade head")
    compliance = entrypoint.index("alembic -c alembic-compliance.ini upgrade head")
    assert core < compliance
    assert "alembic-integrations.ini" not in entrypoint
    assert "app.production_v4:app" in entrypoint
    assert "workers.integration_worker" in entrypoint


def test_release_composes_canonical_private_api_identity() -> None:
    compose = (ROOT / "deploy/backend/compose.v4.yaml").read_text()
    assert "freight-platform-backend" in compose
    assert "freight-api:" in compose
    assert "FREIGHT_BACKEND_IMAGE" in compose
    assert "GATEWAY_SHARED_SECRET_FILE" in compose
    assert "COMPLIANCE_IDENTIFIER_PEPPER_FILE" in compose
    assert "ports:" not in compose
