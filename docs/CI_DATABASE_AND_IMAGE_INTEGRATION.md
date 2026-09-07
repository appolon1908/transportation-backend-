# Exact-source database and image integration

The combined candidate's native backend, integrations, compliance/gateway and
secure-scaffold CI passed. Its generic Required CI failed only because the
existing authenticated document-storage guard test reached PostgreSQL while
that job did not create a database (47 passed, 7 skipped, 1 failed).

Required CI and post-merge delivery validation now create the same digest-pinned
disposable PostGIS service used by native CI. The migration helper requires
GITHUB_ACTIONS=true, ENVIRONMENT=test and exact loopback URLs for all three
connection roles before applying the core and compliance schemas. The original
failing test is unchanged, and the previously skipped PostgreSQL tests now run
when the generic job supplies its database. Existing native role/FORCE-RLS
checks and provider-mocked integration tests remain independent requirements.
There is no production credential or runtime migration in this setup.

The root Dockerfile formerly started app.main, omitting integrations, compliance
and portal routes added by app.production_v4. It now includes both schema trees,
workers and the same reviewed explicit-mode entrypoint used by Dockerfile.v4.
The API requires its existing runtime credentials/gateway configuration, runs as
an unprivileged user and does not migrate at startup. Package source and README
are copied before installation so package construction sees the complete app.
The existing 8080 root-image port is preserved.

These changes do not replace the separate digest-pinned, scanned and attested
release workflow, prove fully reproducible Python dependencies, authorize
provider delivery or constitute staging/production certification. Native
backend and generic CI plus independent current-head review must certify the
combined candidate before promotion.
