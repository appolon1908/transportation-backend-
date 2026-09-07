# Requested backend pull-request consolidation

The implementation is consolidated in PR #14 against development. This avoids
merging older snapshots over the later API, database, gateway and release fixes.
All source branches are retained; no force push or branch-policy change applies.

## Inputs preserved

| PR | Original implementation head | Treatment |
| --- | --- | --- |
| #1 | 872a6b0dcebd4ae2abedd90d51d153de1fc670a3 | Foundation ancestor retained |
| #2 | a88d1dc5893d53eac0292acc41f4f546426760cf | Persistent identity retained; missing pre-membership RLS context fix applied |
| #3 | 9334771ac481d48683e2c8189725d98d77441cf9 | Durable integrations ancestor retained |
| #5 | cc18c3eed3be6122f6d3d657b70de382d384ee0d | Carrier-compliance ancestor retained |
| #6 | 85e3124f82ab1a7d98e38f75d6005f2d02d8f912 | Current router-state guard retained; queue-health repair already present byte-for-byte |
| #7 | 2aa0508428d4c702dfe5d9f3edd13685a180a8e6 | Canonical API and secure deployment scaffold merged |
| #8 | 81b705cea5ee8689712fa47c4a584276cba9b6c7 | Audited, tenant-scoped, idempotent recovery merged |
| #10 | 559a9a0ae058d547725bd49020a589236af3cf3f | Profile already merged to main; carried into development unchanged |
| #11 | da1265d0b0b09f3d8f11b13f724e28fbea8864c6 | Contract-only registration carried unchanged, without inventing browser-session or domain authority |
| #14 | 5a2a52b82fa152f89aec53e63b5619d82c254d1a | Existing development integration and all governance files preserved |

The original identity router is byte-identical to the retained implementation.
The newer configuration/database/test files supersede older implementations and
preserve production rejection of development headers, locally granted RBAC and
row isolation. The additional identity change establishes transaction-local
tenancy before querying RLS-protected memberships; selection still does not grant
membership. New tests cover ordering, permission denial and a real PostgreSQL
freight_api role without SUPERUSER or BYPASSRLS privileges.

Merge acceptance requires the complete current candidate's native backend,
integrations, compliance/gateway, secure-scaffold and Required CI, plus review.
The older draft PRs can be retired as consolidated only after the candidate is
accepted; historical failing checks are not relabeled as successful. Source
promotion does not certify or activate runtime services. Storage providers,
browser-session authority, registered domains, release trust and live operations
retain their explicitly documented gates.
