"""Prepare only the workflow's disposable loopback contract database.

Not a production migration wrapper. Refuse other environments or endpoints;
the native backend workflow retains its independent role/RLS checks.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping

TEST_DATABASE_URL = "postgresql+asyncpg://freight:freight@localhost:5432/freight"


def require_contract_environment(env: Mapping[str, str]) -> None:
    if env.get("GITHUB_ACTIONS") != "true" or env.get("ENVIRONMENT") != "test":
        raise ValueError("contract_database_requires_test_workflow")
    for name in ("DATABASE_URL", "INGRESS_DATABASE_URL", "WORKER_DATABASE_URL"):
        if env.get(name) != TEST_DATABASE_URL:
            raise ValueError("contract_database_requires_disposable_loopback_urls")


def main() -> None:
    require_contract_environment(os.environ)
    root = Path(__file__).resolve().parents[2]
    subprocess.run([sys.executable, "-m", "app.schema_upgrade"], cwd=root, check=True)
    print("DISPOSABLE_CONTRACT_SCHEMA=READY")


if __name__ == "__main__":
    main()
