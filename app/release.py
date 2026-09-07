from __future__ import annotations

import os
from typing import Any

CANONICAL_MIGRATION_HEAD = "0005_portal_workflows"
BACKEND_SERVICE_NAME = "freight-platform-backend"
INTEGRATION_SERVICE_NAME = "freight-platform-integrations"


def release_identity(
    *,
    service_name: str,
    version: str,
    migration_head: str = CANONICAL_MIGRATION_HEAD,
) -> dict[str, Any]:
    """Return the immutable identity advertised by health and release checks."""

    return {
        "name": service_name,
        "version": version,
        "git_sha": os.getenv("GIT_SHA", "unknown"),
        "image_digest": os.getenv("IMAGE_DIGEST", "unknown"),
        "migration_head": migration_head,
    }
