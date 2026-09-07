# Repository Profile — `transportation-backend-`

## Identity

- **Repository:** `appolon1908-hue/transportation-backend-`
- **Category:** Product backend — freight brokerage
- **Visibility:** `public`
- **Default branch:** `main`
- **Authority:** Primary freight-platform backend authority
- **Status:** Canonical server-side architecture for freight brokerage and 3PL operations.

## Purpose

Provides the authoritative backend for tenancy, commercial quoting, carriers, shipments, dispatch, tracking, documents, finance, integrations, operations, reporting, notifications, and search.

## Owns

- Freight business rules, state machines, persistence, authorization, and audit
- Shipment, load, carrier, quote, tracking, document, and finance domains
- Idempotency, concurrency, inbox/outbox, workers, adapters, reconciliation, and operational recovery

## Does not own

- Frontend presentation
- Identity-provider internals
- Unapproved live tendering, dispatch, accounting exports, communications, or portal activation

## Key integrations

- `transportaion-Frontend`
- Keycloak
- Middleware and communications services
- EDI, load boards, telematics, maps, accounting, storage, and carrier adapters

## Current priorities

1. Implement the platform safety foundation first
2. Complete commercial, carrier, and transportation vertical slices
3. Add visibility, documents, finance, operations, reporting, and search
4. Prove external side effects, reconciliation, backup/restore, and rollback before activation

## Governance and safety

- Promotion model: `feature/docs/fix/security/upgrade -> development -> test -> staging -> production -> main`.
- Use pull requests and exact-head/merge-result validation; source merge never authorizes live freight operations.
- Never commit credentials, customer/carrier documents, database dumps, or provider secrets.
- Production artifacts and migrations must be immutable, traceable, and reversible.
- This document does not tender, dispatch, invoice, pay, communicate, or activate production.

## Account-wide catalog

See `appolon1908-hue/documentaions/REPOSITORY_CATALOG.md`.
