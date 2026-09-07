# Runtime Verification Evidence

This directory is reserved for **redacted, read-only** runtime inventory evidence. It is not a deployment workspace and must never contain credentials, private keys, certificates, tokens, secret values, database passwords, `.env` files, or decrypted configuration.

A runtime-evidence commit may change only:

```text
deploy/runtime/*.json
docs/deployment/evidence/*
```

The deployment preflight verifies that the evidence commit follows the reviewed application source SHA on `main` and that no application, workflow, migration, Compose, or gateway file changed between those SHAs.

Each inventory record should identify:

```text
inventory timestamp and timezone
approved target environment
host identity and ownership
verifier identity
read-only commands or data sources used
verified runtime paths and filesystem metadata
verified Docker network names
verified immutable image IDs/digests
bound ports and proxy ownership
backup/restore evidence reference
redactions applied
HOST_CHANGED=NO
DEPLOYMENT_PERFORMED=NO
```

After redaction, calculate the SHA-256 digest of the immutable evidence file. Put that digest in the runtime manifest as:

```text
verification.evidence_reference=sha256:<64 lowercase hexadecimal characters>
```

Runtime evidence expires after 168 hours for preflight purposes. A later preflight requires a new read-only inventory and a new reviewed evidence commit.
