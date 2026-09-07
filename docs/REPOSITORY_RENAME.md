# Issue 4: source identity migration and owner rename

## Current scope

Canonical source: `appolon1908-hue/freight-platform-backend`.
Stable repository ID: `1343761943`; owner ID: `275410064`.

Baseline development: `67156f9296dd48cc9f31fce7562fab1ae60c6fd5`, tree
`c0604758df398981201dfcfcd3063a3994d82028`. Its delivery archive was verified
against GitHub artifact digest
`sha256:ed790fd1f889685d9680622aaf5708e63d88171ae0e504393ef80ff89f1b44eb`
and the embedded source archive checksum before editing.

GitHub still reported `appolon1908-hue/transportation-backend-` during preparation.
The canonical-name lookup returned 404, which is not proof that a hidden target
cannot exist. The owner tool refuses any existing target and never deletes,
recreates, transfers or overwrites a repository to make a name available.
The connected GitHub toolset supports source writes but not repository rename.
No administrative rename or server configuration change is claimed by this patch.

## Implemented source changes

The runtime-manifest validator, manifest example, Orbit adoption schema/manifest,
release Dockerfile label, release workflows and current authority documents use
the canonical identity. Contract discovery exposes source ownership and the
stable ID. Read-only required CI validates these declarations. Both publishing
paths require the canonical GitHub name and immutable owner/repository IDs.
A pre-rename validation may succeed; publication may not. Delivery evidence
records the actual checkout name and a separate canonical-identity assessment.
Historical migrations, deployment evidence, commit hashes and service URLs are
unchanged. This patch does not enable freight operations or browser sessions.

## Owner execution (not run by CI)

From a reviewed checkout containing this patch, inspect without writing:

```sh
python scripts/repository/rename_repository.py --evidence /tmp/freight-rename-before.json
```

Review external trust and integrations below first. The owner may rename through
GitHub Settings > General > Repository name, or use the tool with a separately
authorized token supplied through `GH_TOKEN`/`GITHUB_TOKEN` by their secure shell.
Do not paste tokens into chat, command arguments, evidence or Git.

```sh
python scripts/repository/rename_repository.py \
  --apply \
  --confirm appolon1908-hue/freight-platform-backend \
  --acknowledge-trust-review \
  --evidence /tmp/freight-rename-after.json
```

The tool PATCHes only the name, then verifies immutable identity, clone URL,
default branch, all branch heads/protected flags and resolution through the old
alias. A concurrent branch change fails verification and requires review; there
is no automatic rollback, reset, force-push or branch-policy edit. A failure after
the PATCH may mean the rename happened: inspect again before retrying.

To update a known server/local checkout, add `--update-origin /exact/repository/root`
to the apply command; repeat for explicitly selected checkouts. It verifies the
physical rename first, refuses unrelated/credential-bearing/multiple origins,
preserves HTTPS versus SSH, updates an explicit push URL when present and records
sanitized `git remote -v` origin readback. It never discovers servers, scans for
credentials, fetches, checks out files, restarts services or deploys. Successful
local remote readback does not prove every server has been updated.

## External integrations and identity review

GitHub redirects issue and Git traffic, but does NOT redirect action/reusable
workflow calls. Before the rename, identify all callers, including non-default
branches, workflow variables and external build systems. Update those callers in
coordinated reviewed changes. Do not alter historical signed evidence.

A default-branch search on 2026-09-07 found old-name consumers in:

| Repository | Current consumer path | Required treatment |
|---|---|---|
| `Middleware-` | `config/portfolio-repositories.v1.json` | Coordinate catalog key transition; keep business/service identity and permissions unchanged. |
| `Codestra-Grafana-` | `codestra/business-registry.json` | Update repository coordinate, not runtime service labels or scrape destinations. |
| `Codestra-Prometheus` | `codestra/catalog/services.yml` | Update repository coordinate without activating a pending scrape target. |
| `SDK-repository` | `contracts/communications-production-readiness.v1.json` and related documentation | Update source reference while retaining activation/certification gates. |
| `documentaions` | `REPOSITORY_CATALOG.md` and dated rollout/continuation records | Update the live catalog; preserve dated historical records. |

These consumer files were discovered, not modified or certified. Default-branch
search is not an exhaustive audit of branches, hidden repositories or external
systems. Do not claim account-wide convergence before those updates are reviewed.

Review the repository OIDC customization setting and provider trust policies,
including OpenBao/cloud trust, package provenance and any workflow-reference
allowlists. GitHub's current OIDC reference documents immutable subject formats
for newly created/renamed repositories after July 15, 2026. Do not assume the
legacy name-only subject or switch to a wildcard to make authentication pass.
Inspect actual provider claims securely; retain only non-secret claim evidence,
never the token itself. Repository/owner IDs stay part of the identity check.
For the documented default immutable format, the expected production-release
context would be:

```text
repo:appolon1908-hue@275410064/freight-platform-backend@1343761943:environment:production-release
```

That is a documentation-derived expectation, not an observed token. Custom
subject templates may differ. Preserve restrictive branch/environment/issuer
conditions. The application Keycloak issuer is a different trust boundary and is
not changed by the source-repository rename.

Read back GitHub rulesets/branch protection, environment reviewers/deployment
branch policies, Actions permissions, GitHub App installation coverage, webhook
activity and deploy-key access. Do not loosen protections or copy credentials.
Absence of a CODEOWNERS file is not proof of code-owner review coverage.

## Closure criteria

Keep issue #4 open until all of the following have evidence: physical canonical
name with the same repository ID; old alias resolution; unchanged default/branch
history; reviewed protection/environment settings; green checks after rename;
canonical image/provenance identity when publication is separately authorized;
consumer catalog/workflow updates; working Apps/hooks/deploy keys; reviewed
actual OIDC trust; explicit server origin readbacks. The tool deliberately keeps
external trust, app/hook and all-server verification flags false: those require
separate evidence. A code merge, acknowledgment flag or successful name PATCH is
not full issue completion.

## Primary references

- GitHub repository rename and redirect behavior: https://docs.github.com/en/repositories/creating-and-managing-repositories/renaming-a-repository
- Actions/reusable-workflow redirect limitation: https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations
- OIDC subject formats and immutable IDs: https://docs.github.com/en/actions/reference/security/oidc
