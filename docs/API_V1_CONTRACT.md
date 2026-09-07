# Freight Platform V1 API Contract

`app.production_v4:app` is the authoritative production API composition. Generated OpenAPI is the machine-readable contract for paths, methods, schemas and authentication; this document records the operating rules and major route groups.

## Release identity

```text
service                 freight-platform-backend
application version     shared by all backend entrypoints
canonical migration     0005_portal_workflows
authentication          OIDC bearer JWT
human identity provider auth.codestra.co
```

`GET /health/version` exposes `name`, `version`, `git_sha`, `image_digest` and `migration_head`. Configuration fails when `MIGRATION_HEAD` does not equal the canonical schema head.

## Cross-cutting contract

Authenticated `/api/v1/*` operations are tenant scoped and permission checked. OpenAPI declares an HTTP `BearerAuth` JWT scheme. Provider webhook ingress is separately authenticated with timestamp-bound signatures, rotating key identity and payload hashing.

Material commands require `Idempotency-Key`. Versioned state transitions reject stale writes. Responses carry `X-Correlation-Id`; API responses use `Cache-Control: no-store`. External delivery and external portal access remain disabled unless an approved production change explicitly activates the relevant capability.

## Health and caller context

```text
GET /health/live
GET /health/ready
GET /health/version
GET /api/v1/me
GET /api/v1/me/permissions
GET /api/v1/capabilities
GET /api/v1/auth/context
```

## Persistent administration and tenancy

```text
GET   /api/v1/admin/tenant
PATCH /api/v1/admin/tenant
GET   /api/v1/admin/organizations
POST  /api/v1/admin/organizations
GET   /api/v1/admin/users
POST  /api/v1/admin/users
POST  /api/v1/admin/users/{principal_id}/identities
GET   /api/v1/admin/memberships
POST  /api/v1/admin/memberships
PUT   /api/v1/admin/memberships/{membership_id}/roles
GET   /api/v1/admin/roles
POST  /api/v1/admin/roles
PUT   /api/v1/admin/roles/{role_id}/permissions
GET   /api/v1/admin/permissions
GET   /api/v1/admin/capabilities
PATCH /api/v1/admin/capabilities/{code}
GET   /api/v1/admin/audit
```

The persistent identity/RBAC implementation is authoritative. The former `501 IDENTITY_DIRECTORY_NOT_IMPLEMENTED` placeholders are removed before route registration.

## Customers and carriers

```text
GET   /api/v1/customers
POST  /api/v1/customers
GET   /api/v1/customers/{customer_id}
PATCH /api/v1/customers/{customer_id}
GET   /api/v1/customers/{customer_id}/locations
POST  /api/v1/customers/{customer_id}/locations
GET   /api/v1/customers/{customer_id}/contacts
POST  /api/v1/customers/{customer_id}/contacts

GET   /api/v1/carriers
POST  /api/v1/carriers
GET   /api/v1/carriers/{carrier_id}
PATCH /api/v1/carriers/{carrier_id}
GET   /api/v1/carriers/{carrier_id}/contacts
GET   /api/v1/carriers/{carrier_id}/equipment
GET   /api/v1/carriers/{carrier_id}/compliance
GET   /api/v1/carriers/{carrier_id}/insurance
POST  /api/v1/carriers/{carrier_id}/approve
POST  /api/v1/carriers/{carrier_id}/suspend
POST  /api/v1/carriers/{carrier_id}/readiness/evaluate
```

Carrier assignment and dispatch are database-blocked when authority, insurance or safety policy requirements are not satisfied.

## Quotes, shipments and loads

```text
GET  /api/v1/quotes
POST /api/v1/quotes
GET  /api/v1/quotes/{quote_id}
POST /api/v1/quotes/{quote_id}/send
POST /api/v1/quotes/{quote_id}/accept
POST /api/v1/quotes/{quote_id}/decline
POST /api/v1/quotes/{quote_id}/revise

GET   /api/v1/shipments
POST  /api/v1/shipments
GET   /api/v1/shipments/{shipment_id}
PATCH /api/v1/shipments/{shipment_id}
POST  /api/v1/shipments/{shipment_id}/cancel
GET   /api/v1/shipments/{shipment_id}/legs
POST  /api/v1/shipments/{shipment_id}/legs
GET   /api/v1/shipments/{shipment_id}/stops
POST  /api/v1/shipments/{shipment_id}/stops

GET    /api/v1/loads
POST   /api/v1/loads
GET    /api/v1/loads/{load_id}
POST   /api/v1/loads/{load_id}/shipment-legs
DELETE /api/v1/loads/{load_id}/shipment-legs/{leg_id}
GET    /api/v1/loads/{load_id}/carrier-search
GET    /api/v1/loads/{load_id}/tenders
POST   /api/v1/loads/{load_id}/tenders
POST   /api/v1/tenders/{tender_id}/accept
POST   /api/v1/tenders/{tender_id}/reject
POST   /api/v1/tenders/{tender_id}/withdraw
POST   /api/v1/loads/{load_id}/dispatch
POST   /api/v1/loads/{load_id}/arrive
POST   /api/v1/loads/{load_id}/depart
POST   /api/v1/loads/{load_id}/deliver
```

## Visibility and tracking

```text
GET  /api/v1/loads/{load_id}/tracking
GET  /api/v1/loads/{load_id}/positions
GET  /api/v1/loads/{load_id}/exceptions
POST /api/v1/loads/{load_id}/tracking/manual-event
POST /api/v1/integrations/tracking/{provider}/webhooks
```

The legacy tracking webhook requires `X-Webhook-Id`, `X-Webhook-Timestamp` and `X-Webhook-Signature`. The durable provider-neutral integration ingress below is preferred.

## Durable integrations, Odoo and n8n

```text
GET  /api/v1/admin/integrations/health
GET  /api/v1/admin/integrations
POST /api/v1/admin/integrations
GET  /api/v1/admin/integrations/{connection_id}/deliveries
GET  /api/v1/admin/integrations/inbox/messages
GET  /api/v1/admin/integrations/provenance/verify
POST /api/v1/integrations/{webhook_slug}/webhooks/{provider}
```

The integration boundary supports Odoo JSON-2, n8n signed webhooks and generic signed webhooks. It provides forced tenant RLS, distinct API/ingress/worker database roles, durable inbox/outbox records, timestamp replay protection, event-ID deduplication, same-ID/different-payload collision rejection, retries and provenance verification. Credentials are references resolved outside Git and never appear in health responses.

## Compliance administration

```text
GET  /api/v1/admin/compliance/policies
POST /api/v1/admin/compliance/policies
```

Compliance includes versioned policy, authority, insurance, safety, override and readiness-decision records with forced tenant RLS and database enforcement functions.

## Operations, finance and claims

```text
GET  /api/v1/operations/control-tower
GET  /api/v1/operations/exceptions
POST /api/v1/operations/exceptions/{exception_id}/acknowledge
POST /api/v1/operations/exceptions/{exception_id}/assign
POST /api/v1/operations/exceptions/{exception_id}/resolve
GET  /api/v1/operations/dead-letters
POST /api/v1/operations/dead-letters/{message_id}/replay

GET  /api/v1/invoices
POST /api/v1/invoices
GET  /api/v1/invoices/{invoice_id}
POST /api/v1/invoices/{invoice_id}/approve
POST /api/v1/invoices/{invoice_id}/void
GET  /api/v1/carrier-settlements
POST /api/v1/carrier-settlements
GET  /api/v1/carrier-settlements/{settlement_id}
POST /api/v1/carrier-settlements/{settlement_id}/approve
GET  /api/v1/claims
POST /api/v1/claims
GET  /api/v1/claims/{claim_id}
```

## Portal APIs

### Administration and review

```text
GET   /api/v1/admin/portal-bindings
POST  /api/v1/admin/portal-bindings
GET   /api/v1/admin/portal-reviews/claims
PATCH /api/v1/admin/portal-reviews/claims/{submission_id}
GET   /api/v1/admin/portal-reviews/carrier-evidence
PATCH /api/v1/admin/portal-reviews/carrier-evidence/{submission_id}
```

There are no `/decision` review paths. Review writes are `PATCH` operations with expected-version validation, idempotency, audit and provenance evidence.

### Customer portal

```text
GET  /api/v1/portals/customer/context
GET  /api/v1/portals/customer/quotes
GET  /api/v1/portals/customer/quotes/{quote_id}
POST /api/v1/portals/customer/quotes/{quote_id}/decision
GET  /api/v1/portals/customer/shipments
GET  /api/v1/portals/customer/shipments/{shipment_id}
GET  /api/v1/portals/customer/documents
GET  /api/v1/portals/customer/invoices
GET  /api/v1/portals/customer/claims
POST /api/v1/portals/customer/claims
```

### Carrier portal

```text
GET  /api/v1/portals/carrier/context
GET  /api/v1/portals/carrier/tenders
POST /api/v1/portals/carrier/tenders/{tender_id}/response
GET  /api/v1/portals/carrier/loads
GET  /api/v1/portals/carrier/loads/{load_id}
POST /api/v1/portals/carrier/loads/{load_id}/tracking
GET  /api/v1/portals/carrier/documents
GET  /api/v1/portals/carrier/evidence
POST /api/v1/portals/carrier/evidence
GET  /api/v1/portals/carrier/settlements
```

Customer and carrier public access remain disabled by default. Internal review and staging can exercise the workflows without enabling public portal capabilities.

## Document pipeline status

```text
POST /api/v1/documents/upload-sessions
POST /api/v1/documents/{document_id}/confirm
GET  /api/v1/loads/{load_id}/documents
POST /api/v1/loads/{load_id}/documents
POST /api/v1/loads/{load_id}/pod
```

Document reads are implemented. Upload, confirmation, attachment and POD writes intentionally return `503 STORAGE_NOT_CONFIGURED` until secure object storage, malware scanning, quarantine, content validation and retention controls are implemented on `be/documents-secure-storage-v1`.

## Default-disabled capabilities

```text
carrier.live_tender_send                 disabled
carrier.live_dispatch_notification       disabled
email.live_send                          disabled
sms.live_send                            disabled
accounting.live_export                    disabled
customer_portal.external_access           disabled
carrier_portal.external_access            disabled
```

Route presence and source tests are not deployment approval. Production additionally requires exact-head green CI, independent review, protected merge, immutable signed images, secrets outside Git, staging backup/restore and rollback evidence, Kong/Caddy validation and a capability-by-capability canary.
