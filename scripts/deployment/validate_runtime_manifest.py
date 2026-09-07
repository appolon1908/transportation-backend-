from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = 1
UNVERIFIED = "UNVERIFIED"
MAX_MANIFEST_BYTES = 128 * 1024
DIGEST_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*(?::[a-z0-9._-]+)?@sha256:[0-9a-f]{64}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
IDENTITY_RE = re.compile(r"^[a-z_][a-z0-9_]{1,62}$")
EVIDENCE_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
EXPECTED_DATABASE_USERS = {
    "migrator": "freight_migrator",
    "api": "freight_api",
    "ingress": "freight_ingress",
    "worker": "freight_worker",
}
FORBIDDEN_PATHS = {
    PurePosixPath("/"),
    PurePosixPath("/bin"),
    PurePosixPath("/boot"),
    PurePosixPath("/dev"),
    PurePosixPath("/etc"),
    PurePosixPath("/home"),
    PurePosixPath("/lib"),
    PurePosixPath("/lib64"),
    PurePosixPath("/opt"),
    PurePosixPath("/proc"),
    PurePosixPath("/root"),
    PurePosixPath("/run"),
    PurePosixPath("/sbin"),
    PurePosixPath("/srv"),
    PurePosixPath("/sys"),
    PurePosixPath("/tmp"),
    PurePosixPath("/usr"),
    PurePosixPath("/var"),
}
REQUIRED_PATHS = {
    "application_root",
    "compose_project_dir",
    "secrets_root",
    "evidence_root",
    "backup_root",
    "backend_compose",
    "gateway_compose",
    "caddyfile",
    "kong_template",
}
REQUIRED_IMAGES = {
    "backend",
    "gateway_renderer",
    "redis",
    "kong",
    "caddy",
}
REQUIRED_CAPABILITIES = {
    "external_delivery",
    "odoo_write",
    "n8n_delivery",
    "customer_portal_external_access",
    "carrier_portal_external_access",
    "email_live_send",
    "sms_live_send",
    "production_dispatch",
}
REQUIRED_IDENTITIES = {"migrator", "api", "ingress", "worker"}


class ManifestError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ManifestError(f"unable to read manifest: {exc}") from exc
    if not raw:
        raise ManifestError("manifest is empty")
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ManifestError(f"manifest exceeds {MAX_MANIFEST_BYTES} bytes")
    try:
        payload = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ManifestError) as exc:
        raise ManifestError(f"manifest is not valid unambiguous JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ManifestError("manifest root must be an object")
    return payload, raw


def _require_object(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise ManifestError(f"{key} must be an object")
    return value


def _require_exact_keys(value: dict[str, Any], required: set[str], label: str) -> None:
    missing = sorted(required - set(value))
    extra = sorted(set(value) - required)
    if missing:
        raise ManifestError(f"{label} is missing keys: {', '.join(missing)}")
    if extra:
        raise ManifestError(f"{label} has unsupported keys: {', '.join(extra)}")


def _is_unverified(value: Any) -> bool:
    return value in {UNVERIFIED, "", None}


def _validate_host(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError("runtime.host must be a non-empty string")
    if value.lower() in {"localhost", "localhost.localdomain"}:
        raise ManifestError("runtime.host must not resolve to localhost")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if not HOST_RE.fullmatch(value) or ".." in value:
            raise ManifestError("runtime.host is not a valid hostname or IP address")
        return value
    if address.is_loopback or address.is_unspecified or address.is_multicast:
        raise ManifestError("runtime.host must not be loopback, unspecified, or multicast")
    return value


def _validate_path(label: str, value: Any) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"runtime.paths.{label} must be a non-empty string")
    if any(character in value for character in ("\x00", "\n", "\r", "\t")):
        raise ManifestError(f"runtime.paths.{label} contains control characters")
    if any(character in value for character in ("$", "`", "|", ";", "&", "<", ">")):
        raise ManifestError(f"runtime.paths.{label} contains shell metacharacters")
    path = PurePosixPath(value)
    if not path.is_absolute():
        raise ManifestError(f"runtime.paths.{label} must be absolute")
    if ".." in path.parts:
        raise ManifestError(f"runtime.paths.{label} must not contain parent traversal")
    normalized = PurePosixPath("/", *[part for part in path.parts if part not in {"/", "."}])
    if normalized in FORBIDDEN_PATHS:
        raise ManifestError(f"runtime.paths.{label} is too broad or dangerous: {normalized}")
    if len(normalized.parts) < 3:
        raise ManifestError(f"runtime.paths.{label} must be below a dedicated application directory")
    return normalized


def _is_relative_to(child: PurePosixPath, parent: PurePosixPath) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ManifestError("verification.verified_at must be an RFC3339 timestamp")
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ManifestError("verification.verified_at is not a valid RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ManifestError("verification.verified_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def validate_manifest(
    payload: dict[str, Any],
    *,
    expected_environment: str,
    expected_source_sha: str | None,
    allow_unverified: bool,
    max_verification_age_hours: int,
) -> dict[str, Any]:
    _require_exact_keys(
        payload,
        {
            "schema_version",
            "environment",
            "source",
            "runtime",
            "release",
            "service_identities",
            "capabilities",
            "verification",
        },
        "manifest",
    )
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ManifestError(f"schema_version must equal {SCHEMA_VERSION}")
    if payload.get("environment") != expected_environment:
        raise ManifestError(
            f"environment must equal {expected_environment!r}; got {payload.get('environment')!r}"
        )

    source = _require_object(payload, "source")
    _require_exact_keys(source, {"repository", "sha"}, "source")
    if source.get("repository") != "appolon1908-hue/transportation-backend-":
        raise ManifestError("source.repository must identify the authoritative backend repository")

    runtime = _require_object(payload, "runtime")
    _require_exact_keys(runtime, {"kind", "host", "paths", "networks"}, "runtime")
    if runtime.get("kind") != "docker-compose":
        raise ManifestError("runtime.kind must equal 'docker-compose'")
    paths = _require_object(runtime, "paths")
    networks = _require_object(runtime, "networks")
    _require_exact_keys(paths, REQUIRED_PATHS, "runtime.paths")
    _require_exact_keys(networks, {"backend", "gateway"}, "runtime.networks")

    release = _require_object(payload, "release")
    _require_exact_keys(release, REQUIRED_IMAGES, "release")

    identities = _require_object(payload, "service_identities")
    _require_exact_keys(identities, REQUIRED_IDENTITIES, "service_identities")
    for name, identity_value in identities.items():
        if not isinstance(identity_value, dict):
            raise ManifestError(f"service_identities.{name} must be an object")
        _require_exact_keys(
            identity_value,
            {"database_user", "can_bypass_rls", "long_lived"},
            f"service_identities.{name}",
        )
        if not isinstance(identity_value.get("can_bypass_rls"), bool):
            raise ManifestError(f"service_identities.{name}.can_bypass_rls must be boolean")
        if not isinstance(identity_value.get("long_lived"), bool):
            raise ManifestError(f"service_identities.{name}.long_lived must be boolean")

    capabilities = _require_object(payload, "capabilities")
    _require_exact_keys(capabilities, REQUIRED_CAPABILITIES, "capabilities")
    for name, enabled in capabilities.items():
        if enabled is not False:
            raise ManifestError(f"capabilities.{name} must remain false during preflight")

    verification = _require_object(payload, "verification")
    _require_exact_keys(
        verification,
        {
            "status",
            "verified_by",
            "verified_at",
            "evidence_reference",
            "deployment_authorized",
            "notes",
        },
        "verification",
    )
    if verification.get("deployment_authorized") is not False:
        raise ManifestError("verification.deployment_authorized must remain false in preflight")

    status = verification.get("status")
    unverified = status == "unverified"
    if unverified and not allow_unverified:
        raise ManifestError("runtime paths are unverified; deployment preflight remains blocked")
    if status not in {"unverified", "verified-read-only"}:
        raise ManifestError("verification.status must be 'unverified' or 'verified-read-only'")

    if unverified:
        values_to_check: list[Any] = [
            source.get("sha"),
            runtime.get("host"),
            *paths.values(),
            *networks.values(),
            *release.values(),
            *[identity["database_user"] for identity in identities.values()],
        ]
        if any(not _is_unverified(value) for value in values_to_check):
            raise ManifestError(
                "an unverified example must not contain values that could be mistaken for approved runtime data"
            )
        return {
            "status": "scaffold-valid-unverified",
            "environment": expected_environment,
            "runtime_paths_verified": False,
            "deployment_authorized": False,
        }

    source_sha = source.get("sha")
    if not isinstance(source_sha, str) or not SHA_RE.fullmatch(source_sha):
        raise ManifestError("source.sha must be a 40-character lowercase Git SHA")
    if expected_source_sha and source_sha != expected_source_sha:
        raise ManifestError(
            f"source.sha does not match requested source SHA: {source_sha} != {expected_source_sha}"
        )

    _validate_host(runtime.get("host"))
    normalized_paths = {name: _validate_path(name, value) for name, value in paths.items()}
    if len(set(normalized_paths.values())) != len(normalized_paths):
        raise ManifestError("runtime paths must be unique")
    application_root = normalized_paths["application_root"]
    compose_root = normalized_paths["compose_project_dir"]
    if not _is_relative_to(compose_root, application_root):
        raise ManifestError("compose_project_dir must be below application_root")
    for key in ("backend_compose", "gateway_compose", "caddyfile", "kong_template"):
        if not _is_relative_to(normalized_paths[key], compose_root):
            raise ManifestError(f"{key} must be below compose_project_dir")
    for protected_root in ("secrets_root", "backup_root", "evidence_root"):
        if _is_relative_to(normalized_paths[protected_root], application_root):
            raise ManifestError(f"{protected_root} must not be inside application_root")
    if normalized_paths["backup_root"] == normalized_paths["evidence_root"]:
        raise ManifestError("backup_root and evidence_root must be separate")

    for name, network in networks.items():
        if not isinstance(network, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{1,62}", network):
            raise ManifestError(f"runtime.networks.{name} is invalid")

    for name, image in release.items():
        if not isinstance(image, str) or not DIGEST_RE.fullmatch(image):
            raise ManifestError(f"release.{name} must be pinned by sha256 digest")

    database_users: dict[str, str] = {}
    for name, identity in identities.items():
        user = identity.get("database_user")
        if not isinstance(user, str) or not IDENTITY_RE.fullmatch(user):
            raise ManifestError(f"service_identities.{name}.database_user is invalid")
        expected_user = EXPECTED_DATABASE_USERS[name]
        if user != expected_user:
            raise ManifestError(
                f"service_identities.{name}.database_user must equal {expected_user!r}"
            )
        database_users[name] = user
    if len(set(database_users.values())) != len(database_users):
        raise ManifestError("migrator, API, ingress, and worker database users must be distinct")
    for name in ("api", "ingress", "worker"):
        if identities[name]["can_bypass_rls"] is not False:
            raise ManifestError(f"service_identities.{name} must be NOBYPASSRLS")
        if identities[name]["long_lived"] is not True:
            raise ManifestError(f"service_identities.{name} must be marked long_lived")
    if identities["migrator"]["long_lived"] is not False:
        raise ManifestError("migrator credential must be short-lived and unavailable to runtime services")

    verified_by = verification.get("verified_by")
    evidence_reference = verification.get("evidence_reference")
    notes = verification.get("notes")
    if not isinstance(verified_by, str) or len(verified_by.strip()) < 3:
        raise ManifestError("verification.verified_by must identify the read-only verifier")
    verified_at = _parse_timestamp(verification.get("verified_at"))
    now = datetime.now(timezone.utc)
    if verified_at > now + timedelta(minutes=5):
        raise ManifestError("verification.verified_at must not be in the future")
    if now - verified_at > timedelta(hours=max_verification_age_hours):
        raise ManifestError(
            f"read-only runtime verification is older than {max_verification_age_hours} hours"
        )
    if not isinstance(evidence_reference, str) or not EVIDENCE_RE.fullmatch(evidence_reference):
        raise ManifestError(
            "verification.evidence_reference must be the sha256 digest of immutable read-only evidence"
        )
    if not isinstance(notes, str) or len(notes) > 2000:
        raise ManifestError("verification.notes must be a string no longer than 2000 characters")

    return {
        "status": "preflight-valid",
        "environment": expected_environment,
        "source_sha": source_sha,
        "runtime_host": runtime["host"],
        "runtime_paths_verified": True,
        "deployment_authorized": False,
        "capabilities_enabled": [],
        "database_users": database_users,
    }


def _write_evidence(
    output_path: Path,
    *,
    manifest_path: Path,
    raw: bytes,
    result: dict[str, Any] | None,
    error: str | None,
) -> None:
    evidence = {
        "manifest": str(manifest_path),
        "manifest_sha256": hashlib.sha256(raw).hexdigest() if raw else None,
        "validation": result,
        "error": error,
        "host_contacted": False,
        "deployment_performed": False,
        "external_capabilities_enabled": False,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate the read-only freight deployment runtime-path manifest."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--expected-environment", choices=["staging", "production"], required=True)
    parser.add_argument("--expected-source-sha")
    parser.add_argument("--allow-unverified", action="store_true")
    parser.add_argument("--max-verification-age-hours", type=int, default=168)
    parser.add_argument("--evidence", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    raw = b""
    result: dict[str, Any] | None = None
    error: str | None = None
    try:
        payload, raw = _load_json(args.manifest)
        if args.max_verification_age_hours < 1 or args.max_verification_age_hours > 720:
            raise ManifestError("max verification age must be between 1 and 720 hours")
        result = validate_manifest(
            payload,
            expected_environment=args.expected_environment,
            expected_source_sha=args.expected_source_sha,
            allow_unverified=args.allow_unverified,
            max_verification_age_hours=args.max_verification_age_hours,
        )
    except ManifestError as exc:
        error = str(exc)
    if args.evidence:
        _write_evidence(
            args.evidence,
            manifest_path=args.manifest,
            raw=raw,
            result=result,
            error=error,
        )
    if error:
        print(f"RUNTIME_MANIFEST=BLOCKED: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
