# Secure document API implementation

The document feature branch builds on development 67156f9296dd48cc9f31fce7562fab1ae60c6fd5.
It replaces the four unconditional storage-error handlers, preserves the old
fail-closed default, and adds authenticated metadata and byte-download APIs.
Browser login/logout remains the portal BFF's responsibility; no domain, storage
bucket, identity client, AWS credential or scanner is created by this change.

## Client workflow

1. POST `/api/v1/documents/upload-sessions` with bearer identity, selected tenant,
   `Idempotency-Key`, and JSON containing `load_id`, `filename`, `purpose`,
   `content_type`, `size_bytes` and lowercase hex `checksum_sha256`.
2. PUT the exact bytes to the returned short-lived URL with every returned
   header. Upload signatures cover length, MIME, SHA-256, conditional create,
   KMS encryption, document/tenant identity and the configured retention date.
   A browser sets Content-Length from the Blob body rather than JavaScript.
3. POST `/api/v1/documents/{id}/confirm` with `expected_version` and a new
   idempotency key. The server pins the S3 version, reads bounded bytes,
   validates length/hash/container signature, then uses ClamAV INSTREAM.
4. GET `/api/v1/documents/{id}` for status. Only AVAILABLE documents can be
   attached with POST `/api/v1/loads/{load_id}/documents` or `/pod`, supplying
   `document_id` and `expected_version`. POD additionally requires purpose POD.
5. GET `/api/v1/documents/{id}/download`. Authorization is checked again and
   the exact scanned version is read and rehashed. Bytes are attachment-only,
   application/octet-stream, no-store and nosniff; no download URL is exposed.

The existing load list endpoint is bounded to 200 metadata rows and never
returns object keys. Portal metadata and evidence selectors exclude pending,
rejected and quarantined uploads, and do not expose object coordinates.

## State and safety contract

`PENDING_UPLOAD -> VERIFYING -> AVAILABLE -> ATTACHED` is the clean path. Invalid content is
REJECTED and malware is QUARANTINED; neither may be downloaded or attached.
Scanner/provider outages return 503 and leave the prior durable state unchanged.
Unknown scanner responses are failures, never clean verdicts. Expired sessions
return 410 on confirmation; retrying an upload key does not renew its lifetime.

Mutations have command idempotency, short per-key transaction locking, row locking,
expected versions, audit records and outbox events. Verification events use
`document.verification_completed.v1` and explicitly include the resulting status;
a completed verification does not imply a clean result. Signed upload URLs are
created after commit and never stored in command records, audit or outbox.
Provider failures are returned without provider exception text or credentials.

Document bytes remain associated with the original load. Cross-load attachment
is refused. Tenant predicates apply to every metadata query; the new table has
FORCE RLS and matching restrictive boundaries, including parent-document tenant
matching. Portal actors additionally need local permissions, an active matching
portal binding and both global and tenant capability gates. Customer access to
shared loads is refused rather than disclosing another customer's documents.

## Explicit deployment requirements

`DOCUMENT_STORAGE_ENABLED` defaults to false. Opt-in configuration requires:

- `DOCUMENT_S3_BUCKET`, `DOCUMENT_S3_REGION`, and a concrete
  `DOCUMENT_KMS_KEY_ARN` (not a mutable key alias).
- `DOCUMENT_CLAMAV_SOCKET`: an absolute local Unix socket. Socket permissions,
  malware database updates/freshness, clamd health and StreamMaxLength must be
  certified by the operator. Default scan timeout is 30 seconds.
- SDK-managed AWS credentials or an attached workload identity, provisioned
  outside source. Do not give browsers AWS credentials or bucket-list access.

The adapter requires versioning, all four S3 public-access blocks, and S3 Object
Lock. Uploads request COMPLIANCE retention. **Compliance-mode object retention
cannot be shortened by this application; use a reviewed retention period.** The
30-day default is a software default, not a legal retention recommendation.
Downloads and attachments stop at the stored retention deadline. Physical
purging, lifecycle expiration, legal-hold policy, bucket CORS and private bucket
policies remain operator-managed. The app does not perform provider deletion.
Do not grant public/cross-account reads or bypass permissions to upload clients.

Accepted byte formats are PDF, PNG and JPEG, capped at 10 MiB. Signature checking
is format identification, not a complete parser or content-disarm-and-rebuild
service. Antivirus and download-as-attachment are additional defenses, not a
claim that arbitrary uploaded content can be rendered safely. The app does not
render documents inline, execute macros, or accept archive upload formats.

## Schema and rollback

0007_document_storage is additive after 0006. The additive
0008_document_verification_lease migration adds the verification claim fields. Existing migration files remain
unchanged. Use the explicit `python -m app.schema_upgrade` entrypoint; fresh
installations still apply historical compliance at core 0005 before the current core head.
There are no API-startup migrations. The API database role receives only
SELECT/INSERT/UPDATE on document_objects; ingress and worker roles receive none.

Rollback by disabling DOCUMENT_STORAGE_ENABLED and rolling back the app while
retaining additive metadata and S3 objects. A schema downgrade drops new metadata
and must not be run against retained production documents without a reviewed
backup/recovery plan. Source CI is not staging or production certification.

## Verification

Database-free tests cover configuration, exact byte checks, signed upload
headers, immutable reads and real local-socket ClamAV framing. HTTP/PostgreSQL
tests cover the clean flow, idempotency, concurrent confirmation, quarantine,
scanner outage, tenant/permission denial, expiration, attachment scope, audit,
outbox and non-BYPASSRLS policies. CI substitutes S3 and scan results; no real
bucket, malware database or provider integration is certified by those tests.

## Primary protocol references

- AWS S3 PutObject: https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html
- AWS conditional writes: https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html
- ClamAV INSTREAM: https://docs.clamav.net/manual/Usage/ClamdProtocol.html

## Verification transaction lifecycle

Confirmation reserves its idempotency key and stores a random verification token
with a five-minute expiry in a short transaction. It commits before S3 inspection,
bounded reads and ClamAV scanning, releasing the request connection. Finalization
rechecks authorization, document version, token and expiry in a new transaction,
then atomically records the result, audit entry, outbox event and idempotent reply.

Concurrent requests receive 409 while the claim is active. Provider failures reset
only the current claim to PENDING_UPLOAD; a process crash leaves an expiring claim
that a later confirmation can reclaim while the upload session remains valid.
An older scanner result cannot overwrite a replacement claim. Cancellation or
process loss never publishes a clean result. No background worker or scheduler is
required to authorize a retry; clients must retry after the lease expires. If the
upload session has also expired, create a new upload session instead.
