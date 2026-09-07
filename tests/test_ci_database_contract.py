from __future__ import annotations

import runpy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
HELPER = runpy.run_path(str(ROOT / "scripts/ci/prepare_contract_database.py"))
URL = HELPER["TEST_DATABASE_URL"]
BASE = {
    "GITHUB_ACTIONS": "true", "ENVIRONMENT": "test",
    "DATABASE_URL": URL, "INGRESS_DATABASE_URL": URL, "WORKER_DATABASE_URL": URL,
}


def test_accepts_only_explicit_disposable_test_configuration():
    HELPER["require_contract_environment"](BASE)


@pytest.mark.parametrize("field,value", [
    ("GITHUB_ACTIONS", "false"), ("ENVIRONMENT", "production"),
    ("DATABASE_URL", ""), ("DATABASE_URL", URL.replace("localhost", "database.internal")),
    ("INGRESS_DATABASE_URL", ""), ("WORKER_DATABASE_URL", ""),
])
def test_refuses_real_or_incomplete_database_configuration(field, value):
    with pytest.raises(ValueError):
        HELPER["require_contract_environment"]({**BASE, field: value})


@pytest.mark.parametrize("workflow,job_name", [
    ("required-ci.yml", "validate"), ("continuous-delivery.yml", "deliver"),
])
def test_generic_validation_has_a_real_migrated_postgres(workflow, job_name):
    doc = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
    job = doc["jobs"][job_name]
    image = job["services"]["postgres"]["image"]
    assert image.startswith("postgis/postgis:17-3.5@sha256:")
    assert len(image.rsplit("sha256:", 1)[1]) == 64
    assert job["env"]["ENVIRONMENT"] == "test"
    for key in ("DATABASE_URL", "INGRESS_DATABASE_URL", "WORKER_DATABASE_URL"):
        assert job["env"][key] == URL
    runs = [step.get("run", "") for step in job["steps"]]
    prepare = next(i for i, text in enumerate(runs) if "prepare_contract_database.py" in text)
    validate = next(i for i, text in enumerate(runs) if "validate_repository.py" in text)
    assert prepare < validate
    assert all("continue-on-error" not in step for step in job["steps"])
