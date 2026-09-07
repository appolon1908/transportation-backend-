from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_root_image_starts_the_composed_api_not_the_foundation_app():
    dockerfile = (ROOT / "Dockerfile").read_text()
    entrypoint = (ROOT / "deploy/backend/entrypoint-v4.sh").read_text()
    directives = dict(line.split(" ", 1) for line in dockerfile.splitlines()
                      if line.startswith(("ENTRYPOINT ", "CMD ")))
    assert json.loads(directives["ENTRYPOINT"]) == ["freight-entrypoint"]
    assert json.loads(directives["CMD"]) == ["api"]
    assert "exec uvicorn app.production_v4:app" in entrypoint
    assert "reject_migrator_credential" in entrypoint
    assert '--forwarded-allow-ips "$FORWARDED_ALLOW_IPS"' in entrypoint
    assert "USER freight" in dockerfile
    assert "PORT=8080" in dockerfile


def test_root_image_contains_both_schema_trees_and_workers():
    dockerfile = (ROOT / "Dockerfile").read_text()
    for line in (
        "COPY app ./app", "COPY workers ./workers", "COPY migrations ./migrations",
        "COPY compliance_migrations ./compliance_migrations",
        "COPY alembic.ini alembic-compliance.ini ./",
    ):
        assert line in dockerfile
    assert dockerfile.index("COPY app ./app") < dockerfile.index("pip install .")
    assert "COPY pyproject.toml README.md ./" in dockerfile
