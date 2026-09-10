# CI/CD authority

## Repository

- Repository: `appolon1908-hue/freight-platform-backend`
- Class: `backend`
- Purpose: freight-platform backend
- Current default branch: governance/documentation only
- Integrated application authority: `development` (PRs #14, #2, #11 and #15 accepted).
- Physical rename and external trust acceptance: issue #4 / `docs/REPOSITORY_RENAME.md`.

## Persistent branches

```text
development
test
staging
production
main
```

Promotion order:

```text
feature/fix -> development -> test -> staging -> production -> main
```

The initial bootstrap places the CI/CD policy on every persistent branch. That does not promote the backend or authorize deployment.

## Required CI

`.github/workflows/required-ci.yml` runs on every push, pull request, and manual dispatch. It proves exact source identity, runs checksum-verified secret scanning, validates repository data and documentation, installs Python dependencies in an isolated virtual environment, compiles Python, runs tests, checks installed dependency consistency, validates Compose, builds Dockerfiles, and publishes sanitized evidence.

Implementation changes target `development` and are validated against their exact source and proposed merge result.

## Every-branch audit

`.github/workflows/all-branches-audit.yml` runs daily and manually. It validates every current branch tip in an isolated worktree without changing branch history.

## Continuous delivery

`.github/workflows/continuous-delivery.yml` runs only on the persistent branch train. It creates deterministic source/build bundles, records exact SHA/tree evidence and SHA-256 checksums, and may publish an immutable GHCR image from `staging`, `production`, or `main` when reproducible build inputs are present.

Runtime deployment, database migrations, provider calls, and external effects remain unauthorized. A separate protected-environment deployment must prove backup/restore, migration safety, health/readiness/version readback, monitoring, exact digest, and rollback.

## Current source gate

The buildable backend is integrated into `development`. Promotion through `test`, `staging`, `production`, and `main` remains separately gated. Canonical publication additionally requires the observed GitHub repository name, repository ID and owner ID to match the source identity contract. Validation can run before the administrative rename, but cannot publish from the legacy location.

## Required GitHub settings

Protect all five persistent branches or apply equivalent rulesets. Require `required-ci`, approving review, resolved conversations, linear history, no force pushes, no deletion, and up-to-date protected promotions.
