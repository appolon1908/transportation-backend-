# Secure CI and Deployment Scaffolding Review

**Branch:** `ops/secure-ci-deployment-scaffold-v1`  
**Direct dependency:** `be/api-contract-readiness-v1`  
**Initial dependency SHA:** `cdbb6223c33176e790433fb43ed8cc8a8af1810b`  
**Change posture:** validation and evidence only; no server contact or deployment

## Scope

This change prepares a fail-closed CI, image-release, Compose, gateway, and deployment-preflight boundary. It deliberately does **not** connect to a staging or production host. It does not use SSH, a self-hosted runner, GitHub deployment secrets, a deployment environment, or any host-mutation command.

The live server must remain unchanged until its actual host identity, filesystem paths, Compose project directory, secret locations, networks, backup destination, and immutable image digests are captured through an approved **read-only** inventory and committed as separately reviewed evidence.

## Controls added

### CI supply chain

- GitHub Actions used by backend CI, integration CI, compliance/gateway CI, release publication, and deployment preflight are pinned to complete commit SHAs.
- Checkout credentials are not persisted.
- CI database and gateway validation containers are selected by digest.
- The release workflow requires a digest-pinned `python:3.12-slim-bookworm` base image instead of accepting a mutable Dockerfile default.
- The release workflow derives build timestamps from the source commit, publishes only from protected `main` or a signed release tag, scans the exact published digest, signs it, attaches provenance and an SBOM, and stops before deployment.
- Release evidence always records `runtime_paths_verified=false`, `host_contacted=false`, `deployment_performed=false`, and `external_capabilities_enabled=false`.

### Runtime and Compose model

- Deployment hosts may pull only digest-pinned images; Compose builds are rejected.
- Containers are non-privileged, use read-only root filesystems, set `no-new-privileges`, drop all Linux capabilities, have bounded PID limits, and define health checks for long-running services.
- Only Caddy publishes host ports: TCP 80, TCP 443, and UDP 443.
- Docker/container-runtime sockets, host network/PID/IPC namespaces, and unapproved bind mounts are rejected.
- External Docker network names must be supplied from the verified runtime manifest instead of being inferred.
- The migrator, API, webhook-ingress, and worker DSNs must have distinct database usernames. API, ingress, and worker identities are documented as non-superuser `NOBYPASSRLS` roles. The migrator is short-lived.
- All live external effects and external portal access are explicitly disabled in the Compose environment.

### Gateway contract

- Caddy trusted-proxy CIDRs are required runtime inputs rather than hardcoded assumptions.
- The special webhook body-size and rate-limit boundary now protects the implemented signed-ingress prefix, `/api/v1/integrations/{webhook_slug}/webhooks/{provider}`.
- Kong strips caller-supplied identity and gateway-proof headers before adding its own gateway proof.
- Kong Admin API remains disabled and no Kong, Redis, or backend administrative port is published publicly.

### Read-only runtime-path preflight

`deployment-preflight-read-only` is manual-only and supports `staging` only. It requires:

1. an exact reviewed application source SHA;
2. an exact, separately reviewed runtime-manifest commit SHA;
3. a manifest path below `deploy/runtime/`;
4. a manifest whose embedded application source SHA matches the requested application SHA;
5. runtime evidence no older than 168 hours;
6. a SHA-256 reference to immutable read-only inventory evidence;
7. all external capabilities disabled and `deployment_authorized=false`;
8. both commits on ordered `main` lineage;
9. no changes between the application SHA and manifest SHA except `deploy/runtime/*.json` and `docs/deployment/evidence/*`.

The application checkout and runtime-evidence checkout are separate. This avoids an impossible self-referential manifest in which a file would need to contain the SHA of the commit that contains the file, while the evidence-only diff rule prevents application drift between the two reviewed SHAs.

The workflow renders Compose with isolated `.invalid` endpoints and non-secret placeholder values, validates the model, validates the manifest, proves that both preflight workflows contain no live-host operations, uploads non-secret evidence, and fails closed unless every validation succeeds.

A successful preflight means only:

```text
SOURCE_CONTRACT_VALIDATED=YES
RUNTIME_MANIFEST_STRUCTURALLY_VERIFIED=YES
COMPOSE_SECURITY_MODEL=PASS
HOST_CONTACTED=NO
DEPLOYMENT_PERFORMED=NO
```

It is not deployment approval.

## Required read-only inventory

An approved operator must identify the intended **staging** host without changing it. The evidence must record, without exposing secret contents:

```text
host identity and ownership
application_root
compose_project_dir
secrets_root
evidence_root
backup_root
backend Compose path
gateway Compose path
Caddyfile path
Kong template path
backend external network name
gateway external network name
filesystem owner/group/mode for each path
Docker and Compose versions
currently running project names and immutable image IDs/digests
bound ports and reverse-proxy ownership
backup target and last successful restore evidence reference
```

Permitted inventory is read-only. Do not run package installation, file creation, permission changes, service reloads, container pulls, Compose mutation, migrations, firewall changes, DNS changes, secret reads, or deployment commands while producing this evidence.

The inventory output must be redacted, stored immutably, hashed with SHA-256, and referenced from a `verified-read-only` runtime manifest. The manifest belongs in a separately reviewed commit so its `source.sha` can identify the exact application source without a circular commit hash.

## Review findings corrected

1. The existing release and CI workflows used movable action tags. The scaffold pins actions to exact commits.
2. The backend Dockerfile had a mutable Python base-image fallback. The release now requires a digest-pinned base input.
3. CI service and gateway-validator images were tag-selected. The reviewed scaffold pins those validation images by digest.
4. The backend Compose model coupled the migrator to the API DSN and described the worker as `BYPASSRLS`, contradicting the database and CI contract. The model now uses four distinct named identities and documents API/ingress/worker as `NOBYPASSRLS`.
5. The environment example omitted the ingress DSN. It now documents migrator, API, ingress, and worker DSNs separately.
6. Caddy and Kong protected a legacy webhook path. They now match the implemented signed integration-ingress prefix.
7. Trusted proxy CIDRs and external Docker network names were assumed. They are now required verified inputs.
8. A runtime manifest embedded in the application source would create a self-SHA problem. The preflight now pins and checks out the runtime-evidence commit separately.
9. There was no machine-enforced proof that a deployment workflow could not contact a host. The new workflow-safety validator rejects secrets, write permissions, self-hosted runners, deployment environments, mutable action refs, remote clients, infrastructure mutation, service mutation, and container-host mutation.

## Remaining production blockers

The scaffold is intentionally **not** a production approval. These blockers remain:

- Runtime host and path values are still `UNVERIFIED`; no approved host was contacted.
- Branch protection, required independent review, and exact required checks are not yet enforced on the current dependency/release branches.
- Python build dependencies use compatible version ranges rather than a hash-locked, reproducible dependency set. An immutable requirements lock with hashes must be generated and reviewed before the final release image is trusted.
- The current application bootstrap constructs API, ingress, and worker database engines in a shared process model. Compose uses distinct credentials, but API and worker containers still receive multiple runtime DSNs. Process-level credential minimization requires an application-bootstrap refactor on its own backend feature branch.
- The dedicated `freight_migrator` role, grants, credential lifetime, and secret-delivery mechanism have not been verified against a staging database.
- The gateway renderer image has not been built, scanned, signed, attested, or published by approved digest.
- Actual backend, Redis, Kong, Caddy, and gateway-renderer runtime digests have not been approved in a runtime manifest.
- Secret-file owners, modes, mount behavior, rotation, and rollback have not been inspected.
- Backup creation, restore, migration rollback, gateway reload, staging soak, and rollback evidence do not yet exist for the target environment.
- No staging deployment, live API smoke test, Odoo delivery, n8n delivery, public portal activation, or production canary has been performed.

## Branch ownership

This branch owns only CI/deployment safety controls:

```text
ops/secure-ci-deployment-scaffold-v1
  action and validation-image pinning
  immutable release-input controls
  fail-closed runtime manifest and preflight
  Compose/gateway deployment model hardening
  no-live-host workflow proof and evidence
```

Application credential-bootstrap refactoring, dependency locking, document storage, observability, and feature implementations belong on separate branches and pull requests.

## Merge and execution order

```text
1. Review this branch against be/api-contract-readiness-v1.
2. Obtain green exact-head CI on an unchanged SHA.
3. Obtain independent approval.
4. Merge through protected controls into its direct dependency.
5. Produce read-only staging inventory evidence without changing the host.
6. Commit the verified runtime manifest separately and review its exact SHA.
7. Run deployment-preflight-read-only with both exact SHAs.
8. Resolve every blocker and obtain explicit staging approval.
9. Only then create a separate deployment change; this scaffold performs no deployment.
```

## Current status

```text
SCAFFOLD_PREPARED=YES
STATIC_REVIEW_COMPLETED=YES
LIVE_SERVER_CONTACTED=NO
LIVE_SERVER_CHANGED=NO
RUNTIME_PATHS_VERIFIED=NO
STAGING_DEPLOYMENT_PERFORMED=NO
PRODUCTION_DEPLOYMENT_PERFORMED=NO
LIVE_CAPABILITIES_ENABLED=NO
GO_NO_GO=NO_GO
```
