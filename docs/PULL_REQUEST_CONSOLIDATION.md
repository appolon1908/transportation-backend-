# Backend pull-request consolidation

PR #14 was accepted into development as 7e79acb322d262476b8ca85ca4a9cae308acfae8
from source 6e12a69d57eccb27ad7e322a1634d779fd5a66be. Its five PR workflows and
post-merge development packaging passed. The later 3fc9921 candidate was not
part of that merge and must not be described as accepted by PR #14.

PRs #1, #3, #5, #7 and #8 were retired after their implementation ancestry was
verified. PR #6's remaining queue-health changes were verified byte-for-byte
against the accepted implementation. These older drafts were consolidated,
not individually promoted to their original targets. Their branches remain.
PR #10's profile was separately merged to main as 30970dbec8eb6687c1a56a571a8f54777cf7ccde.

## Identity follow-through

The original #2 branch had a distinct 0002b identity/RBAC migration which could
not be copied into the accepted linear chain without creating a second head.
Its isolation intent is forward-ported as 0006_identity_rbac_rls, a child of
0005_portal_workflows. The five identity/RBAC tables now have ENABLE and FORCE
RLS, matching permissive access and restrictive tenant boundaries, and both
membership and role must belong to the same tenant for an assignment.

The pre-membership transaction context fix is retained; neither caller-selected
tenants nor token role/permission claims grant local membership or permissions.
The bootstrap CLI supplies its tenant context before accessing the protected
membership and role tables. Tests exercise real freight_api reads/writes,
missing context, foreign tenants and cross-tenant role links without SUPERUSER
or BYPASSRLS privileges, alongside local-authority and migration-graph checks.

## Upgrade ordering and compatibility

Historical migrations, including the exact bytes of carrier compliance 0001,
are unchanged. The explicit migration command is python -m app.schema_upgrade.
For a new database it applies core through 0005, then compliance 0001, then core
0006. For an existing 0005/compliance installation it advances the core only.
It holds a PostgreSQL advisory lock, verifies both recorded heads, rejects
unknown/multiple/inconsistent states, never stamps or downgrades, and fails
closed if compliance is missing after core 0006 was independently applied.
API startup never invokes this command; it is used only by the existing
explicit migrate mode or disposable CI database setup.

Canonical health/release identity and active validation examples now use 0006.
An application rollback should normally retain the additive RLS hardening;
the explicit migration downgrade restores the previous 0005 policy state only
for a separately reviewed rollback. CI exercises downgrade/re-upgrade on a
disposable database, not a production host.

## Orbit scope

The unchanged #11 registration is contract-only. It registers no DNS, browser
client, live provider or session service. Its remaining browser-session/domain
and production-certification blockers remain explicit; source acceptance does
not represent those deployment prerequisites as completed.

No source-only change here authorizes production deployment, secret changes,
provider calls, freight activation or bypass of branch protections.
