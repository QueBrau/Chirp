# C436 media provider boundary

```text
provider: media_storage
scope: tmp/{user_id}/, posts/{user_id}/, avatars/{user_id}/
mode: dormant operator adapter
```

The dormant GCS adapter is in `backend/app/services/account_provider_gcs.py` and
is not imported by the deletion executor. It requires an operator-issued,
short-lived, user-bound freeze receipt and a repeatable-read database session
before it creates a snapshot. It inventories all three approved account
prefixes, including orphan historical objects, under bounded list and deadline
limits. Every object is bound to its exact generation and the snapshot digest.

Before any delete, the adapter re-lists every approved prefix and blocks on an
extra object, generation change, malformed object, incomplete listing, expired
freeze, foreign subject, unsupported owned database URL, or provider error. It
then performs generation-conditional deletes and readback. A retry accepts an
already-absent reviewed object, while a reappearing or changed generation
requires manual review. The returned receipt is scoped to the exact user and
prefixes and does not claim deletion of backups, logs, CDN copies, shared
records, or unsupported bucket locations.

The production GCS reader refreshes the remaining timeout on each listing page,
disables SDK retries and uses generation preconditions. A 120-second budget
prevents starting further work. An in-flight synchronous SDK request cannot be
aborted by cancelling its Python await: timeout or cancellation drains that
worker before returning an uncertain result, so the wait may extend beyond the
budget by the in-flight request. A lost response never proves that the remote
delete did not happen. Retry the same reviewed snapshot after readback; never
expand its scope to include a replacement generation.

The separate operator process must start with
`DISABLE_GCS_PYTHON_CLIENT_OTEL_BUCKET_METADATA=true`. The reader refuses to
construct a client without this setting and uses a dedicated client rather than
the API's shared storage client. The installed Google SDK otherwise starts
background bucket-metadata requests with independent retries; explicit object
request timeouts alone do not bound those requests. No new bucket-metadata
permission is needed. The setting is checked again before each SDK operation;
the adapter never changes the process environment itself. Offline tests execute
real SDK pagination, reads and conditional deletes, and reject any background
thread start, including during 404 handling.

Enablement requires a separate reviewed service account with
list/get/delete permissions constrained to these prefixes, a verified account
write freeze, and an operator workflow that preserves the immutable snapshot.
The freeze receipt is an explicit operator attestation, not a cryptographic
proof or an implemented account-freeze mechanism. It defaults to unverified.
The operator must stop uploads/finalization, allow already issued upload URLs
to expire, prevent new references to the affected objects and hold this state
through the final readback. Local account cleanup happens after provider steps;
this adapter does not assume that content has already been hidden. Snapshotting
checks current and historical owned references and bounded foreign references,
including equivalent URL forms and orphan objects. Unknown owned URLs or an
incomplete scan require manual review. The database snapshot is not a lock on
future application writes.

Success proves absence of live objects only. Backups and caches have independent
retention. Construction requires an explicit `MediaRetentionReview`, verified by
the operator against the bucket's current versioning and soft-delete policy.
Unknown policy or enabled versioning blocks this adapter because it does not
inventory noncurrent versions. When soft delete is enabled, the success receipt
includes the fixed `gcs_soft_deleted_versions` code understood by the fulfillment
journal; expiry is not measured. A reviewed policy with neither versioning nor
soft delete returns no provider-version retention code. These are operator
attestations, not automatic policy discovery; preserve the reviewed policy and
snapshot evidence together. This component neither changes bucket policy nor
claims permanent byte erasure.

The implementation follows [GCS generation preconditions](https://docs.cloud.google.com/storage/docs/request-preconditions)
and [strong object listing/read consistency](https://docs.cloud.google.com/storage/docs/consistency).
This patch grants no IAM permissions, contacts no cloud provider, and does not
wire the adapter into runtime fulfillment.
