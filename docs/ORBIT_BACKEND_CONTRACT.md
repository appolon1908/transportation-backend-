# Backend contract discovery and remaining portal responsibilities

`GET /api/v1/platform/contract` is a read-only, authenticated backend endpoint.
It advertises existing identity context, permission, tenant-capability,
integration-health, operations, customer and carrier context APIs, with the
canonical schema revision. Tenant identity comes from the authenticated local
Actor, not directly from the caller's tenant header. The descriptor is not an
access grant: advertised endpoints still enforce their own permissions, tenant
capabilities and customer/carrier resource binding.

The backend owns business authorization, state transitions, durable data,
idempotency, audit and integration/provider gating. The portal BFF owns browser
sessions, OIDC login/callback, refresh, logout and logout-all. This contract does
not issue browser tokens, set a login cookie or implement those session routes.
Do not route browsers directly to provider credentials or expose tokens in
browser storage. Portal domains, identity clients and session certification are
separate acceptance gates; the manifest retains its blockers and explicitly
refuses to claim deployment or external-effect authorization.

The Pydantic manifest rejects unknown fields, disabled authorization requirements,
fake completion, browser-session ownership changes and empty blockers. Tests
validate its eight endpoint references against the actual composed OpenAPI,
including HTTP BearerAuth and the canonical migration head. HTTP regressions
check anonymous rejection, actor-derived tenant scope, no-store/correlation
headers and absence of tokens/cookies. Source-only discovery is not evidence of
production health, provider delivery or completed browser-session integration.

Additional PostgreSQL regressions extend the accepted identity migration tests
with per-table update/insert denial, transaction-context restoration, disabled
membership denial and repeatable bootstrap under non-owner freight_api. All test
fixture writes require the disposable loopback CI database. The accepted ordered
schema upgrade and historical migration files are unchanged.
