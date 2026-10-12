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

The production GCS reader uses finite request timeouts and generation
preconditions. Enablement requires a separate reviewed service account with
list/get/delete permissions constrained to these prefixes, a verified account
write freeze, and an operator workflow that preserves the immutable snapshot.
This patch grants no IAM permissions, contacts no cloud provider, and does not
wire the adapter into runtime fulfillment.
