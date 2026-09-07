# Carrier compliance, gateway and release runbook

This runbook applies to the stacked backend branch
`be/compliance-gateway-production-v4`. It depends on both
`be/identity-tenancy-rbac-v2` and `be/integrations-webhooks-provenance-v3`.
Nothing in this branch enables a live carrier tender, dispatch, email, SMS,
accounting export, Odoo call, n8n call or public portal by default.

## 1. Production invariants

- Caddy is the only public listener on TCP 80/443 and UDP 443.
- Kong and Redis have no host-published ports. Kong's Admin API is disabled.
- The FastAPI listener is private and accepts non-health traffic only from a
  configured gateway CIDR carrying the shared gateway proof.
- Kong removes caller-supplied tenant, actor, permission and gateway-proof headers
  before adding its own proof. The application continues to derive actor and tenant
  authority from verified OIDC identity and local membership, not from those
  headers.
- Webhook and API request bodies are limited at Caddy, Kong and FastAPI.
- Redis-backed Kong rate limiting is configured fail-closed (`fault_tolerant=false`).
- Raw authority numbers and insurance policy numbers are never stored. They are
  normalized and HMAC-hashed with a production-only pepper.
- Compliance evidence is append-only. Readiness decisions include the active
  policy version, reason codes and an input fingerprint.
- PostgreSQL triggers enforce readiness on carrier tender/assignment and dispatched
  load writes when those tables expose the required fields. API, worker, Odoo,
  n8n and direct service writes share the same database rule.
- Hard safety blocks—carrier not found, missing policy, out-of-service, and
  unsatisfactory safety—cannot be overridden. Other exceptions require
  `compliance.override`, a reason, an approver and a window no longer than 24 hours.
- Deployment images must be referenced by digest. The release workflow publishes
  the canonical `freight-platform-backend` image, SBOM, build provenance and
  signature but deliberately performs no deployment.

## 2. API surface

| Method | Route | Permission / purpose |
|---|---|---|
| GET | `/api/v1/admin/compliance/policies` | `compliance.manage`; policy history |
| POST | `/api/v1/admin/compliance/policies` | `compliance.manage`; append a policy version |
| GET | `/api/v1/carriers/{carrier_id}/compliance` | `operations.read`; evidence and overrides |
| POST | `/api/v1/carriers/{carrier_id}/compliance/authority` | `compliance.manage`; hashed authority evidence |
| POST | `/api/v1/carriers/{carrier_id}/compliance/insurance` | `compliance.manage`; hashed insurance evidence |
| POST | `/api/v1/carriers/{carrier_id}/compliance/safety` | `compliance.manage`; safety evidence |
| POST | `/api/v1/carriers/{carrier_id}/compliance/overrides` | `compliance.override`; time-bounded exception |
| POST | `/api/v1/carriers/{carrier_id}/readiness/evaluate` | `operations.read`; recorded decision |
| GET | `/api/v1/carriers/{carrier_id}/readiness/history` | `operations.read`; decision history |

Every material POST uses the platform's existing command executor and therefore
requires `Idempotency-Key`. A replay with the same key and same body returns the
recorded result. Reuse with a different body is rejected.

## 3. Policy reason codes

The database readiness function can return:

- `CARRIER_NOT_FOUND`
- `POLICY_MISSING`
- `AUTHORITY_INACTIVE_OR_EXPIRING`
- `AUTO_LIABILITY_INSUFFICIENT_OR_EXPIRING`
- `CARGO_INSURANCE_INSUFFICIENT_OR_EXPIRING`
- `SAFETY_RECORD_MISSING`
- `SAFETY_RECORD_STALE`
- `SAFETY_RATING_BLOCKED`
- `OUT_OF_SERVICE`
- `UNSATISFACTORY_SAFETY`
- `OVERRIDE_APPLIED`

A blocked database write is surfaced as HTTP 409 with code
`CARRIER_NOT_READY`, the reason list and the request correlation ID.

## 4. Database roles

Use separate roles:

1. **Migration role** — owns schema changes; not used by API or workers.
2. **API role** — no `BYPASSRLS`; tenant context is set per transaction.
3. **Integration worker role** — dedicated `BYPASSRLS` role, restricted to the
   outbox/inbox/delivery operations it needs. Never reuse it for HTTP requests.
4. **Read-only operations role** — optional reporting role with explicit views;
   do not grant base-table ownership.

The v4 worker exits in production when its current database role does not have
`BYPASSRLS`. This prevents a worker from silently seeing an empty cross-tenant
queue, while keeping the ordinary API role under forced RLS.

## 5. Migration order

Run from the exact reviewed image digest as a one-shot job:

```bash
docker compose --env-file deploy/backend/.env.v4 \
  -f deploy/backend/compose.v4.yaml run --rm migrate
```

The entrypoint runs:

```text
core 0002_identity_tenancy
  -> integrations 0001_integrations_durability
  -> compliance 0001_carrier_readiness
```

Each later track checks the required earlier head and aborts on mismatch. Do not
start the v4 API or worker after a partial migration failure.

## 6. Gateway build and startup

Build the renderer and pin every runtime image to a digest. Place the two gateway
secrets in root-owned mode-0600 files. Create the shared private network once:

```bash
docker network inspect freight_private >/dev/null 2>&1 || \
  docker network create --internal freight_private
```

Render and validate before startup:

```bash
docker compose --env-file deploy/gateway/.env \
  -f deploy/gateway/compose.gateway.yaml config -q

docker compose --env-file deploy/gateway/.env \
  -f deploy/gateway/compose.gateway.yaml run --rm kong-config-render
```

Then start in dependency order:

```bash
docker compose --env-file deploy/backend/.env.v4 \
  -f deploy/backend/compose.v4.yaml up -d freight-api integration-worker

docker compose --env-file deploy/gateway/.env \
  -f deploy/gateway/compose.gateway.yaml up -d redis kong caddy
```

The backend and Kong must share only the `freight_private` network. Redis remains
on the separate internal gateway network. Caddy joins the public and gateway
networks but never the backend network directly.

## 7. Caddy and Kong checks

Before DNS cutover:

```bash
curl -fsS https://api.example.test/health/live
curl -i https://api.example.test/api/v1/admin/integrations/health
```

Expected results:

- health is successful;
- unauthenticated admin access is 401/403;
- a direct request to the private FastAPI listener without gateway proof is 403;
- a caller-supplied `X-Tenant-ID`, `X-Permissions` or
  `X-Freight-Gateway-Proof` does not survive Kong's transformer;
- request-size and rate-limit rejections occur at the gateway;
- `X-Correlation-Id` is echoed;
- no Kong Admin port, Redis port or FastAPI port is publicly reachable.

## 8. Compliance canary

1. Create a policy version with `enabled=false`.
2. Record test carrier authority, insurance and safety evidence.
3. Evaluate readiness; expect `POLICY_MISSING` because no policy is active.
4. Enable the policy through the reviewed administrative process.
5. Evaluate readiness and record the decision ID/input hash.
6. Attempt a tender with insufficient insurance; expect HTTP 409 and no tender row.
7. Add qualifying evidence and repeat with a new `Idempotency-Key`; expect success.
8. Set the latest safety record to out-of-service; verify both API and direct SQL
   tender writes are rejected.
9. Create a permitted non-hard-block override, verify its audit/provenance event,
   then verify it expires automatically.
10. Run provenance verification for `carrier.compliance`.

## 9. Rollback

Rollback external behavior first, not data:

1. Disable the relevant live capability.
2. Disable the integration subscription and connection.
3. Stop the integration worker.
4. Route Caddy away from the new Kong service or restore the previously recorded
   digest.
5. Keep evidence, deliveries, decisions, attempts and provenance records intact.
6. Database downgrade is a last resort and must not be used after v4 writes have
   become authoritative without an approved data-conversion plan.

## 10. Monitoring

Alert on:

- `CARRIER_NOT_READY` rate and reason distribution;
- attempts to use expired or hard-block overrides;
- missing active policy per tenant;
- authority/insurance within the configured expiry buffer;
- stale safety evidence;
- failed provenance verification;
- Kong 429/413/5xx rates and Redis health;
- FastAPI `UNTRUSTED_INGRESS` or `INVALID_GATEWAY_PROOF` responses;
- integration dead letters, stale leases and queue age;
- image digest mismatch against release evidence.

Logs must not contain raw compliance identifiers, policy numbers, payload bodies,
access tokens, HMACs, gateway proof or resolved secret values.

## 11. Release evidence

The manual `backend-release` workflow can run only from protected `main` or a
signed release tag. It verifies the requested source, builds the canonical image,
pushes by immutable tags, scans the resulting digest, generates an SPDX SBOM,
keyless-signs the digest, attaches GitHub build/SBOM attestations, and uploads a
release evidence JSON with `deployment_performed=false`.

Configure the `production-release` GitHub Environment with an independent reviewer.
Publishing an artifact is not approval to migrate or deploy it.
