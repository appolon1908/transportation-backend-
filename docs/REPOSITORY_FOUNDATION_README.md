# freight-platform-backend

Canonical backend repository for the freight brokerage / 3PL operating platform.

This repository owns all server-side business authority, persistence, integrations, workers, security enforcement, auditability, and operational reliability.

## Core domains

- Platform: tenancy, authentication, authorization, capabilities, audit, idempotency, optimistic concurrency, outbox, inbox, background jobs, operational exceptions, health/readiness.
- Commercial: customers, contacts, locations, contracts, quotes, quote versions, sell rates, carrier buy rates, rate tables, fuel surcharge, accessorials, margin, approvals.
- Carrier: carriers, contacts, drivers, equipment, compliance, authority, insurance, safety, scorecards, lanes, carrier search.
- Transportation: shipments, legs, stops, appointments, commodities, hazmat foundation, loads, load/leg assignments, carrier assignment, tendering, dispatch, pickup, in-transit, delivery, cancellation, state machines.
- Visibility: tracking events, GPS positions, ETA, geofences, arrival/departure detection, dwell, exceptions, provider health.
- Documents: BOL, rate confirmation, POD, receipts, insurance, document metadata/versioning, malware scanning, signed access, secure object storage.
- Finance: customer invoices, invoice lines/adjustments, carrier settlements, settlement lines, accessorial charges, payment status, AR/AP foundations, claims, financial audit.
- Integrations: service bus, event contracts, inbound/outbound webhooks, signatures, retries, dead letters, tracking adapters, carrier adapters, EDI, accounting adapters, email/SMS, maps/geocoding, identity, replay.
- Operations: exception queue, dispatch board, load board, manual overrides, tasks, notes, internal comments, escalations, SLA monitoring, replay/recovery.
- Notifications: templates, email, SMS, in-app notifications, preferences, delivery status, retry/dead-letter.
- Reporting: operational KPIs, revenue, margin, carrier/customer performance, on-time pickup/delivery, dwell, exports.
- Search: shipment, load, carrier, customer, document, and global operations search.

## Architecture rules

- Backend is the source of truth for all business rules and permissions.
- Every material write must enforce tenant isolation, authorization, capabilities, valid state transitions, idempotency, concurrency, audit, and outbox publication.
- External side effects must be adapter-driven and capability-gated.
- Production activation of tendering, dispatch, accounting exports, email/SMS, and portal access must remain disabled until release gates pass.

See `docs/ARCHITECTURE.md` and `docs/IMPLEMENTATION_PLAN.md` for the implementation authority.
