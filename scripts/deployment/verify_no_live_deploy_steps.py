from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
USES_RE = re.compile(r"\buses:\s*([^@\s]+)@([^\s#]+)")
WRITE_PERMISSION_RE = re.compile(r"^\s+[A-Za-z0-9_-]+:\s*write\s*$", re.MULTILINE)
JOB_ENVIRONMENT_RE = re.compile(r"^\s{4,8}environment:\s*\S+\s*$", re.MULTILINE)
FORBIDDEN_PATTERNS = {
    "remote shell": re.compile(r"(?<![A-Za-z0-9_-])(ssh|scp|sftp)(?![A-Za-z0-9_-])", re.IGNORECASE),
    "remote file synchronization": re.compile(r"\brsync\b", re.IGNORECASE),
    "configuration management": re.compile(r"\b(ansible|ansible-playbook|salt-call|puppet)\b", re.IGNORECASE),
    "cluster mutation": re.compile(r"\b(kubectl|helm)\b", re.IGNORECASE),
    "host service mutation": re.compile(r"\b(systemctl|service|sudo)\b", re.IGNORECASE),
    "infrastructure mutation": re.compile(r"\bterraform\s+(apply|destroy|import|taint)\b", re.IGNORECASE),
    "container host mutation": re.compile(
        r"\bdocker\s+(context|swarm)\b|\bdocker\s+compose\s+(up|down|pull|restart|stop|rm|create|start)\b",
        re.IGNORECASE,
    ),
    "arbitrary network client": re.compile(r"\b(curl|wget|netcat|nc|telnet)\b", re.IGNORECASE),
    "GitHub mutation": re.compile(r"\bgh\s+(api|workflow|run|secret|variable|release)\b", re.IGNORECASE),
}


class WorkflowSafetyError(ValueError):
    pass


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise WorkflowSafetyError(f"unable to read workflow {path}: {exc}") from exc


def _validate_common(path: Path, text: str) -> dict[str, Any]:
    if "secrets." in text:
        raise WorkflowSafetyError(f"{path} must not access GitHub secrets")
    if WRITE_PERMISSION_RE.search(text):
        raise WorkflowSafetyError(f"{path} requests a write permission")
    if re.search(r"runs-on:\s*(?:\[[^\]]*)?self-hosted", text, re.IGNORECASE):
        raise WorkflowSafetyError(f"{path} must not run on a self-hosted runner")
    if JOB_ENVIRONMENT_RE.search(text):
        raise WorkflowSafetyError(f"{path} must not bind a GitHub deployment environment")
    for label, pattern in FORBIDDEN_PATTERNS.items():
        match = pattern.search(text)
        if match:
            raise WorkflowSafetyError(
                f"{path} contains forbidden {label} token {match.group(0)!r}"
            )

    actions: list[dict[str, str]] = []
    for match in USES_RE.finditer(text):
        action, revision = match.groups()
        if action.startswith("./"):
            raise WorkflowSafetyError(f"{path} must not invoke unreviewed local composite actions")
        if not SHA_RE.fullmatch(revision):
            raise WorkflowSafetyError(
                f"{path} action {action} is not pinned to a 40-character commit SHA"
            )
        actions.append({"action": action, "sha": revision})
    if not actions:
        raise WorkflowSafetyError(f"{path} must contain at least one SHA-pinned action")
    checkout_count = sum(item["action"] == "actions/checkout" for item in actions)
    if checkout_count == 0:
        raise WorkflowSafetyError(f"{path} must use the reviewed checkout action")
    persisted_disabled_count = len(
        re.findall(r"persist-credentials:\s*false", text, re.IGNORECASE)
    )
    if persisted_disabled_count < checkout_count:
        raise WorkflowSafetyError(
            f"{path} must disable persisted credentials for every checkout"
        )
    if not re.search(r"permissions:\s*\n\s+contents:\s*read", text, re.IGNORECASE):
        raise WorkflowSafetyError(f"{path} must declare contents: read permissions")
    return {"path": str(path), "actions": actions}


def validate_workflows(preflight_path: Path, scaffold_ci_path: Path) -> dict[str, Any]:
    preflight_text = _read(preflight_path)
    scaffold_text = _read(scaffold_ci_path)
    preflight = _validate_common(preflight_path, preflight_text)
    scaffold = _validate_common(scaffold_ci_path, scaffold_text)

    if "workflow_dispatch:" not in preflight_text:
        raise WorkflowSafetyError("deployment preflight must be manually dispatched")
    if re.search(r"^\s*(push|pull_request):", preflight_text, re.MULTILINE):
        raise WorkflowSafetyError("deployment preflight must not run automatically")
    if "deployment-preflight-evidence" not in preflight_text:
        raise WorkflowSafetyError("deployment preflight must upload a non-secret evidence artifact")
    if "--allow-unverified" in preflight_text:
        raise WorkflowSafetyError("manual deployment preflight must require verified runtime paths")
    if "runtime_manifest_sha:" not in preflight_text:
        raise WorkflowSafetyError(
            "deployment preflight must pin the separately reviewed manifest commit"
        )
    if "path: runtime-manifest-repo" not in preflight_text:
        raise WorkflowSafetyError(
            "deployment preflight must check out runtime evidence separately from application source"
        )
    if '--manifest "runtime-manifest-repo/' not in preflight_text:
        raise WorkflowSafetyError(
            "deployment preflight must validate the separately checked-out manifest"
        )

    if preflight_text.count("merge-base --is-ancestor") < 3:
        raise WorkflowSafetyError(
            "deployment preflight must prove source and manifest are on ordered main lineage"
        )
    if "deploy/runtime/*.json|docs/deployment/evidence/*" not in preflight_text:
        raise WorkflowSafetyError(
            "deployment preflight must restrict manifest commits to runtime evidence files"
        )

    if "pull_request:" not in scaffold_text:
        raise WorkflowSafetyError("scaffold CI must run for pull requests")
    if "--allow-unverified" not in scaffold_text:
        raise WorkflowSafetyError("scaffold CI must validate the unverified example explicitly")

    return {
        "preflight": preflight,
        "scaffold_ci": scaffold,
        "host_contacted": False,
        "deployment_performed": False,
    }


def _write_evidence(path: Path, result: dict[str, Any] | None, error: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "validation": result,
                "error": error,
                "host_contacted": False,
                "deployment_performed": False,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prove that deployment scaffolding cannot contact or mutate a live host.")
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--scaffold-ci", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result: dict[str, Any] | None = None
    error: str | None = None
    try:
        result = validate_workflows(args.preflight, args.scaffold_ci)
    except WorkflowSafetyError as exc:
        error = str(exc)
    if args.evidence:
        _write_evidence(args.evidence, result, error)
    if error:
        print(f"WORKFLOW_SAFETY=BLOCKED: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
