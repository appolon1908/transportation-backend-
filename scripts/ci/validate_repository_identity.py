"""Offline canonical-name validation; optionally enforce actual CI identity.

Runs without application dependencies, including before package installation.
Historical documents and the explicit rename tool may retain the old alias.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.repository_identity import (  # noqa: E402
    IMAGE, REPOSITORY, SOURCE_URL, repository_evidence, require_canonical_checkout,
)

LEGACY = "transportation-backend-"
ACTIVE_ROOTS = ("app", "deploy", "orbit", ".github/workflows")


def validate(root: Path) -> dict[str, object]:
    errors: list[str] = []
    # Scan tracked source only: bytecode, virtualenvs and build artifacts do not
    # become source authorities. Fail if the Git inventory itself cannot be read.
    paths = subprocess.check_output(
        ["git", "-C", str(root), "ls-files", "-z"], text=True,
    ).split("\0")
    for relative in filter(None, paths):
        path = root / relative
        if not any(relative.startswith(prefix + "/") for prefix in ACTIVE_ROOTS):
            continue
        if path.suffix in {".pyc", ".pyo"}:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if LEGACY in source:
            errors.append(f"legacy_repository_in_active_source:{relative}")
    manifest = json.loads((root / "orbit/adoption-manifest.json").read_text())
    runtime = json.loads((root / "deploy/runtime/runtime-paths.example.json").read_text())
    metadata = tomllib.loads((root / "pyproject.toml").read_text())
    if manifest.get("repository") != REPOSITORY:
        errors.append("orbit_repository_mismatch")
    if runtime.get("source", {}).get("repository") != REPOSITORY:
        errors.append("runtime_repository_mismatch")
    if metadata.get("project", {}).get("name") != REPOSITORY.split("/")[1]:
        errors.append("package_name_mismatch")
    if SOURCE_URL not in (root / "deploy/backend/Dockerfile.v4").read_text():
        errors.append("release_image_source_label_mismatch")
    if IMAGE not in (root / ".github/workflows/backend-release.yml").read_text():
        errors.append("release_image_coordinate_mismatch")
    if errors:
        raise ValueError(";".join(errors))
    return {"canonical_repository": REPOSITORY, "static_identity_validation": "passed"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--require-renamed", action="store_true")
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    result: dict[str, object] = {"identity": repository_evidence(os.environ)}
    try:
        result.update(validate(args.root.resolve()))
        if args.require_renamed:
            require_canonical_checkout(os.environ)
        result["error"] = None
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        result["error"] = str(exc)
    if args.evidence:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, sort_keys=True))
    return 1 if result["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
