from __future__ import annotations

import hashlib
import hmac
import os


def hash_identifier(value: str) -> str:
    normalized = "".join(value.upper().split())
    environment = os.getenv("ENVIRONMENT", "development").lower()
    pepper = os.getenv("COMPLIANCE_IDENTIFIER_PEPPER")
    if environment == "production" and not pepper:
        raise RuntimeError("COMPLIANCE_IDENTIFIER_PEPPER is required in production.")
    pepper = pepper or "development-only-pepper"
    return hmac.new(pepper.encode(), normalized.encode(), hashlib.sha256).hexdigest()


def validate_evidence_hash(value: str) -> str:
    normalized = value.lower().removeprefix("sha256:")
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError("Evidence hash must be a SHA-256 hex digest.")
    return normalized
