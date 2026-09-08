# Key directory resource limits

The mounted device and prekey endpoints store public, opaque bytes. Mobile crypto
remains parked, and no production E2EE library or algorithm is selected by this
change. These limits close the exposed storage and request-work abuse surface.

| Resource | Limit |
| --- | --- |
| Active devices per account | 5 |
| All retained devices per account, including revoked devices | 20 |
| Retained EC one-time prekeys per device, including consumed rows | 400 |
| Retained one-time Kyber prekeys per device, including consumed rows | 400 |
| Retained signed prekeys per device | 16 |
| Retained last-resort Kyber prekeys per device | 16 |
| Decoded identity / EC public key or signature | 1 to 256 bytes |
| Decoded Kyber public key | 1 to 4,096 bytes |
| Device label | 100 characters |
| Registration ID and prekey ID | 0 to 2,147,483,647 (PostgreSQL INTEGER) |

The existing 200-key limit per uploaded one-time batch still applies. Base64 is
validated both before and after decoding; an encoded-length check alone misses
oversized decoded values with the same padded length.

Registration accepts 10 attempts per account per hour. Replenishment accepts 60
attempts per account and 30 per account/device pair per ten minutes. These use the
existing Redis limiter in production, including its documented local fallback
during an outage. The database quotas remain authoritative when rate limiting is
degraded: registration holds the user's row lock until commit, and replenishment
holds the owned device's row lock until commit. Concurrent requests cannot both
spend the final stored-row slot through these HTTP routes.

Quota failures return HTTP 409 with a specific `*_limit_reached` detail. Throttled
writes return HTTP 429. Invalid input returns HTTP 422. Existing over-quota rows
are not deleted or silently repaired. A bundle request reads at most six active
devices and refuses an account above five before consuming any prekey; it never
silently drops recipient devices. Quota counts use a bounded subquery, and available
key counts refuse an oversized legacy pool rather than returning a truncated count.

Retained caps are lifetime ceilings under this initial policy. Consumed and
superseded rows continue to occupy their slots. Reaching a cap requires an explicit
retirement or device lifecycle decision; a client must not retry indefinitely or
create devices repeatedly to evade the error. Deploying these guards does not make
unlimited replenishment available or establish safe crypto operation.

Before enabling mobile crypto, c347 still needs algorithm-specific key lengths and
signature validation under the selected protocol, a reviewed device replacement
and retirement contract, and repeat-replenishment evidence beyond the retained
ceiling. The current database has no unique `(device_id, key_id)` replay tombstone
constraint. It must not be assumed that deleting consumed rows preserves one-time
use: the eventual contract must prevent reuploading previously consumed key IDs or
material. Device private keys and message history must remain outside public
directory cleanup. Existing atomic consume-on-read and last-resort fallback
behavior are preserved by this change.

The limits apply to these HTTP writers. Direct database writes and pre-existing
oversized records require an operational review; this change performs no production
queries or cleanup. Migration 0035 adds indexes for account device lookups, retained
prekey counts, and latest signed/last-resort selection; the existing available-pool
partial indexes remain. Apply it after the coordinated 0033 and 0034 migrations.
These are regular transactional index builds, so schedule production migration with
the existing deployment procedure and assess table size and write-lock impact first.
This code change does not apply the migration to production.
