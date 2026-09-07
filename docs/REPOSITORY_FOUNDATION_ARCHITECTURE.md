# Freight Platform Backend Architecture

Canonical project name: `freight-platform-backend`

## Mission

Provide the server-side operating system for a multi-tenant freight brokerage / 3PL platform covering commercial pricing, carrier management, shipment/load execution, tracking, documents, finance, integrations, operations, notifications, reporting, and search.

## Domain boundaries

### Platform
- Tenant management
- Organizations
- Users
- Authentication
- Sessions / token validation
- Authorization / RBAC
- Fine-grained permissions
- Capabilities / feature activation
- Configuration
- Secrets references
- Audit trail
- Idempotency
- Optimistic concurrency
- Outbox
- Inbox
- Background jobs
- Operational exceptions
- System health/readiness

### Commercial
- Customers
- Customer contacts
- Customer locations
- Contracts
- Quotes
- Quote versions
- Sell rates
- Carrier buy rates
- Rate tables
- Fuel surcharge
- Accessorials
- Margin
- Margin rules
- Quote approval

### Carrier
- Carriers
- Carrier contacts
- Drivers
- Equipment
- Equipment types
- Compliance
- Authority
- Insurance
- Documents
- Safety records
- Carrier scorecards
- Carrier lanes
- Carrier search

### Transportation
- Shipments
- Shipment legs
- Stops
- Stop appointments
- Commodities
- Hazmat foundation
- Loads
- Load ↔ shipment-leg assignments
- Carrier assignment
- Tendering
- Tender offers
- Dispatch
- Driver assignment
- Pickup
- In-transit
- Delivery
- Cancellation
- Shipment/load state machines

### Visibility
- Tracking events
- GPS positions
- Current position
- ETA
- ETA history
- Geofences
- Arrival/departure detection
- Dwell
- Temperature foundation
- Exceptions
- Tracking provider status

### Documents
- BOL
- Rate confirmation
- POD
- Receipts
- Insurance
- Carrier documents
- Customer documents
- Document metadata
- Document versions
- Virus scanning
- Signed download URLs
- Secure object/blob storage

### Finance
- Customer invoices
- Invoice lines
- Invoice adjustments
- Carrier settlements
- Settlement lines
- Accessorial charges
- Payment status
- Receivables foundation
- Payables foundation
- Claims
- Claim documents
- Financial audit

### Integrations
- Service Bus
- Event contracts
- Outbound Webhooks
- Inbound Webhooks
- Webhook signatures
- Webhook retries
- Dead-letter handling
- Tracking adapters
- Carrier adapters
- EDI foundation
- Accounting adapters
- Email
- SMS
- Maps/geocoding
- External identity
- Operations replay

### Operations
- Exception queue
- Dispatch board
- Load board
- Manual overrides
- Task management
- Notes
- Internal comments
- Escalations
- SLA monitoring
- Replay/recovery tools

### Notifications
- Notification templates
- Email notifications
- SMS notifications
- In-app notifications
- Preferences
- Delivery status
- Retry/dead-letter

### Reporting
- Operational KPIs
- Revenue
- Margin
- Carrier performance
- Customer performance
- On-time pickup
- On-time delivery
- Dwell
- Exports

### Search
- Shipment search
- Load search
- Carrier search
- Customer search
- Document search
- Global operations search

## API authority

Top-level resources:

- `/api/v1/tenants`
- `/api/v1/users`
- `/api/v1/roles`
- `/api/v1/permissions`
- `/api/v1/capabilities`
- `/api/v1/customers`
- `/api/v1/customer-locations`
- `/api/v1/quotes`
- `/api/v1/rates`
- `/api/v1/accessorials`
- `/api/v1/carriers`
- `/api/v1/carriers/{id}/contacts`
- `/api/v1/carriers/{id}/equipment`
- `/api/v1/carriers/{id}/compliance`
- `/api/v1/carriers/{id}/insurance`
- `/api/v1/shipments`
- `/api/v1/shipments/{id}/legs`
- `/api/v1/shipments/{id}/stops`
- `/api/v1/shipments/{id}/commodities`
- `/api/v1/loads`
- `/api/v1/loads/{id}/assignments`
- `/api/v1/loads/{id}/tenders`
- `/api/v1/loads/{id}/dispatch`
- `/api/v1/loads/{id}/tracking`
- `/api/v1/tracking-events`
- `/api/v1/exceptions`
- `/api/v1/documents`
- `/api/v1/invoices`
- `/api/v1/settlements`
- `/api/v1/claims`
- `/api/v1/notifications`
- `/api/v1/reports`
- `/api/v1/webhooks`
- `/api/v1/integrations`

Important state changes must use explicit commands, for example:

- `POST /loads/{id}/tenders`
- `POST /tenders/{id}/accept`
- `POST /tenders/{id}/reject`
- `POST /loads/{id}/dispatch`
- `POST /loads/{id}/cancel`
- `POST /shipments/{id}/pickup`
- `POST /shipments/{id}/deliver`
- `POST /invoices/{id}/issue`
- `POST /invoices/{id}/void`
- `POST /settlements/{id}/approve`

## Material write pipeline

Every material command must enforce, in order:

1. Authentication
2. Tenant isolation
3. Authorization/permission
4. Capability enabled
5. Resource access
6. Optimistic concurrency/version check
7. Valid domain state transition
8. Relevant compliance checks
9. Idempotency
10. Database transaction
11. Audit record
12. Outbox event

The frontend may improve UX by hiding unavailable actions, but backend checks are authoritative.

## Event envelope

All domain/integration events use a versioned envelope:

```json
{
  "id": "evt_uuid",
  "type": "shipment.delivered",
  "version": "1",
  "occurred_at": "2026-08-23T18:00:00Z",
  "tenant_id": "tenant_uuid",
  "aggregate_id": "shipment_uuid",
  "correlation_id": "uuid",
  "data": {}
}
```

Important event families include:

- `quote.created`, `quote.updated`, `quote.accepted`, `quote.expired`
- `shipment.created`, `shipment.updated`, `shipment.cancelled`
- `load.created`, `load.carrier_assigned`
- `tender.created`, `tender.accepted`, `tender.rejected`, `tender.expired`
- `load.dispatched`, `load.picked_up`, `load.in_transit`, `load.delivered`
- `tracking.position.received`, `tracking.eta.changed`, `tracking.exception.detected`
- `document.uploaded`, `document.pod.received`
- `invoice.created`, `invoice.issued`, `invoice.paid`
- `settlement.created`, `settlement.approved`, `settlement.paid`
- `carrier.compliance.changed`, `carrier.insurance.expiring`, `carrier.insurance.expired`

## Webhook requirements

Inbound and outbound webhooks must support:

- HMAC signatures
- Timestamp protection
- Replay protection
- Idempotency
- Event IDs
- Delivery attempts
- Exponential retry
- Dead-letter handling
- Audit trail
- Manual replay
- Request/response logging with secret redaction

## Target runtime

- FastAPI
- Python
- PostgreSQL
- SQLAlchemy 2
- Alembic
- Redis
- Durable worker layer
- Service bus / broker abstraction
- S3-compatible object storage
- OIDC/Keycloak-compatible identity
- OpenTelemetry
- Prometheus
- Grafana
- Sentry-compatible error reporting
- Docker
- GitHub Actions

## Architecture style

Start as a modular monolith with hard domain boundaries. Adapters must be behind interfaces. Domain logic must not depend directly on provider SDKs. Separate services later only when operational scale or ownership justifies it.
