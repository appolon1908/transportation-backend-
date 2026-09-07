# Identity, tenancy and RBAC contract

Canonical repository identity: `freight-platform-backend`.
The current GitHub URL `transportation-backend-` is a legacy alias and must not be used for image names, deployment service names or API metadata.

## Authentication boundary

- OIDC Authorization Code + PKCE is used for human sessions; client credentials are used for approved machine principals.
- JWT signature, issuer, audience, algorithm, expiry, issued-at, optional not-before, key id and optional authorized-party claims are validated.
- Signing keys are fetched asynchronously, cached, and may use a bounded stale window during a temporary identity-provider outage.
- Token role and permission claims are not authoritative. The token supplies identity and tenant selection only.
- Local identity is bound by `issuer + subject`.
- Every request resolves an active local principal, an active tenant membership, tenant roles and database permissions.
- Unknown identities, disabled principals and missing memberships fail closed.

## Tenant selection and database isolation

Production requests select a tenant with `X-Tenant-Id` or an approved token selection claim. The selected tenant grants no authority by itself; membership is verified locally.

The request session sets transaction-local PostgreSQL settings:

- `app.tenant_id`
- `app.actor_id`

Migration `0002_identity_tenancy` enables row-level policies on material tenant tables. Production must use a non-owner application database role because PostgreSQL table owners normally bypass RLS. Worker access must use a separately reviewed role; do not share an unrestricted owner credential with the API.

## Bootstrap

Run migrations, then create the first tenant administrator from a trusted operator shell:

```bash
python scripts/bootstrap_identity.py \
  --tenant-slug codestra-freight \
  --tenant-name "Codestra Freight" \
  --issuer "https://auth.codestra.co/realms/freight" \
  --subject "<keycloak-subject>" \
  --display-name "Ralph Appolon" \
  --email "<operator-email>"
```

The script is idempotent for the tenant slug and `issuer + subject`. It creates the permission catalog, tenant role templates, membership and admin assignment. It does not create passwords or store bearer tokens.

## Administrative API

- `GET /api/v1/auth/context`
- `GET|PATCH /api/v1/admin/tenant`
- `GET|POST /api/v1/admin/organizations`
- `GET|POST /api/v1/admin/users`
- `POST /api/v1/admin/users/{principal_id}/identities`
- `GET|POST /api/v1/admin/memberships`
- `PUT /api/v1/admin/memberships/{membership_id}/roles`
- `GET|POST /api/v1/admin/roles`
- `PUT /api/v1/admin/roles/{role_id}/permissions`
- `GET /api/v1/admin/permissions`
- `GET|PATCH /api/v1/admin/capabilities`
- `GET /api/v1/admin/audit`

All material writes retain the foundation command contract: idempotency, transaction, audit and outbox event in one database commit.
