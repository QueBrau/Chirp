# Local message cleanup

Conversation leave now records an account-bound cleanup intent before contacting
the server. Sending, replay, synchronization and thread reads stop for that
conversation. A confirmed leave purges the native conversation records and the
app's outbox/failed-message metadata. A storage failure leaves a durable retry;
restart resumes it without needing the conversation to remain visible. Other
conversations and accounts are preserved. A fresh successful membership read
permits a legitimate rejoin of the same DM identifier. Native replay records
continue to prevent previously purged envelopes from restoring old plaintext.

Ordinary sign-out still locks the encrypted store and retains history.

## Account deletion completion

While a deletion request's owner can authenticate, the app obtains a read-only
completion capability from
`POST /me/data-requests/{request_id}/completion-token`. The request requires fresh
authentication and ownership. The capability binds the account and request,
expires after 90 days, and is stored only in the phone's device-only secure
storage. Refreshing request status renews it during its final week.

`POST /account-deletion/completion` accepts that capability in its JSON body. It
does not require the Firebase identity, which deletion removes before the final
database commit. A completion response requires the exact request, committed
deleted-user tombstone and successful database-tombstone step, with no remaining
fulfillment lease. It returns only the bound account/request identifiers and a
boolean. It grants no export, account mutation or provider access.

The signing subkey is derived from the existing `MEDIA_SIGNING_SECRET` with a
separate domain, and uses a full HMAC-SHA256 signature. No schema change or new
database privilege is required. Missing signing configuration fails closed.
Capability responses use `private, no-store`; the mobile calls refuse redirects
through Expo's native fetch transport (React Native's global fetch ignores that
option), and do not refresh Firebase credentials when checking completion.

The app checks saved receipts on launch, foreground and identity changes, and
once per minute while foregrounded. Checks share one in-flight run. A valid
completion for the captured account triggers its native-store wipe, metadata
cleanup and sign-out. A durable local account tombstone prevents an interrupted
cleanup or still-mounted session from recreating the account's local device.
Checks for one account do not sign out or purge a different signed-in account.

An expired capability, rotated signing key, generic 401, missing receipt,
unavailable backend or malformed response never substitutes for completion
proof. Those paths preserve local history. A local wipe failure retains the
receipt for an idempotent retry; some erasure may already have completed. The
account-data screen offers status retry when receipt preparation fails.
If the proof expires or its signing key rotates after Firebase deletion,
automatic completion verification is unavailable. This implementation does not
add a manual erasure UI or imply that restoring authentication is possible.
Receipt acquisition is a separate request after intake: if fulfillment removes
Firebase before that request or secure storage succeeds, automatic verification
is unavailable on that phone. Other phones must prepare their own receipts while
authenticated; a phone that never visits account-data has no saved proof. These
coverage limits remain on c452 and are not represented as complete erasure of
every installed device. Expired/missing receipt metadata is pruned when preparing
a new receipt so it cannot permanently exhaust the bounded pending index.

## Scope and release checks

This clears the active encrypted messaging store. Historical safety-number
markers written before their new per-account index may remain in Keychain when
their peer identifiers are unknown. Those markers contain public comparison
numbers, not messages or encryption keys. The old, unscoped SQLite message/outbox
schema has no active messaging writer; its independently tested purge primitive
is not used to guess account ownership or delete another account's rows.

The completion receipt proves the committed account tombstone, not expiry of
financial/safety/provider retention or deferred cleanup; those remain on c436.

Deploy the reviewed backend before distributing a mobile build containing this
integration. Old backends refuse receipt preparation; the app preserves the
request and presents retry. The backend change needs a Cloud Run redeploy using
the existing paired release procedure. It does not run an account deletion,
change maintenance schedules or enable a load test.

Automated checks execute the real engine, receipt coordinator, API adapters,
screen/hooks, and PostgreSQL-backed endpoints against controlled boundaries.
Installed native acceptance remains necessary: leave with queued messages,
restart during cleanup, confirm an authorized synthetic account deletion,
verify the removed account's history is gone, and verify ordinary sign-out and
another account's history are preserved. No phone result is inferred from CI.
