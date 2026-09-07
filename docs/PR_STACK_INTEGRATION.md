# Backend pull-request integration

This candidate combines the existing implementation branches without resetting
or discarding the development integration history.

## Source inputs

- Development integration #14: `5a2a52b82fa152f89aec53e63b5619d82c254d1a`.
- API/release contract #7, including previously accepted deployment scaffold #9:
  `2aa0508428d4c702dfe5d9f3edd13685a180a8e6`.
- Recovery command #8: `81b705cea5ee8689712fa47c4a584276cba9b6c7`.

The #14/#7 shared ancestor is in the release-readiness lineage. #14's nine
additional governance/documentation files are retained unchanged. Its flat-route
compliance guard is superseded by the later app-state registration guard, which
also invalidates cached OpenAPI after composition. The complete #7 secure
scaffold, migrations, identity, compliance and provider boundaries are retained.

The #7/#8 conflicts are resolved by retaining both sets of authoritative route
replacements: persistent admin handlers, compliance, durable signed ingress,
and command-backed dead-letter replay. The old direct replay handler is removed
before registration. #7's more recent deployment documentation and duplicate-route
tests are retained; the separate #8 PostgreSQL recovery tests are added intact.
Integration-health queue counting fixes are already present in #7.

## Integrated recovery API

`POST /api/v1/operations/dead-letters/{message_id}/replay` requires the existing
OIDC/local permission `integration.retry`, a non-empty Idempotency-Key and a
reason. It locks a tenant-owned terminal outbox row, moves it only to
PENDING_CONFIGURATION, and atomically records the command result, audit and
`operations.dead_letter.replayed.v1` outbox event. Identical retries return the
original result; changed payloads, other tenants and non-terminal rows are denied.
It does not authorize or immediately perform provider delivery.

`GET /api/v1/admin/integrations/health` reports only the authenticated tenant's
connection and inbox/delivery counts, including terminal failures from both queues.
The canonical release identity is `0005_portal_workflows`; compliance has its own
migration head. All earlier default-disabled capability gates remain unchanged.

## Validation and acceptance

Run the complete backend, integrations, compliance/gateway and repository-aware
CI against this exact candidate and current merge result. New composition tests
exercise real unauthorized HTTP dispatch and present/absent Server headers;
route-presence checks are not a replacement for these runtime tests. All existing
PostgreSQL RLS, signed-webhook, collision, replay/audit and migration tests remain.
No local full-suite result or staging/production certification is claimed here.

Independent current-head review is still required where repository policy says
so. Earlier approvals do not certify new conflict resolutions. This integration
does not permit a branch-policy bypass, force push, host deployment, runtime
migration, secrets change, provider activation or production promotion.
