from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.integrations.models import IntegrationConnection, IntegrationDelivery
from app.integrations.security import (
    canonical_json_bytes,
    resolve_secret,
    sha256_hex,
    signed_headers,
    validate_destination_url,
)


@dataclass(frozen=True)
class DeliveryResult:
    """Bounded delivery evidence returned to the durable worker.

    Provider response bodies are never retained. Only a bounded hash, status and
    a sanitized error classification cross the adapter boundary.
    """

    success: bool
    retryable: bool
    status_code: int | None
    response_hash: str | None
    error_code: str | None = None
    error_detail: str | None = None
    duration_ms: int = 0


class OutboundAdapter(Protocol):
    async def deliver(
        self,
        connection: IntegrationConnection,
        delivery: IntegrationDelivery,
    ) -> DeliveryResult: ...


def event_envelope(delivery: IntegrationDelivery) -> dict[str, Any]:
    """Return the canonical versioned, tenant-bound outbound event envelope."""

    payload = dict(delivery.payload or {})
    schema_version = str(payload.pop("schema_version", 1))
    correlation_id = payload.pop("correlation_id", None)
    occurred_at = payload.pop("occurred_at", None)
    if occurred_at is None and getattr(delivery, "created_at", None) is not None:
        occurred_at = delivery.created_at.isoformat()
    return {
        "id": str(delivery.event_id),
        "type": delivery.event_type,
        "version": schema_version,
        "tenant_id": str(delivery.tenant_id),
        "correlation_id": str(correlation_id) if correlation_id else None,
        "occurred_at": occurred_at,
        "data": payload,
    }


def _bounded_response_hash(content: bytes) -> str:
    return sha256_hex(content[:65_536])


def _result_from_response(response: httpx.Response, started: float) -> DeliveryResult:
    duration_ms = max(0, round((time.perf_counter() - started) * 1_000))
    response_hash = _bounded_response_hash(response.content)
    if 200 <= response.status_code < 300:
        return DeliveryResult(
            success=True,
            retryable=False,
            status_code=response.status_code,
            response_hash=response_hash,
            duration_ms=duration_ms,
        )
    retryable = response.status_code in {408, 409, 425, 429} or response.status_code >= 500
    return DeliveryResult(
        success=False,
        retryable=retryable,
        status_code=response.status_code,
        response_hash=response_hash,
        error_code="REMOTE_RETRYABLE" if retryable else "REMOTE_REJECTED",
        error_detail=f"Destination returned HTTP {response.status_code}.",
        duration_ms=duration_ms,
    )


def _configuration_error(exc: RuntimeError | ValueError, started: float) -> DeliveryResult:
    duration_ms = max(0, round((time.perf_counter() - started) * 1_000))
    return DeliveryResult(
        success=False,
        retryable=False,
        status_code=None,
        response_hash=None,
        error_code=str(exc)[:120] or "CONFIGURATION_ERROR",
        error_detail="Delivery configuration failed validation.",
        duration_ms=duration_ms,
    )


def _network_error(exc: Exception, started: float) -> DeliveryResult:
    duration_ms = max(0, round((time.perf_counter() - started) * 1_000))
    return DeliveryResult(
        success=False,
        retryable=True,
        status_code=None,
        response_hash=None,
        error_code="NETWORK_RETRYABLE",
        error_detail=type(exc).__name__,
        duration_ms=duration_ms,
    )


def _verify_tls_contract(connection: IntegrationConnection) -> None:
    if os.getenv("ENVIRONMENT", "development").lower() == "production" and not connection.verify_tls:
        raise ValueError("DESTINATION_TLS_VERIFICATION_REQUIRED")


class SignedWebhookAdapter:
    async def deliver(
        self,
        connection: IntegrationConnection,
        delivery: IntegrationDelivery,
    ) -> DeliveryResult:
        started = time.perf_counter()
        try:
            _verify_tls_contract(connection)
            target = validate_destination_url(connection.base_url, connection.endpoint_path)
            secret = resolve_secret(connection.signing_secret_ref)
            body = canonical_json_bytes(event_envelope(delivery))
            headers = signed_headers(
                secret=secret,
                key_id=connection.signing_key_id or "default",
                event_id=str(delivery.event_id),
                raw_body=body,
            )
            headers["User-Agent"] = "freight-platform-backend/1"
            headers["X-Freight-Tenant-Id"] = str(delivery.tenant_id)
            correlation_id = event_envelope(delivery).get("correlation_id")
            if correlation_id:
                headers["X-Correlation-Id"] = str(correlation_id)
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connection.timeout_seconds),
                follow_redirects=False,
                verify=connection.verify_tls,
            ) as client:
                response = await client.post(target, content=body, headers=headers)
            return _result_from_response(response, started)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            return _network_error(exc, started)
        except (RuntimeError, ValueError) as exc:
            return _configuration_error(exc, started)


class N8nWebhookAdapter(SignedWebhookAdapter):
    """n8n consumes the same signed durable envelope as generic webhooks."""


class OdooJson2Adapter:
    async def deliver(
        self,
        connection: IntegrationConnection,
        delivery: IntegrationDelivery,
    ) -> DeliveryResult:
        started = time.perf_counter()
        try:
            _verify_tls_contract(connection)
            configuration = dict(connection.configuration or {})
            model = str(configuration.get("model") or "").strip()
            method = str(configuration.get("method") or "").strip()
            argument_name = str(configuration.get("argument_name") or "event").strip()
            endpoint_path = connection.endpoint_path
            if not endpoint_path:
                if not model or not method:
                    raise ValueError("ODOO_JSON2_MODEL_METHOD_REQUIRED")
                endpoint_path = f"json/2/{model}/{method}"
            if not argument_name:
                raise ValueError("ODOO_JSON2_ARGUMENT_NAME_REQUIRED")

            target = validate_destination_url(connection.base_url, endpoint_path)
            api_key = resolve_secret(connection.secret_ref)
            body = canonical_json_bytes({argument_name: event_envelope(delivery)})
            headers = {
                "Authorization": f"bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "freight-platform-backend/1",
                "Idempotency-Key": str(delivery.event_id),
            }
            correlation_id = event_envelope(delivery).get("correlation_id")
            if correlation_id:
                headers["X-Correlation-Id"] = str(correlation_id)
            database = configuration.get("database")
            if database:
                headers["X-Odoo-Database"] = str(database)

            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connection.timeout_seconds),
                follow_redirects=False,
                verify=connection.verify_tls,
            ) as client:
                response = await client.post(target, content=body, headers=headers)
            return _result_from_response(response, started)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            return _network_error(exc, started)
        except (RuntimeError, ValueError) as exc:
            return _configuration_error(exc, started)


ADAPTERS: dict[str, OutboundAdapter] = {
    "SIGNED_WEBHOOK": SignedWebhookAdapter(),
    "N8N_WEBHOOK": N8nWebhookAdapter(),
    "ODOO_JSON2": OdooJson2Adapter(),
}


def adapter_for(kind: str) -> OutboundAdapter:
    try:
        return ADAPTERS[kind.upper()]
    except KeyError as exc:
        raise ValueError("UNSUPPORTED_INTEGRATION_KIND") from exc


async def deliver(
    connection: IntegrationConnection,
    delivery: IntegrationDelivery,
) -> DeliveryResult:
    """Dispatch through the reviewed adapter selected by persisted connection kind."""

    return await adapter_for(connection.kind).deliver(connection, delivery)
