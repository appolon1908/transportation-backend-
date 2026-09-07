from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any


_SECRET_REF = re.compile(r"^env://[A-Z][A-Z0-9_]{2,127}$")


def canonical_json(value: Any) -> bytes:
    """Return the stable JSON representation used for signatures and hashes."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def payload_hash(value: Any) -> str:
    return sha256_hex(canonical_json(value))


def signature_for(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        timestamp.encode("ascii") + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


def verify_signature(secret: str, timestamp: str, body: bytes, supplied: str) -> bool:
    expected = signature_for(secret, timestamp, body)
    normalized = supplied if supplied.startswith("sha256=") else f"sha256={supplied}"
    return hmac.compare_digest(expected, normalized)


def validate_timestamp(value: str, tolerance_seconds: int = 300) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError("Webhook timestamp must be a Unix timestamp.") from exc
    if abs(int(time.time()) - parsed) > tolerance_seconds:
        raise ValueError("Webhook timestamp is outside the accepted replay window.")
    return parsed


def validate_secret_ref(secret_ref: str) -> str:
    if not _SECRET_REF.fullmatch(secret_ref):
        raise ValueError("Only env://UPPER_CASE secret references are accepted.")
    return secret_ref


@dataclass(frozen=True)
class SecretResolver:
    """Resolve opaque references without persisting or logging secret values."""

    def resolve(self, secret_ref: str) -> str:
        validate_secret_ref(secret_ref)
        env_name = secret_ref.removeprefix("env://")
        value = os.getenv(env_name)
        if not value:
            raise RuntimeError(f"Secret reference {secret_ref} is unavailable.")
        return value
