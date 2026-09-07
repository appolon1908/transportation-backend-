from __future__ import annotations

from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from pydantic import ValidationError

from app.platform.orbit_contract import AdoptionManifest
from app.repository_identity import (
    IMAGE, OWNER_ID, REPOSITORY, REPOSITORY_ID, repository_evidence, require_canonical_checkout,
)

ROOT = Path(__file__).resolve().parents[1]


def load(relative):
    spec = spec_from_file_location(Path(relative).stem, ROOT / relative)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = load("scripts/ci/validate_repository_identity.py")
rename = load("scripts/repository/rename_repository.py")
CANONICAL_ENV = {
    "GITHUB_REPOSITORY": REPOSITORY,
    "GITHUB_REPOSITORY_ID": REPOSITORY_ID,
    "GITHUB_REPOSITORY_OWNER_ID": OWNER_ID,
}


def test_canonical_coordinates_and_immutable_ids_authorize_name_gate_only():
    result = require_canonical_checkout(CANONICAL_ENV)
    assert result["canonical_image"] == IMAGE
    assert result["canonical_checkout_verified"] is True
    assert "runtime_deployment_authorized" not in result


@pytest.mark.parametrize("key,value", [
    ("GITHUB_REPOSITORY", rename.LEGACY), ("GITHUB_REPOSITORY", "attacker/freight-platform-backend"),
    ("GITHUB_REPOSITORY_ID", "other"), ("GITHUB_REPOSITORY_OWNER_ID", "other"),
    ("GITHUB_REPOSITORY", ""), ("GITHUB_REPOSITORY_ID", ""), ("GITHUB_REPOSITORY_OWNER_ID", ""),
])
def test_publication_rejects_old_names_forks_wrong_ids_and_missing_claims(key, value):
    env = {**CANONICAL_ENV, key: value}
    evidence = repository_evidence(env)
    assert evidence["observed_repository"] == env["GITHUB_REPOSITORY"]
    assert evidence["canonical_checkout_verified"] is False
    with pytest.raises(ValueError, match="immutable_ids"):
        require_canonical_checkout(env)


def test_static_repository_contracts_are_consistent():
    assert checker.validate(ROOT)["static_identity_validation"] == "passed"
    manifest = json.loads((ROOT / "orbit/adoption-manifest.json").read_text())
    assert AdoptionManifest.model_validate(manifest).repository == REPOSITORY
    manifest["repository"] = rename.LEGACY
    with pytest.raises(ValidationError):
        AdoptionManifest.model_validate(manifest)


def test_runtime_manifest_rejects_legacy_and_unknown_repository():
    module = load("scripts/deployment/validate_runtime_manifest.py")
    source = json.loads((ROOT / "deploy/runtime/runtime-paths.example.json").read_text())
    kwargs = dict(expected_environment="staging", expected_source_sha=None, allow_unverified=True, max_verification_age_hours=168)
    module.validate_manifest(source, **kwargs)
    for name in (rename.LEGACY, "attacker/freight-platform-backend"):
        payload = deepcopy(source)
        payload["source"]["repository"] = name
        with pytest.raises(ValueError, match="source.repository"):
            module.validate_manifest(payload, **kwargs)


def test_static_checker_rejects_new_legacy_workflow_reference(tmp_path):
    for relative in ("orbit/adoption-manifest.json", "deploy/runtime/runtime-paths.example.json", "pyproject.toml", "deploy/backend/Dockerfile.v4", ".github/workflows/backend-release.yml"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, path)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    bad = tmp_path / ".github/workflows/consumer.yml"
    bad.write_text(f"uses: {rename.LEGACY}/.github/workflows/test.yml@main\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    with pytest.raises(ValueError, match="legacy_repository_in_active_source"):
        checker.validate(tmp_path)


def test_both_publication_workflows_enforce_renamed_repository():
    release = (ROOT / ".github/workflows/backend-release.yml").read_text()
    assert release.index('test "$GITHUB_REPOSITORY_ID"') < release.index("Authenticate to GHCR")
    assert release.index('test "$GITHUB_REPOSITORY_OWNER_ID"') < release.index("Authenticate to GHCR")
    assert f'test "$GITHUB_REPOSITORY" = "{REPOSITORY}"' in release
    delivery = (ROOT / ".github/workflows/continuous-delivery.yml").read_text()
    assert delivery.count("require_canonical_checkout(os.environ)") == 2
    assert f'image="{IMAGE}:' in delivery
    assert 'raw_name=' not in delivery
    assert "'repository': os.environ['GITHUB_REPOSITORY']" in delivery
    for text in (release, delivery):
        assert rename.LEGACY not in text


class FakeAPI:
    def __init__(self, name=rename.LEGACY, target_exists=False):
        self.name = name
        self.target_exists = target_exists
        self.writes = []
        self.repo_id = int(REPOSITORY_ID)
        self.owner_id = int(OWNER_ID)
        self.branch_change = False

    def request(self, path, *, method="GET", payload=None):
        if method == "PATCH":
            self.writes.append((path, payload))
            self.name = REPOSITORY
            return self.metadata()
        assert method == "GET"
        if "/branches?" in path:
            return [{"name": "development", "commit": {"sha": "b" * 40 if self.branch_change and self.writes else "a" * 40}, "protected": True},
                    {"name": "main", "commit": {"sha": "c" * 40}, "protected": True}]
        if path == f"/repos/{REPOSITORY}" and self.name != REPOSITORY:
            if not self.target_exists:
                raise rename.ApiError(404)
            return {"id": "a-different-repo"}
        return self.metadata()

    def metadata(self):
        return {"id": self.repo_id, "owner": {"id": self.owner_id}, "full_name": self.name,
                "default_branch": "main", "archived": False, "clone_url": f"https://github.com/{self.name}.git"}


def test_inspection_is_read_only_and_keeps_unverified_gates_explicit():
    api = FakeAPI()
    result = rename.inspect(api)
    assert not result["physical_rename_verified"]
    assert not result["external_oidc_trust_verified"]
    assert not result["github_apps_and_hooks_verified"]
    assert api.writes == []


@pytest.mark.parametrize("confirmation,reviewed", [("wrong", True), (REPOSITORY, False)])
def test_rename_requires_explicit_scope_and_trust_review(confirmation, reviewed):
    api = FakeAPI()
    with pytest.raises(ValueError, match="confirmation"):
        rename.apply_rename(api, confirmation=confirmation, trust_reviewed=reviewed)
    assert api.writes == []


def test_rename_changes_only_name_and_preserves_branches_and_repository_id():
    api = FakeAPI()
    result = rename.apply_rename(api, confirmation=REPOSITORY, trust_reviewed=True)
    assert api.writes == [(f"/repos/{rename.LEGACY}", {"name": "freight-platform-backend"})]
    assert result["physical_rename_verified"] and result["branch_inventory_preserved"]
    assert result["legacy_alias_verified"]
    assert result["external_oidc_trust_verified"] is False


def test_rename_is_idempotent_for_same_canonical_repository():
    api = FakeAPI(REPOSITORY)
    result = rename.apply_rename(api, confirmation=REPOSITORY, trust_reviewed=True)
    assert result["rename_performed"] is False
    assert api.writes == []


def test_target_collision_never_displaces_another_repository():
    api = FakeAPI(target_exists=True)
    with pytest.raises(ValueError, match="already_exists"):
        rename.apply_rename(api, confirmation=REPOSITORY, trust_reviewed=True)
    assert api.writes == []


@pytest.mark.parametrize("field", ["repo_id", "owner_id"])
def test_wrong_immutable_identity_prevents_rename(field):
    api = FakeAPI()
    setattr(api, field, 999)
    with pytest.raises(ValueError, match="immutable_identity_mismatch"):
        rename.apply_rename(api, confirmation=REPOSITORY, trust_reviewed=True)
    assert api.writes == []


def test_concurrent_branch_movement_requires_review_not_reset():
    api = FakeAPI()
    api.branch_change = True
    with pytest.raises(ValueError, match="branch_inventory_changed"):
        rename.apply_rename(api, confirmation=REPOSITORY, trust_reviewed=True)
    assert len(api.writes) == 1


def test_token_control_characters_are_rejected_without_echoing_token():
    with pytest.raises(ValueError, match="^invalid_token_format$"):
        rename.GitHubAPI("secret\ninjected-header")


def test_credentials_never_follow_cross_host_redirect(monkeypatch):
    from urllib.error import HTTPError
    api = rename.GitHubAPI("synthetic-test-token")
    calls = []

    def redirect(request, **kwargs):
        calls.append(request.full_url)
        raise HTTPError(request.full_url, 301, "moved", {"Location": "https://attacker.invalid/steal"}, None)

    monkeypatch.setattr(api.opener, "open", redirect)
    with pytest.raises(ValueError, match="unsafe_repository_redirect"):
        api.request(f"/repos/{rename.LEGACY}")
    assert calls == [f"https://api.github.com/repos/{rename.LEGACY}"]


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


@pytest.mark.parametrize("ssh", [False, True])
def test_explicit_origin_update_preserves_transport_and_working_files(tmp_path, ssh):
    git(tmp_path, "init", "-q")
    old = f"git@github.com:{rename.LEGACY}.git" if ssh else f"https://github.com/{rename.LEGACY}.git"
    git(tmp_path, "remote", "add", "origin", old)
    (tmp_path / "work.txt").write_text("unchanged\n")
    with pytest.raises(ValueError, match="verify_physical_rename"):
        rename.update_origin(tmp_path, canonical_verified=False)
    assert git(tmp_path, "remote", "get-url", "origin") == old
    result = rename.update_origin(tmp_path, canonical_verified=True)
    expected = f"git@github.com:{REPOSITORY}.git" if ssh else f"https://github.com/{REPOSITORY}.git"
    assert git(tmp_path, "remote", "get-url", "origin") == expected
    assert expected in result["remote_readback"]
    assert (tmp_path / "work.txt").read_text() == "unchanged\n"


def test_origin_update_refuses_unrelated_repository_and_credentials(tmp_path):
    git(tmp_path, "init", "-q")
    for url in ("https://github.com/other/repo.git", "https://secret@github.com/other/repo.git"):
        subprocess.run(["git", "-C", str(tmp_path), "remote", "remove", "origin"], capture_output=True)
        git(tmp_path, "remote", "add", "origin", url)
        with pytest.raises(ValueError, match="unexpected_origin_configuration"):
            rename.update_origin(tmp_path, canonical_verified=True)
        assert git(tmp_path, "remote", "get-url", "origin") == url
