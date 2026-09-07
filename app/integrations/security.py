from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urljoin, urlparse

from fastapi import HTTPException, status

_SECRET_REF = re.compile(r"^env:([A-Z][A-Z0-9_]{2,127})$")
_REDACTED_HEADERS = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
    "x-webhook-signature",
    "x-freight-signature",
}


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def resolve_secret(secret_ref: str | None) -> str:
    if not secret_ref:
        raise RuntimeError("SECRET_REFERENCE_MISSING")
    match = _SECRET_REF.fullmatch(secret_ref)
    if match is None:
        raise RuntimeError("UNSUPPORTED_SECRET_REFERENCE")
    value = os.getenv(match.group(1))
    if not value:
        raise RuntimeError("SECRET_NOT_AVAILABLE")
    return value


def parse_signature_timestamp(raw: str, tolerance_seconds: int | None = None) -> datetime:
    try:
        parsed = datetime.fromtimestamp(int(raw), tz=timezone.utc)
    except (TypeError, ValueError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_WEBHOOK_TIMESTAMP", "message": "Webhook timestamp is invalid."},
        ) from exc

    tolerance = tolerance_seconds or int(os.getenv("INTEGRATION_WEBHOOK_TOLERANCE_SECONDS", "300"))
    age = abs((datetime.now(timezone.utc) - parsed).total_seconds())
    if age > tolerance:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "WEBHOOK_TIMESTAMP_EXPIRED",
                "message": "Webhook timestamp is outside the accepted replay window.",
            },
        )
    return parsed


def signature_hex(secret: str, timestamp: str, raw_body: bytes) -> str:
    signed = timestamp.encode("utf-8") + b"." + raw_body
    return hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def verify_signature(*, secret: str, timestamp: str, raw_body: bytes, supplied: str) -> None:
    supplied_value = supplied.removeprefix("sha256=").strip().lower()
    expected = signature_hex(secret, timestamp, raw_body)
    if not hmac.compare_digest(expected, supplied_value):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "INVALID_WEBHOOK_SIGNATURE",
                "message": "Webhook signature validation failed.",
            },
        )


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in _REDACTED_HEADERS or "secret" in lowered or "token" in lowered:
            result[name] = "[REDACTED]"
        else:
            result[name] = value[:500]
    return result


def allowed_destination_hosts() -> set[str]:
    return {
        item.strip().lower()
        for item in os.getenv("INTEGRATION_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    }


def _literal_ip_is_private(hostname: str) -> bool:
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def validate_destination_url(base_url: str, endpoint_path: str | None = None) -> str:
    resolved = urljoin(base_url.rstrip("/") + "/", (endpoint_path or "").lstrip("/"))
    parsed = urlparse(resolved)
    environment = os.getenv("ENVIRONMENT", "development").lower()

    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("INVALID_DESTINATION_URL")
    if parsed.username or parsed.password:
        raise ValueError("DESTINATION_USERINFO_FORBIDDEN")
    if environment == "production" and parsed.scheme != "https":
        raise ValueError("DESTINATION_TLS_REQUIRED")

    host = parsed.hostname.lower()
    allowlist = allowed_destination_hosts()
    if environment == "production" and not allowlist:
        raise ValueError("DESTINATION_ALLOWLIST_REQUIRED")
    if allowlist and host not in allowlist:
        raise ValueError("DESTINATION_HOST_NOT_ALLOWED")
    if not allowlist and (host in {"localhost", "localhost.localdomain"} or _literal_ip_is_private(host)):
        raise ValueError("PRIVATE_DESTINATION_NOT_ALLOWED")

    return resolved


def signed_headers(
    *,
    secret: str,
    key_id: str,
    event_id: str,
    raw_body: bytes,
    timestamp: int | None = None,
) -> dict[str, str]:
    unix_time = timestamp or int(datetime.now(timezone.utc).timestamp())
    timestamp_value = str(unix_time)
    return {
        "Content-Type": "application/json",
        "Idempotency-Key": event_id,
        "X-Freight-Event-Id": event_id,
        "X-Freight-Timestamp": timestamp_value,
        "X-Freight-Key-Id": key_id,
        "X-Freight-Signature": f"sha256={signature_hex(secret, timestamp_value, raw_body)}",
    }
