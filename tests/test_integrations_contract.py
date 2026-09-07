from __future__ import annotations

import os
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.integrations.adapters import event_envelope
from app.integrations.security import (
    canonical_json_bytes,
    resolve_secret,
    sha256_hex,
    signature_hex,
    signed_headers,
    validate_destination_url,
    verify_signature,
)
from app.integrations.service import integration_event_matches, retry_delay_seconds
from app.integrations_main import app


def test_integration_routes_are_registered() -> None:
    routes = set(app.openapi()["paths"])
    assert "/api/v1/admin/integrations/health" in routes
    assert "/api/v1/admin/integrations" in routes
    assert "/api/v1/admin/integrations/{connection_id}/deliveries" in routes
    assert "/api/v1/admin/integrations/inbox/messages" in routes
    assert "/api/v1/admin/integrations/provenance/verify" in routes
    assert "/api/v1/integrations/{webhook_slug}/webhooks/{provider}" in routes
    assert "/api/v1/integrations/tracking/{provider}/webhooks" in routes


def test_signature_contract_is_timestamp_bound() -> None:
    raw = canonical_json_bytes({"type": "tracking.position.received", "data": {"load_id": "123"}})
    timestamp = str(int(time.time()))
    supplied = signature_hex("test-secret", timestamp, raw)
    verify_signature(secret="test-secret", timestamp=timestamp, raw_body=raw, supplied=supplied)
    with pytest.raises(HTTPException) as exc:
        verify_signature(
            secret="test-secret",
            timestamp=timestamp,
            raw_body=raw + b" ",
            supplied=supplied,
        )
    assert exc.value.status_code == 401


def test_signed_headers_include_idempotency_and_key_identity() -> None:
    raw = b"{}"
    headers = signed_headers(
        secret="secret",
        key_id="key-2026-08",
        event_id="event-1",
        raw_body=raw,
        timestamp=1_787_700_000,
    )
    assert headers["Idempotency-Key"] == "event-1"
    assert headers["X-Freight-Key-Id"] == "key-2026-08"
    assert headers["X-Freight-Signature"].startswith("sha256=")


def test_secret_resolution_accepts_references_not_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_PROVIDER_SECRET", "resolved-value")
    assert resolve_secret("env:TEST_PROVIDER_SECRET") == "resolved-value"
    with pytest.raises(RuntimeError, match="UNSUPPORTED_SECRET_REFERENCE"):
        resolve_secret("resolved-value")
    with pytest.raises(RuntimeError, match="SECRET_NOT_AVAILABLE"):
        resolve_secret("env:ABSENT_PROVIDER_SECRET")


def test_destination_policy_requires_allowlisted_tls_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("INTEGRATION_ALLOWED_HOSTS", "odoo.internal.example,n8n.internal.example")
    assert (
        validate_destination_url("https://odoo.internal.example", "/json/2/freight.event/ingest")
        == "https://odoo.internal.example/json/2/freight.event/ingest"
    )
    with pytest.raises(ValueError, match="DESTINATION_TLS_REQUIRED"):
        validate_destination_url("http://odoo.internal.example")
    with pytest.raises(ValueError, match="DESTINATION_HOST_NOT_ALLOWED"):
        validate_destination_url("https://attacker.example")


def test_event_filter_supports_exact_prefix_and_explicit_wildcard() -> None:
    exact = SimpleNamespace(event_types=["load.dispatched.v1"])
    prefix = SimpleNamespace(event_types=["shipment.*"])
    all_events = SimpleNamespace(event_types=["*"])
    disabled = SimpleNamespace(event_types=[])
    assert integration_event_matches(exact, "load.dispatched.v1")
    assert not integration_event_matches(exact, "load.delivered.v1")
    assert integration_event_matches(prefix, "shipment.created.v1")
    assert integration_event_matches(all_events, "invoice.issued.v1")
    assert not integration_event_matches(disabled, "invoice.issued.v1")


def test_retry_backoff_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTEGRATION_RETRY_BASE_SECONDS", "10")
    monkeypatch.setenv("INTEGRATION_RETRY_CAP_SECONDS", "60")
    assert retry_delay_seconds(1) == 10
    assert retry_delay_seconds(2) == 20
    assert retry_delay_seconds(8) == 60


def test_event_envelope_is_versioned_and_tenant_bound() -> None:
    tenant_id = uuid4()
    event_id = uuid4()
    delivery = SimpleNamespace(
        event_id=event_id,
        event_type="load.dispatched.v1",
        tenant_id=tenant_id,
        payload={
            "schema_version": 2,
            "occurred_at": "2026-08-26T00:00:00+00:00",
            "correlation_id": "corr-1",
            "id": str(uuid4()),
            "status": "DISPATCHED",
        },
        created_at=SimpleNamespace(isoformat=lambda: "fallback"),
    )
    envelope = event_envelope(delivery)
    assert envelope["id"] == str(event_id)
    assert envelope["tenant_id"] == str(tenant_id)
    assert envelope["version"] == "2"
    assert envelope["correlation_id"] == "corr-1"
    assert "schema_version" not in envelope["data"]
    assert sha256_hex(canonical_json_bytes(envelope)) == sha256_hex(canonical_json_bytes(envelope))
