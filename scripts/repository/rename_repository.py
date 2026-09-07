"""Owner-run rename/verification tool. Read-only by default; never deploys.

Use a separately authorized GH_TOKEN or GITHUB_TOKEN from the environment. Tokens,
webhook configuration, key contents and provider credentials are never reported.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.repository_identity import (  # noqa: E402
    CLONE_URL, OWNER_ID, REPOSITORY, REPOSITORY_ID,
)

LEGACY = "appolon1908-hue/transportation-backend-"
API_ORIGIN = "https://api.github.com"


class ApiError(RuntimeError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"github_api_status_{status}")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHubAPI:
    def __init__(self, token: str = ""):
        if any(ord(character) < 32 or ord(character) == 127 for character in token):
            raise ValueError("invalid_token_format")
        self.token = token
        self.opener = build_opener(NoRedirect())

    def request(self, path: str, *, method: str = "GET", payload=None, hops: int = 0):
        if not path.startswith("/") or path.startswith("//") or hops > 2:
            raise ValueError("invalid_github_api_path")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None if payload is None else json.dumps(payload).encode()
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            with self.opener.open(Request(API_ORIGIN + path, data=data, headers=headers, method=method), timeout=30) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ValueError("github_response_too_large")
                return json.loads(raw)
        except HTTPError as exc:
            # Follow repository redirects only for GET and only on the same API
            # host. Never forward authorization to an arbitrary Location host.
            if method == "GET" and exc.code in {301, 302, 307, 308}:
                target = urlsplit(exc.headers.get("Location", ""))
                if (target.scheme, target.netloc) == ("https", "api.github.com") and target.path.startswith(("/repositories/", "/repos/")) and not target.fragment:
                    return self.request(target.path + ("?" + target.query if target.query else ""), hops=hops + 1)
                raise ValueError("unsafe_repository_redirect") from None
            raise ApiError(exc.code) from None
        except (URLError, TimeoutError):
            raise RuntimeError("github_connection_failed") from None


def verified_metadata(api, name: str | None = None) -> dict:
    path = f"/repos/{name or LEGACY}"
    repo = api.request(path)
    if str(repo.get("id")) != REPOSITORY_ID or str(repo.get("owner", {}).get("id")) != OWNER_ID:
        raise ValueError("repository_immutable_identity_mismatch")
    if repo.get("full_name") not in {LEGACY, REPOSITORY} or repo.get("archived"):
        raise ValueError("unexpected_or_archived_repository")
    expected_url = f"https://github.com/{repo['full_name']}.git"
    if repo.get("clone_url") != expected_url:
        raise ValueError("repository_clone_url_mismatch")
    return {key: repo[key] for key in ("id", "full_name", "default_branch", "clone_url")}


def branch_snapshot(api, name: str) -> dict:
    refs = {}
    for page in range(1, 21):
        rows = api.request(f"/repos/{name}/branches?per_page=100&page={page}")
        for row in rows:
            refs[row["name"]] = {"sha": row["commit"]["sha"], "protected": row["protected"]}
        if len(rows) < 100:
            return refs
    raise ValueError("branch_inventory_exceeds_limit")


def inspect(api) -> dict:
    repo = verified_metadata(api)
    name = repo["full_name"]
    branches = branch_snapshot(api, name)
    if repo["default_branch"] not in branches or "development" not in branches:
        raise ValueError("incomplete_branch_inventory")
    return {
        "repository": repo,
        "branches": branches,
        "physical_rename_verified": name == REPOSITORY,
        "external_oidc_trust_verified": False,
        "github_apps_and_hooks_verified": False,
        "server_remotes_verified": False,
        "runtime_deployment_authorized": False,
    }


def apply_rename(api, *, confirmation: str, trust_reviewed: bool) -> dict:
    if confirmation != REPOSITORY or trust_reviewed is not True:
        raise ValueError("explicit_repository_confirmation_and_trust_review_required")
    before = inspect(api)
    renamed = False
    if before["repository"]["full_name"] != REPOSITORY:
        try:
            # Never displace, transfer, delete, recreate or merge a target repo.
            api.request(f"/repos/{REPOSITORY}")
        except ApiError as exc:
            if exc.status != 404:
                raise
        else:
            raise ValueError("canonical_repository_name_already_exists")
        api.request(f"/repos/{LEGACY}", method="PATCH", payload={"name": REPOSITORY.split("/")[1]})
        renamed = True
    after = inspect(api)
    if not after["physical_rename_verified"]:
        raise ValueError("rename_not_verified")
    if before["branches"] != after["branches"] or before["repository"]["default_branch"] != after["repository"]["default_branch"]:
        raise ValueError("rename_applied_but_branch_inventory_changed_manual_review_required")
    canonical = verified_metadata(api, REPOSITORY)
    alias = verified_metadata(api, LEGACY)
    if canonical != alias or alias["full_name"] != REPOSITORY:
        raise ValueError("legacy_alias_redirect_not_verified")
    after.update({"rename_performed": renamed, "legacy_alias_verified": True, "branch_inventory_preserved": True})
    # The acknowledgment is operator input, not proof of external trust or apps.
    return after


def update_origin(path: Path, *, canonical_verified: bool) -> dict:
    if canonical_verified is not True:
        raise ValueError("verify_physical_rename_before_remote_update")
    root = path.resolve(strict=True)

    def git(*args, allow_missing=False):
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=15)
        if result.returncode and not (allow_missing and result.returncode == 1):
            raise ValueError("git_remote_operation_failed")
        return result.stdout.strip()

    if Path(git("rev-parse", "--show-toplevel")).resolve() != root:
        raise ValueError("checkout_path_must_be_repository_root")
    accepted = {
        f"https://github.com/{name}{suffix}" for name in (LEGACY, REPOSITORY) for suffix in ("", ".git")
    } | {f"git@github.com:{name}.git" for name in (LEGACY, REPOSITORY)}
    urls = git("remote", "get-url", "--all", "origin").splitlines()
    pushurls = git("config", "--get-all", "remote.origin.pushurl", allow_missing=True).splitlines()
    if len(urls) != 1 or len(pushurls) > 1 or any(url not in accepted for url in urls + pushurls):
        raise ValueError("unexpected_origin_configuration_refusing_to_overwrite")
    canonical = f"git@github.com:{REPOSITORY}.git" if urls[0].startswith("git@") else CLONE_URL
    git("remote", "set-url", "origin", canonical)
    if pushurls:
        push = f"git@github.com:{REPOSITORY}.git" if pushurls[0].startswith("git@") else CLONE_URL
        git("remote", "set-url", "--push", "origin", push)
    if git("remote", "get-url", "origin") != canonical:
        raise ValueError("origin_readback_failed")
    # No fetch, checkout, reset, tag, hook, or deployment is executed.
    return {"checkout": str(root), "remote_readback": "\n".join(
        line for line in git("remote", "-v").splitlines() if line.startswith("origin\t")
    )}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--acknowledge-trust-review", action="store_true")
    parser.add_argument("--update-origin", type=Path, action="append", default=[])
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    result: dict = {"runtime_deployment_authorized": False}
    try:
        api = GitHubAPI(os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN", ""))
        if args.apply:
            if not api.token:
                raise ValueError("owner_authorized_token_required")
            result = apply_rename(api, confirmation=args.confirm, trust_reviewed=args.acknowledge_trust_review)
        else:
            if args.update_origin:
                raise ValueError("remote_mutation_requires_apply")
            result = inspect(api)
            result["rename_performed"] = False
        result["remotes"] = [update_origin(path, canonical_verified=result["physical_rename_verified"]) for path in args.update_origin]
        result["error"] = None
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        result["error"] = str(exc)
        result["manual_review_required"] = True
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, sort_keys=True))
    return 1 if result["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
