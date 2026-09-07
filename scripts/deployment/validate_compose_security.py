from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DIGEST_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
EXPECTED_DATABASE_USERS = {
    "migrate": {
        "DATABASE_URL": "freight_migrator",
        "INGRESS_DATABASE_URL": "freight_ingress",
        "WORKER_DATABASE_URL": "freight_worker",
    },
    "freight-api": {
        "DATABASE_URL": "freight_api",
        "INGRESS_DATABASE_URL": "freight_ingress",
        "WORKER_DATABASE_URL": "freight_worker",
    },
    "integration-worker": {
        "DATABASE_URL": "freight_api",
        "INGRESS_DATABASE_URL": "freight_ingress",
        "WORKER_DATABASE_URL": "freight_worker",
    },
}


class ComposeSecurityError(ValueError):
    pass


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ComposeSecurityError(f"unable to read rendered Compose JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ComposeSecurityError(f"rendered Compose model {path} must be an object")
    return value


def _services(model: dict[str, Any], label: str) -> dict[str, dict[str, Any]]:
    services = model.get("services")
    if not isinstance(services, dict) or not services:
        raise ComposeSecurityError(f"{label} has no services")
    if not all(isinstance(name, str) and isinstance(service, dict) for name, service in services.items()):
        raise ComposeSecurityError(f"{label}.services is malformed")
    return services  # type: ignore[return-value]


def _environment(service: dict[str, Any]) -> dict[str, str]:
    value = service.get("environment", {})
    if isinstance(value, dict):
        return {str(key): "" if item is None else str(item) for key, item in value.items()}
    if isinstance(value, list):
        result: dict[str, str] = {}
        for item in value:
            if not isinstance(item, str):
                raise ComposeSecurityError("service environment list contains a non-string")
            key, separator, content = item.partition("=")
            result[key] = content if separator else ""
        return result
    raise ComposeSecurityError("service environment must be an object or list")


def _as_string_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if not isinstance(value, list):
        raise ComposeSecurityError("expected a list")
    return {str(item) for item in value}


def _volume_entries(service: dict[str, Any]) -> list[dict[str, Any]]:
    values = service.get("volumes", [])
    if values is None:
        return []
    if not isinstance(values, list):
        raise ComposeSecurityError("service volumes must be a list")
    result: list[dict[str, Any]] = []
    for value in values:
        if isinstance(value, dict):
            result.append(value)
        elif isinstance(value, str):
            source, _, remainder = value.partition(":")
            target, _, mode = remainder.partition(":")
            result.append(
                {
                    "type": "bind" if source.startswith(("/", ".")) else "volume",
                    "source": source,
                    "target": target,
                    "read_only": "ro" in mode.split(","),
                }
            )
        else:
            raise ComposeSecurityError("service volumes contains an unsupported entry")
    return result


def _network_names(service: dict[str, Any]) -> set[str]:
    value = service.get("networks", [])
    if isinstance(value, list):
        return {str(item) for item in value}
    if isinstance(value, dict):
        return {str(item) for item in value}
    raise ComposeSecurityError("service networks must be a list or object")


def _port_entries(service: dict[str, Any]) -> list[dict[str, Any]]:
    values = service.get("ports", [])
    if values is None:
        return []
    if not isinstance(values, list):
        raise ComposeSecurityError("service ports must be a list")
    result: list[dict[str, Any]] = []
    for value in values:
        if isinstance(value, dict):
            result.append(value)
        elif isinstance(value, str):
            pieces = value.split(":")
            if len(pieces) == 2:
                published, target = pieces
            elif len(pieces) == 3:
                _host, published, target = pieces
            else:
                raise ComposeSecurityError(f"unsupported port syntax: {value}")
            result.append({"published": published, "target": target, "protocol": "tcp"})
        else:
            raise ComposeSecurityError("service ports contains an unsupported entry")
    return result


def _validate_common_service(name: str, service: dict[str, Any], *, transient: bool) -> None:
    image = service.get("image")
    if not isinstance(image, str) or not DIGEST_RE.fullmatch(image):
        raise ComposeSecurityError(f"{name}.image must be pinned by sha256 digest")
    if "build" in service:
        raise ComposeSecurityError(f"{name} must not build on a deployment host")
    if service.get("privileged") is True:
        raise ComposeSecurityError(f"{name} must not be privileged")
    if str(service.get("network_mode", "")).lower() == "host":
        raise ComposeSecurityError(f"{name} must not use host networking")
    if str(service.get("pid", "")).lower() == "host":
        raise ComposeSecurityError(f"{name} must not use the host PID namespace")
    if str(service.get("ipc", "")).lower() == "host":
        raise ComposeSecurityError(f"{name} must not use the host IPC namespace")
    if service.get("read_only") is not True:
        raise ComposeSecurityError(f"{name} must use a read-only root filesystem")
    security_options = {item.replace("=", ":") for item in _as_string_set(service.get("security_opt"))}
    if "no-new-privileges:true" not in security_options:
        raise ComposeSecurityError(f"{name} must set no-new-privileges:true")
    if "ALL" not in {item.upper() for item in _as_string_set(service.get("cap_drop"))}:
        raise ComposeSecurityError(f"{name} must drop all Linux capabilities")
    added = {item.upper() for item in _as_string_set(service.get("cap_add"))}
    allowed_added = {"NET_BIND_SERVICE"} if name == "caddy" else set()
    if added - allowed_added:
        raise ComposeSecurityError(f"{name} adds unsupported capabilities: {sorted(added - allowed_added)}")
    if transient:
        if str(service.get("restart", "no")).lower() not in {"no", "none", ""}:
            raise ComposeSecurityError(f"{name} must not restart automatically")
    else:
        pids_limit = service.get("pids_limit")
        if not isinstance(pids_limit, int) or pids_limit <= 0 or pids_limit > 2048:
            raise ComposeSecurityError(f"{name} must define a bounded positive pids_limit")
        if not isinstance(service.get("healthcheck"), dict):
            raise ComposeSecurityError(f"{name} must define a healthcheck")
    for volume in _volume_entries(service):
        source = str(volume.get("source", ""))
        target = str(volume.get("target", ""))
        combined = f"{source}:{target}".lower()
        if "docker.sock" in combined or "/run/containerd" in combined:
            raise ComposeSecurityError(f"{name} must not mount a container-runtime socket")
        if volume.get("type") == "bind":
            if name != "caddy" or target != "/etc/caddy/Caddyfile" or volume.get("read_only") is not True:
                raise ComposeSecurityError(
                    f"{name} has an unapproved bind mount; only Caddyfile:ro is allowed"
                )


def _database_username(value: str, label: str) -> str:
    username = urlparse(value).username
    if not username:
        raise ComposeSecurityError(f"{label} must contain an explicit database username")
    return username


def _validate_backend(model: dict[str, Any]) -> dict[str, Any]:
    services = _services(model, "backend")
    expected = {"migrate", "freight-api", "integration-worker"}
    if set(services) != expected:
        raise ComposeSecurityError(
            f"backend services must equal {sorted(expected)}; got {sorted(services)}"
        )
    for name, service in services.items():
        _validate_common_service(name, service, transient=name == "migrate")
        if _port_entries(service):
            raise ComposeSecurityError(f"{name} must not publish host ports")
        if _network_names(service) != {"freight_private"}:
            raise ComposeSecurityError(f"{name} must attach only to freight_private")
        environment = _environment(service)
        expected_users = EXPECTED_DATABASE_USERS[name]
        observed: dict[str, str] = {}
        for variable, expected_user in expected_users.items():
            if variable not in environment:
                raise ComposeSecurityError(f"{name} is missing {variable}")
            user = _database_username(environment[variable], f"{name}.{variable}")
            observed[variable] = user
            if user != expected_user:
                raise ComposeSecurityError(
                    f"{name}.{variable} must resolve to {expected_user!r} in CI; got {user!r}"
                )
        if len(set(observed.values())) != 3:
            raise ComposeSecurityError(f"{name} must receive distinct API, ingress, and worker identities")
        if name != "migrate" and "MIGRATOR_DATABASE_URL" in environment:
            raise ComposeSecurityError(f"{name} must not receive the migrator credential")
    networks = model.get("networks", {})
    if not isinstance(networks, dict) or not isinstance(networks.get("freight_private"), dict):
        raise ComposeSecurityError("backend must define freight_private network")
    if networks["freight_private"].get("external") is not True:
        raise ComposeSecurityError("backend freight_private network must be external")
    return {
        "services": sorted(services),
        "external_network": "freight_private",
        "production_blockers": [
            "Application bootstrap currently requires API, ingress, and worker DSNs in each backend process; process-level database credential minimization is not yet achieved."
        ],
    }


def _validate_gateway(model: dict[str, Any]) -> dict[str, Any]:
    services = _services(model, "gateway")
    expected = {"kong-config-render", "redis", "kong", "caddy"}
    if set(services) != expected:
        raise ComposeSecurityError(
            f"gateway services must equal {sorted(expected)}; got {sorted(services)}"
        )
    expected_networks = {
        "kong-config-render": {"gateway_internal"},
        "redis": {"gateway_internal"},
        "kong": {"gateway_internal", "freight_private"},
        "caddy": {"gateway_public", "gateway_internal"},
    }
    for name, service in services.items():
        _validate_common_service(name, service, transient=name == "kong-config-render")
        if _network_names(service) != expected_networks[name]:
            raise ComposeSecurityError(
                f"{name} network membership must equal {sorted(expected_networks[name])}"
            )
        ports = _port_entries(service)
        if name != "caddy" and ports:
            raise ComposeSecurityError(f"{name} must not publish host ports")
    caddy_ports = {
        (int(port.get("published")), int(port.get("target")), str(port.get("protocol", "tcp")))
        for port in _port_entries(services["caddy"])
    }
    required_ports = {(80, 80, "tcp"), (443, 443, "tcp"), (443, 443, "udp")}
    if caddy_ports != required_ports:
        raise ComposeSecurityError(
            f"caddy published ports must equal {sorted(required_ports)}; got {sorted(caddy_ports)}"
        )
    networks = model.get("networks", {})
    if not isinstance(networks, dict):
        raise ComposeSecurityError("gateway networks must be an object")
    if not isinstance(networks.get("gateway_internal"), dict) or networks["gateway_internal"].get("internal") is not True:
        raise ComposeSecurityError("gateway_internal must be an internal network")
    if not isinstance(networks.get("freight_private"), dict) or networks["freight_private"].get("external") is not True:
        raise ComposeSecurityError("gateway freight_private network must be external")
    return {"services": sorted(services), "published_ports": sorted(caddy_ports)}


def _write_evidence(path: Path, *, result: dict[str, Any] | None, error: str | None) -> None:
    value = {
        "validation": result,
        "error": error,
        "host_contacted": False,
        "deployment_performed": False,
        "live_capabilities_enabled": False,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate rendered freight Compose security invariants.")
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--gateway", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result: dict[str, Any] | None = None
    error: str | None = None
    try:
        result = {
            "backend": _validate_backend(_load(args.backend)),
            "gateway": _validate_gateway(_load(args.gateway)),
        }
    except ComposeSecurityError as exc:
        error = str(exc)
    if args.evidence:
        _write_evidence(args.evidence, result=result, error=error)
    if error:
        print(f"COMPOSE_SECURITY=BLOCKED: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
