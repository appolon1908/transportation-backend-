"""Canonical source coordinates, separate from observed checkout provenance.

Constants are a target identity, never proof that GitHub has been renamed.
Publication requires both the canonical name and immutable GitHub IDs.
"""
from __future__ import annotations

from collections.abc import Mapping

REPOSITORY = "appolon1908/transportation-backend-"
REPOSITORY_ID = "1343761943"
OWNER_ID = "335843231"
SOURCE_URL = f"https://github.com/{REPOSITORY}"
CLONE_URL = f"{SOURCE_URL}.git"
IMAGE = f"ghcr.io/{REPOSITORY}"


def repository_evidence(environment: Mapping[str, str]) -> dict[str, str | bool]:
    """Report real CI values without substituting a desired name for provenance."""
    observed = environment.get("GITHUB_REPOSITORY", "")
    repository_id = environment.get("GITHUB_REPOSITORY_ID", "")
    owner_id = environment.get("GITHUB_REPOSITORY_OWNER_ID", "")
    verified = (observed, repository_id, owner_id) == (REPOSITORY, REPOSITORY_ID, OWNER_ID)
    return {
        "canonical_repository": REPOSITORY,
        "canonical_image": IMAGE,
        "observed_repository": observed,
        "observed_repository_id": repository_id,
        "observed_owner_id": owner_id,
        "canonical_checkout_verified": verified,
    }


def require_canonical_checkout(environment: Mapping[str, str]) -> dict[str, str | bool]:
    evidence = repository_evidence(environment)
    if not evidence["canonical_checkout_verified"]:
        raise ValueError("publication_requires_canonical_repository_name_and_immutable_ids")
    return evidence
