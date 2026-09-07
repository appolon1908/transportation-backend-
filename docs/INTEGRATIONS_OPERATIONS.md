# Durable integrations: Odoo, n8n and signed webhooks

Status: implementation branch; no live provider capability is enabled by this change.

Canonical repository identity: `freight-platform-backend`.
Current GitHub legacy alias: `appolon1908-hue/transportation-backend-`.

## Runtime separation

The business API continues to commit domain state, audit records and an outbox message in one PostgreSQL transaction. It never calls Odoo, n8n or another provider inline.

The integration stack is deployed as two processes from the same immutable backend image:

- `uvicorn app.integrations_main:app --port 8081` accepts administration requests and signed inbound webhooks.
- `python -m workers.integration_worker --mode all` performs outbox fanout, inbound translation and outbound delivery.

Kong must expose the integration API only through the approved Caddy-to-Kong path. Port 8081 is private and must not be published directly.

## Required database migration

```bash
alembic upgrade 0003_integrations_durability
alembic current
```

Expected head:

```text
0003_integrations_durability
```

The revision is additive and can be downgraded to `0002_identity_tenancy` before live traffic is enabled.

### RLS activation note

The integration ingress must discover a connection from a globally unique, unguessable webhook slug before it knows the tenant, and the worker must claim bounded work across tenants. Revision `0003` therefore does not enable row-level security on the new integration tables. It requires:

1. a dedicated integration-ingress database role;
2. a dedicated integration-worker database role;
3. no schema ownership, DDL, role-management or unrestricted table privileges for either role;
4. explicit tenant predicates on every authenticated query;
5. a reviewed follow-up migration using `SECURITY DEFINER` lookup/claim functions before RLS is enabled for these tables.

This is an explicit production gate, not an implicit exemption.

## Secret handling

Database records contain references such as:

```text
env:ODOO_FREIGHT_API_KEY
env:N8N_FREIGHT_SIGNING_SECRET
env:TRACKING_PROVIDER_WEBHOOK_SECRET
```

They never contain the referenced values. The current resolver accepts only `env:NAME` references and fails closed when a reference is missing, malformed or unresolved. Production may later add a Vault/KMS resolver behind the same interface.

The environment file must be root-owned, mode `0600`, outside the repository and outside the container image.

## Required environment

```text
ENVIRONMENT=production
DATABASE_URL=postgresql+asyncpg://...
OIDC_ISSUER=https://auth.codestra.co/realms/<realm>
OIDC_AUDIENCE=freight-api
INTEGRATION_ALLOWED_HOSTS=odoo.internal.example,n8n.internal.example
INTEGRATION_MAX_WEBHOOK_BYTES=1000000
INTEGRATION_WEBHOOK_TOLERANCE_SECONDS=300
INTEGRATION_DELIVERY_LEASE_SECONDS=90
INTEGRATION_INBOX_LEASE_SECONDS=90
INTEGRATION_RETRY_BASE_SECONDS=10
INTEGRATION_RETRY_CAP_SECONDS=3600
```

Provider secret values referenced by connection records must also be present in the runtime secret environment.

## Capability and connection gates

A delivery occurs only when all of these are true:

1. the integration connection exists for the same tenant as the outbox message;
2. the event type matches an explicit filter (`load.*`, an exact event, or deliberate `*`);
3. the connection has `enabled=true`;
4. the connection has a non-empty `capability_code`;
5. that tenant capability exists in PostgreSQL and has `enabled=true` at delivery time;
6. the destination is approved by `INTEGRATION_ALLOWED_HOSTS`;
7. required secret references resolve;
8. the worker owns a current, unexpired claim.

Disabling the connection or capability stops new external effects without deleting queued evidence.

## Administrative endpoints

All authenticated operations remain tenant-scoped.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/admin/integrations` | List safe connection metadata |
| POST | `/api/v1/admin/integrations` | Create a disabled connection |
| GET | `/api/v1/admin/integrations/{id}` | Read one connection with version ETag |
| PATCH | `/api/v1/admin/integrations/{id}` | Version-checked configuration or enable/disable |
| POST | `/api/v1/admin/integrations/{id}/validate` | Resolve configuration and secrets without sending |
| GET/POST | `/api/v1/admin/integrations/{id}/webhook-keys` | List or register rotating inbound keys |
| GET | `/api/v1/admin/integrations/{id}/deliveries` | Inspect delivery state |
| POST | `/api/v1/admin/integrations/deliveries/{id}/replay` | Audited terminal replay |
| GET | `/api/v1/admin/integrations/inbox/messages` | Inspect inbound state |
| POST | `/api/v1/admin/integrations/inbox/{id}/replay` | Audited inbound replay |
| GET | `/api/v1/admin/integrations/provenance` | Read append-only hash-chain entries |
| GET | `/api/v1/admin/integrations/provenance/verify` | Recalculate a chain and report the first invalid entry |

Material configuration calls require an `Idempotency-Key` through the existing command boundary.

## Inbound webhook contract

Preferred endpoint:

```text
POST /api/v1/integrations/{webhook_slug}/webhooks/{provider}
```

Compatibility endpoint:

```text
POST /api/v1/integrations/tracking/{provider}/webhooks
X-Integration-Slug: <webhook_slug>
```

Required headers:

```text
X-Webhook-Id: provider-stable-event-id
X-Webhook-Timestamp: unix-seconds
X-Webhook-Key-Id: rotating-key-id
X-Webhook-Signature: sha256=<hex-hmac>
X-Event-Type: tracking.position.received
```

Signature input:

```text
<unix-seconds>.<exact raw request bytes>
```

HMAC algorithm: SHA-256.

The server validates the key activation window, replay timestamp, HMAC and JSON body before insertion. Deduplication is unique per `(connection_id, external_event_id)`:

- same ID and same body hash returns an accepted duplicate;
- same ID and a different body hash returns HTTP 409 and records a provenance conflict;
- concurrent duplicates converge on the same inbox record.

Only redacted headers are persisted.

## n8n reverse commands

n8n may send a signed `automation.command.requested` event through the same webhook boundary. The inbound worker never executes arbitrary workflow-provided code or SQL. It accepts only these command names:

```text
tracking.event.record
operations.exception.create
document.review.request
shipment.review.request
```

Command requests are persisted in `integration_command_requests`. Commands requiring additional domain handlers remain in `RECEIVED` state until a reviewed handler is added. Unknown commands fail terminally and open an operational exception.

## Odoo 19 JSON-2 connection

Connection kind:

```text
ODOO_JSON2
```

Required connection fields:

```json
{
  "base_url": "https://odoo.internal.example",
  "secret_ref": "env:ODOO_FREIGHT_API_KEY",
  "configuration": {
    "model": "freight.integration.event",
    "method": "ingest_event",
    "payload_parameter": "event",
    "database": "codestra"
  },
  "event_types": ["customer.*", "shipment.*", "load.*", "invoice.*"],
  "capability_code": "integration.odoo.live_delivery"
}
```

The adapter sends:

```text
POST /json/2/{model}/{method}
Authorization: Bearer <resolved API key>
X-Odoo-Database: <configured database, when required>
Idempotency-Key: <event UUID>
```

The JSON body contains one versioned freight event under the configured payload parameter. Odoo must implement its own unique event-ID constraint and return a successful status for already-applied duplicates.

## n8n / generic signed outbound connection

Connection kind:

```text
N8N_WEBHOOK
```

Required fields:

```json
{
  "base_url": "https://n8n.internal.example",
  "endpoint_path": "/webhook/freight-events",
  "signing_secret_ref": "env:N8N_FREIGHT_SIGNING_SECRET",
  "signing_key_id": "freight-2026-08",
  "event_types": ["load.*", "tracking.*", "operations.exception.*"],
  "capability_code": "integration.n8n.live_delivery"
}
```

Outbound headers include the event ID, Unix timestamp, key ID, HMAC signature and `Idempotency-Key`. Redirects are not followed. Network failures, 408, 425, 429 and 5xx responses are retryable; other 4xx responses are terminal.

## Retry and provenance behavior

Delivery claims use `FOR UPDATE SKIP LOCKED`, an expiring token and capped exponential retry. The external request occurs after the claim transaction commits. Finalization requires the same claim token, preventing a stale process from overwriting a replacement worker.

Every accepted inbound event, payload-ID conflict, delivery creation, attempt outcome, replay and translation result appends an entry to a tenant/connection hash chain. Provider response bodies are not stored; only their SHA-256 hashes and status codes are retained.

## Production acceptance checklist

Do not enable a live capability until all checks pass:

```text
[ ] GitHub repository is physically renamed to freight-platform-backend
[ ] PR stack reviewed and CI green at unchanged commit SHA
[ ] immutable image digest and SBOM recorded
[ ] migration backup and rollback rehearsal passed
[ ] dedicated ingress and worker DB roles installed
[ ] integration-table RLS follow-up reviewed or formally risk-accepted
[ ] Kong route has auth/rate-limit/body-size policies
[ ] Caddy trusts only Kong/upstream private addresses
[ ] Odoo and n8n hosts are explicitly allowlisted
[ ] provider secret references resolve without logging values
[ ] inbound key rotation and replay-window test passed
[ ] duplicate same-body and conflict different-body tests passed
[ ] capability remains disabled during smoke test
[ ] human approval recorded before capability enablement
```
