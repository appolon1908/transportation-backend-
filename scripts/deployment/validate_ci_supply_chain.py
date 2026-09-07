from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ACTION_RE = re.compile(r"\buses:\s*([^@\s]+)@([^\s#]+)")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
SERVICE_IMAGE_RE = re.compile(r"^\s{8}image:\s*(\S+)\s*$", re.MULTILINE)
FORBIDDEN_REMOTE_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(ssh|scp|sftp|rsync|ansible-playbook|kubectl|helm)(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
REQUIRED_NODE24_ACTION_SHAS = {
    "actions/checkout": "fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09",
    "actions/setup-python": "ece7cb06caefa5fff74198d8649806c4678c61a1",
    "actions/upload-artifact": "b7c566a772e6b6bfb58ed0dc250532a479d7789f",
}


class SupplyChainError(ValueError):
    pass


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SupplyChainError(f"unable to read {path}: {exc}") from exc


def _actions(path: Path, text: str) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    for match in ACTION_RE.finditer(text):
        action, revision = match.groups()
        if action.startswith("./"):
            raise SupplyChainError(f"{path} invokes an unreviewed local action: {action}")
        if not SHA_RE.fullmatch(revision):
            raise SupplyChainError(
                f"{path} action {action}@{revision} is not pinned to a full commit SHA"
            )
        required_sha = REQUIRED_NODE24_ACTION_SHAS.get(action)
        if required_sha and revision != required_sha:
            raise SupplyChainError(
                f"{path} action {action} must use reviewed Node 24 SHA {required_sha}; got {revision}"
            )
        actions.append({"action": action, "sha": revision})
    if not actions:
        raise SupplyChainError(f"{path} has no external actions to validate")
    return actions


def _docker_run_windows(text: str) -> list[str]:
    windows: list[str] = []
    start = 0
    while True:
        index = text.find("docker run", start)
        if index < 0:
            break
        windows.append(text[index : index + 900])
        start = index + len("docker run")
    return windows


def _validate_workflow(path: Path) -> dict[str, Any]:
    text = _read(path)
    if "pull_request_target:" in text:
        raise SupplyChainError(f"{path} must not use pull_request_target")
    if re.search(r"runs-on:\s*(?:\[[^\]]*)?self-hosted", text, re.IGNORECASE):
        raise SupplyChainError(f"{path} must not run on a self-hosted runner")

    actions = _actions(path, text)
    checkout_actions = [item for item in actions if item["action"] == "actions/checkout"]
    if checkout_actions and "persist-credentials: false" not in text:
        raise SupplyChainError(f"{path} must disable persisted checkout credentials")

    service_images = SERVICE_IMAGE_RE.findall(text)
    for image in service_images:
        if not DIGEST_RE.fullmatch(image):
            raise SupplyChainError(f"{path} CI service image is not digest pinned: {image}")

    for window in _docker_run_windows(text):
        candidates = re.findall(r"[^\s]+@sha256:[0-9a-f]{64}", window)
        if not any(DIGEST_RE.fullmatch(candidate.strip("'\"")) for candidate in candidates):
            raise SupplyChainError(f"{path} contains docker run without a digest-pinned image")

    if path.name == "backend-release.yml":
        if "python_base_image:" not in text:
            raise SupplyChainError("backend release must require a digest-pinned Python base image")
        if "PYTHON_BASE_IMAGE=${{ inputs.python_base_image }}" not in text:
            raise SupplyChainError("backend release must pass the reviewed base image into BuildKit")
        if "'deployment_performed': False" not in text:
            raise SupplyChainError("backend release evidence must state that no deployment occurred")
        match = FORBIDDEN_REMOTE_RE.search(text)
        if match:
            raise SupplyChainError(
                f"backend release contains a remote-deployment token: {match.group(0)!r}"
            )
    else:
        permissions_match = re.search(r"permissions:\s*\n\s+contents:\s+read\s*(?:\n|$)", text)
        if not permissions_match:
            raise SupplyChainError(f"{path} must declare read-only repository permissions")

    return {
        "path": str(path),
        "actions": actions,
        "service_images": service_images,
        "docker_run_count": len(_docker_run_windows(text)),
    }


def _validate_dockerfile(path: Path) -> dict[str, Any]:
    text = _read(path)
    arg_lines = [line.strip() for line in text.splitlines() if line.strip().startswith("ARG PYTHON_BASE_IMAGE")]
    if arg_lines != ["ARG PYTHON_BASE_IMAGE"]:
        raise SupplyChainError(
            "Dockerfile must define PYTHON_BASE_IMAGE exactly once without a mutable default"
        )
    from_lines = [line.strip() for line in text.splitlines() if line.strip().startswith("FROM ")]
    if len(from_lines) != 2 or any(not line.startswith("FROM ${PYTHON_BASE_IMAGE}") for line in from_lines):
        raise SupplyChainError("every Dockerfile stage must use the required digest-pinned base input")
    return {"path": str(path), "base_argument_has_default": False, "stage_count": 2}


def _dependency_warnings(pyproject_path: Path) -> list[str]:
    text = _read(pyproject_path)
    warnings: list[str] = []
    if re.search(r"[\"'][A-Za-z0-9_.-]+(?:\[[^\]]+\])?>=", text):
        warnings.append(
            "Python dependencies use version ranges and are not locked with hashes; immutable release reproducibility remains blocked."
        )
    return warnings


def validate(
    workflow_paths: list[Path], dockerfile: Path, pyproject: Path
) -> dict[str, Any]:
    return {
        "workflows": [_validate_workflow(path) for path in workflow_paths],
        "dockerfile": _validate_dockerfile(dockerfile),
        "warnings": _dependency_warnings(pyproject),
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
    parser = argparse.ArgumentParser(
        description="Validate CI action pins, validation container digests, and release base-image controls."
    )
    parser.add_argument("--workflow", action="append", type=Path, required=True)
    parser.add_argument("--dockerfile", type=Path, required=True)
    parser.add_argument("--pyproject", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result: dict[str, Any] | None = None
    error: str | None = None
    try:
        result = validate(args.workflow, args.dockerfile, args.pyproject)
    except SupplyChainError as exc:
        error = str(exc)
    if args.evidence:
        _write_evidence(args.evidence, result, error)
    if error:
        print(f"CI_SUPPLY_CHAIN=BLOCKED: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
