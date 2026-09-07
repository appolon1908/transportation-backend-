# Freight Platform Backend Implementation Plan

Canonical project name: `freight-platform-backend`

## Current repository status

This repository currently contains documentation only. No executable FastAPI runtime, database migrations, workers, tests, Docker runtime, CI pipeline, or production integration code is present yet.

```text
ARCHITECTURE_DEFINED=YES
RUNTIME_IMPLEMENTED=NO
DATABASE_IMPLEMENTED=NO
AUTH_IMPLEMENTED=NO
AUTHORIZATION_IMPLEMENTED=NO
OUTBOX_IMPLEMENTED=NO
INBOX_IMPLEMENTED=NO
WEBHOOKS_IMPLEMENTED=NO
PRODUCTION_READY=NO
```

The implementation must use **Python + FastAPI**. Any earlier .NET/C# examples are non-binding and must not be introduced into this repository.

## Binding backend stack

- Python
- FastAPI
- Pydantic v2
- SQLAlchemy 2.x async
- Alembic
- PostgreSQL
- PostGIS
- Redis
- Celery or ARQ workers
- OIDC/OAuth 2.0
- OpenAPI
- OpenTelemetry
- Prometheus-compatible metrics
- S3-compatible secure object storage
- Docker
- GitHub Actions

## Missing code — P0 production foundation

### Runtime / API

- `pyproject.toml`
- `app/main.py`
- settings/configuration with fail-closed production validation
- API router `/api/v1`
- standard API error envelope
- correlation/request ID middleware
- structured logging and secret/PII redaction
- `/health/live`
- `/health/ready`
- `/health/version`
- OpenAPI generation
- Dockerfile and development compose stack
- CI lint/type/test workflow

### PostgreSQL / tenancy

- async SQLAlchemy engine/session
- base models and UUID/TIMESTAMPTZ/version conventions
- Alembic migration framework
- tenant, organization, user, membership and external-identity tables
- tenant-owned foreign keys and indexes
- PostGIS extension migration
- PostgreSQL row-level security or equivalent DB-enforced tenant isolation
- cross-tenant negative tests

### Authentication / authorization

- OIDC/JWKS validation
- issuer, audience, algorithm, expiry, nbf, iat and key-id validation
- external identity binding by `issuer + subject`
- machine-to-machine client-credentials support
- disabled-user fail-closed behavior
- roles
- permissions
- role-permission bindings
- tenant membership/role bindings
- record-level authorization predicates
- authorized repository queries

### Capabilities

- capability registry
- tenant overrides
- dependency rules
- unknown capability = disabled
- server-side capability enforcement
- capability-change audit

### Command infrastructure

- `CommandContext`
- explicit command handlers
- policies
- state machines
- unit-of-work / transaction boundary
- optimistic concurrency/version enforcement
- ETag / `If-Match`
- stale-write 412/409 mapping
- idempotency table and service
- request hashing
- `IN_PROGRESS`, `COMPLETED`, `FAILED_RETRYABLE`
- replay/conflict behavior
- one atomic transaction containing business mutation + history + audit + idempotency + outbox

### Audit / outbox / inbox

- append-only audit records
- outbox records and schema versioning
- retry schedule and terminal state
- worker claim token / lease expiry
- PostgreSQL `FOR UPDATE SKIP LOCKED`
- stale-worker finalization protection
- outbox worker
- inbox records
- provider/external-event uniqueness
- signature result and key-id storage
- raw-body hash
- timestamp/replay checks
- inbox worker
- dead-letter / operational exception creation
- authorized manual replay

## Missing code — P1 business domains

### Commercial

- customers, contacts and locations
- quotes and quote versions
- sell rates and carrier buy rates
- accessorials and fuel surcharge
- margin calculation and approval rules
- quote send/accept/decline/revise/expire commands and state machine

### Carrier

- carriers, contacts, drivers and equipment
- authority, compliance and insurance
- expiration/readiness policies
- suspension
- carrier lanes/search
- carrier scorecard foundation

### Transportation

- shipments
- shipment legs
- stops/appointments
- commodities/hazmat foundation
- loads
- load ↔ shipment-leg assignment
- carrier assignment history
- tenders
- shipment/load/tender/stop state machines
- create/update/cancel shipment
- create/remove legs
- create load
- attach/detach legs
- tender send/accept/reject/withdraw/expire
- dispatch, arrive, depart, pickup, transit and delivery commands
- concurrency tests for carrier assignment and tender acceptance

### Visibility

- tracking events
- PostGIS positions
- current-position projection
- ETA and ETA history
- geofences
- dwell
- arrival/departure detection
- exceptions
- tracking adapter interfaces and provider implementations
- duplicate/out-of-order event handling

### Documents

- document metadata/version model
- upload sessions
- object-storage adapter
- signed uploads/downloads
- checksum/MIME/size/extension validation
- malware scanner adapter
- upload/scanning state machine
- BOL, rate confirmation, POD, receipt and insurance purposes

### Finance

- invoices and invoice lines
- invoice state machine
- carrier settlements and lines
- settlement state machine
- accessorial charges
- claims foundation
- financial audit
- accounting adapter interface
- Decimal/minor-unit money rules

### Operations / notifications / reporting

- operational exception records and workflows
- dead-letter query/replay
- notification intents/templates/preferences
- email/SMS adapters and worker
- KPI/revenue/margin/performance reports
- global search

## Binding API surface

At minimum V1 must expose the documented platform, customer, carrier, quote, shipment, load, tender, dispatch, tracking, document, finance, operations and admin endpoints in `docs/ARCHITECTURE.md` and generated OpenAPI.

Important mutations must be explicit business commands rather than arbitrary status PATCH operations.

## Backend sequence

1. Repository + CI + runtime foundation
2. PostgreSQL/PostGIS + migrations + tenancy
3. OIDC authentication
4. RBAC/permissions + record authorization + capabilities
5. Command context + audit + idempotency + optimistic concurrency
6. Outbox/inbox + durable jobs + replay
7. Customers + contacts + locations
8. Carriers + compliance + insurance + equipment
9. Quotes + rates + margin
10. Shipments + legs + stops + commodities
11. Loads + shipment-leg assignments
12. Tendering + carrier assignment + dispatch
13. Tracking + ETA + geofences + exceptions
14. Documents + secure blob storage + malware scanning
15. Invoices + settlements + claims foundation
16. Webhooks + integration adapters
17. Notifications
18. Reporting + search
19. Observability + security hardening
20. Performance + resilience testing
21. Staging + backup/restore + immutable deployment + rollback gates

## Required quality gates per implementation PR

Each PR must include all applicable:

- implementation
- additive migration
- rollback/downgrade path
- domain rules/state transitions
- unit tests
- real PostgreSQL/PostGIS integration tests
- authorization/tenant-isolation tests
- idempotency/concurrency tests
- OpenAPI contract changes
- audit/outbox/inbox behavior
- metrics/logging/tracing
- failure-mode handling
- operational documentation
- readiness evidence

## External effect policy

Remain disabled until complete production gates pass:

- `carrier.live_tender_send`
- `carrier.live_dispatch_notification`
- `email.live_send`
- `sms.live_send`
- `accounting.live_export`
- `documents.external_share`
- `customer_portal.external_access`
- `carrier_portal.external_access`
- production-mutating external webhooks unless explicitly approved

## Required backend tree

```text
freight-platform-backend/
├── app/
│   ├── main.py
│   ├── api/v1/
│   ├── platform/
│   ├── commercial/
│   ├── carriers/
│   ├── transportation/
│   ├── visibility/
│   ├── documents/
│   ├── finance/
│   ├── integrations/
│   ├── operations/
│   ├── notifications/
│   ├── reporting/
│   ├── search/
│   ├── persistence/
│   ├── adapters/
│   └── observability/
├── workers/
├── migrations/
├── tests/
│   ├── unit/
│   ├── domain/
│   ├── integration/
│   ├── authorization/
│   ├── concurrency/
│   ├── webhooks/
│   ├── contract/
│   └── e2e/
├── docs/
├── deploy/
├── scripts/
├── .github/workflows/
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── alembic.ini
└── README.md
```

## Current verdict

```text
DESIGN=STRONG
IMPLEMENTATION=DOCUMENTATION_ONLY
NEXT_SAFE_ACTION=PR_01_RUNTIME_FOUNDATION
PRODUCTION_READY=NO
```
