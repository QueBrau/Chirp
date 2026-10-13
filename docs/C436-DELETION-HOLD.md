# Account-deletion write-hold primitive

This migration adds `account_deletion_holds` and the transaction-scoped primitive
in `backend/app/services/account_deletion_hold.py`. The HTTP enrollment below
is disabled by default. WebSocket, outbox, and deletion-executor paths are not yet
enrolled. The primitive and partial enrollment do not freeze the product.

`activate_deletion_hold()` takes the per-user PostgreSQL transaction advisory lock
exclusively, refreshes the user and deletion-request columns under row locks, and
inserts one permanent active hold. Repeating the same request is idempotent; a
different active request is rejected. It never commits. The returned row is
provisional until the caller commits, and rollback removes an uncommitted hold.
There is no public activation or release route and no automatic expiry.

Every user-owned writer must acquire admission before its first database operation.
Explicit callers can use `admit_user_write()`; enrolled HTTP callers use the
session guard below so each new transaction checks admission automatically.
That call acquires the same per-user advisory lock in shared mode and performs a
fresh active-hold query. It must be the first database operation in the transaction;
pending ORM changes are rejected. The lock remains until the caller commits or rolls
back. Deletion's internal journal and final cleanup will need a separately reviewed
hold-owner path; ordinary routes must never bypass the admission check.

`activate_deletion_hold()` is also a first-operation API. A transaction that already
holds a shared admission lock cannot upgrade to the exclusive activation lock; it
must roll back and retry from a fresh transaction. Callers must apply their own
bounded lock timeout and rollback on cancellation or timeout. PostgreSQL releases
the transaction advisory lock on rollback, but the caller still owns cleanup of its
SQLAlchemy transaction. The primitive does not inspect SQL issued before the helper
was called, so route enrollment and ordering remain required for enforcement.

The key is a stable signed 64-bit SHA-256-derived value from the canonical UUID.
Hash collisions can only serialize or over-block unrelated accounts; they cannot
permit a write. The protocol requires PostgreSQL `READ COMMITTED`, because that
is the default isolation level with a fresh statement snapshot. Repeatable-read
and serializable callers fail closed rather than using a potentially stale snapshot.
The session guard acquires deduplicated advisory keys in ascending signed-bigint
order before any application SQL or row lock. All future multi-account
integrations must use that same order to avoid advisory/row-lock cycles.

The primitive does not attest ingestion quiescence, provider deletion, Redis
delivery, active WebSocket closure, or complete write-path coverage. Those are
explicit follow-up integration gates. Tests in `backend/tests/test_c436_deletion_hold.py`
use real PostgreSQL, observe actual blocking PIDs, and coordinate an admitted writer, an activation blocked behind
its shared lock, a fresh post-activation writer, rollback, isolation rejection, and
independent-account behavior.

## HTTP enrollment (disabled)

`ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED` defaults to `false`. When enabled,
`get_current_user_for_write()` protects these actor-owned mutations:

- `PUT /alumni/profile`
- `POST /jobs`
- `DELETE /jobs/{job_id}` (the existing poster-only check still applies)
- `POST /devices` and `POST /devices/{device_id}/prekeys`
- `POST /v2/devices`, `POST /v2/devices/{device_id}/approve`,
  `POST /v2/devices/{device_id}/keys`, and `DELETE /v2/devices/{device_id}`
- `POST /campuses/{campus_id}/chirps` and `DELETE /chirps/{chirp_id}`
- `POST /conversations/{conversation_id}/leave`
- `POST /messages/{message_id}/receipts`

`POST /auth/legal-acceptance` uses the same hold guard with the legal-status
identity dependency. It preserves suspension checks without requiring the
acceptance the request is recording. Policy/status reads remain unguarded reads.
Chirp creation uses a guarded campus dependency; the existing campus and current
verification checks still apply. The organization routes listed below use
guarded membership and role dependencies; other callers of the ordinary
dependencies are not implicitly enrolled.

The dependency resolves the verified Firebase UID to a database user UUID in a
separate short read session, then closes that session before using the request
session. It binds `account_write_guard.py` only to a pristine request session.
An instance-local SQLAlchemy `after_begin` handler acquires the shared advisory
lock before application SQL, checks the current UUID/UID association, and reads
the active hold with a fresh READ COMMITTED snapshot. This repeats after each
commit or rollback, including a flush with pending ORM changes. A nested savepoint
keeps the outer transaction's admission lock. Ordinary suspension, legal and
membership authorization still apply.

The entire shared-lock acquisition has a five-second budget and preserves a
stricter existing `lock_timeout`; the caller's timeout is restored after all locks
are acquired. Rejected
admission invalidates the connection and marks the session failed, so catching the
exception cannot permit another statement, flush or commit. An outer rollback
clears that marker; the next transaction must pass admission again.

An active hold returns HTTP 423 (`account_deletion_in_progress`); invalid admission
state returns a generic HTTP 503. Reads and account-data request status remain
available. A hold activated after a successful commit can block the response's
subsequent refresh; callers must not interpret every 423 as proof that no earlier
transaction committed. No background/provider work is added to these routes.

## Consuming key requests

`GET /users/{user_id}/prekey-bundle` and `POST /v2/keys/claim` also mutate data:
they consume another account's one-time keys. Their guarded dependencies resolve
the actor and target account set in a separate short read session before binding
the request session. The v2 preflight applies the existing reachability rules
before any target-hold check, preserving the identical refusal for an unknown
device and a device owned by an unreachable account. Ordinary auth, suspension
and legal checks apply to preflight and to the guarded request.

The bound account set is immutable and includes the actor. Every transaction
acquires all its shared locks, rechecks the actor's Firebase identity, verifies
target existence and rejects any active hold. The handler revalidates ownership
and reachability before consuming keys; a changed account set fails without
consumption. A new request must perform a fresh preflight rather than expanding
the lock set inside a transaction. Directory and key-count reads remain reads.

The current key-request schema bounds device claims to ten. The general guard
supports up to 257 accounts, matching conversation creation's 256 invitees plus
the creator. All shared locks still use one five-second budget, not five seconds
per participant.

## Conversation and message SQL

The same disabled flag enrolls `POST /conversations`,
`POST /conversations/{conversation_id}/messages`, and
`POST /v2/conversations/{conversation_id}/messages`. Creation preflights the
existing chapter, reachability and block rules before binding the requested
participants, then repeats authorization in the guarded request transaction.
After a conflicting DM insert rolls back, authorization runs again before reuse.

Message admission binds the complete active roster, including members excluded
from delivery by block filtering. Before new message, encrypted-leg or delivery
intent rows are written, the handler locks the conversation row and rereads the
active roster. A changed roster fails closed and requires a fresh request rather
than extending the lock set mid-transaction. DM reuse and conversation leave use
the same parent-row ordering; reuse validates its actual members before clearing
`left_at`. Leave remains an actor-only mutation so another member's hold does not
prevent departure.

An owned v2 replay found in preflight binds only the actor and returns its stored
message before recipient device-set and block eligibility checks. A recipient's
hold therefore does not prevent this read-only retry; the actor's hold still
does. The handler verifies the original conversation and exact encrypted legs.
If the stored message disappears between preflight and the handler, the immutable
actor-only scope cannot fall through to a new multi-account insert. The history
read routes remain available. Admission does not imply that a previously accepted
message vanished.

Local deletion cleanup already marks the deleted account's conversation
memberships left. Remaining group members therefore bind the remaining active
roster and can keep sending after cleanup; existing shared history is retained.
Reusing a DM cannot reactivate a held member. These are SQL transaction boundaries,
not a guarantee that earlier Redis, push or WebSocket delivery has drained.
Immediate delivery and the outbox worker still require separate durable operation
fencing and drain integration before production deletion can be enabled.

## Organization and safety preferences

The same disabled flag also enrolls these mutations:

- Chapter creation and joining: `POST /chapters`, `POST /chapters/join`.
- Chapter updates and approval: `PATCH /chapters/{chapter_id}` and
  `PATCH /chapters/{chapter_id}/moderation-approval`.
- Membership updates: `PATCH /chapters/{chapter_id}/members`.
- Invite creation and revocation: `POST /chapters/{chapter_id}/invites` and
  `POST /chapters/{chapter_id}/invites/revoke`.
- Meeting creation, updates and deletion, plus attendance upserts:
  `POST /chapters/{chapter_id}/meetings`,
  `PATCH` and `DELETE /chapters/{chapter_id}/meetings/{meeting_id}`, and
  `PUT /chapters/{chapter_id}/meetings/{meeting_id}/attendance`.
- Reporter/blocker-owned safety preferences: `POST /moderation/reports`,
  `POST` and `DELETE /moderation/blocks`, and
  `POST` and `DELETE /moderation/blocks/by-chirp/{chirp_id}`.

Membership updates bind both the actor and the target user. A separate short
preflight checks ordinary authentication, active chapter membership, president
authority and target membership existence before checking any target hold. The
guarded request repeats authorization and locks the target membership, validating
the exact bound scope before mutation. A hold prevents reactivation or promotion
after cleanup and prevents new role terms for the deleted account.

The other listed routes bind only the actor. Organization meeting and attendance
records are retained history, and an attendee's hold must not freeze another
authorized officer's work. The existing attendee eligibility and role policies
still apply. Incoming reports and blocks are retained safety records owned by
the reporter/blocker; their target's hold does not prevent these actions. In
particular, anonymous blocking does not query or expose the author's hold state.
Cleanup removes outgoing blocks, and the held actor cannot recreate them.
Reads retain their existing authorization and stay outside write admission.

Moderator report resolution, content removal and account suspension are not
enrolled by this safety-preference change. Finance, other shared-content writers,
background dispatch and external operations also remain separate review gates.

## Remaining rollout gates

This flag neither activates a hold nor adds an activation endpoint. Ordinary
deletion intake still does not create a hold. Keep the flag disabled until migration
0044 and the runtime's required read permission have been separately reviewed and
applied. With the default flag off, these dependencies use the existing auth path
and do not query the new table, preserving compatibility with deployed schema 0043.

Before any production deletion, inventory and enroll the remaining HTTP writers
(including GET routes that consume keys), WebSocket writes, outbox/worker writes,
and writes by other actors that target the deleting account. Multi-account paths
need sorted admission keys before row locks; consuming key requests and the enrolled
conversation/message writes and membership updates bind multiple accounts.
Drain in-flight uploads, exports, provider calls and analytics ingestion
and review the executor's hold-owner path. SQL admission alone proves none of
those external boundaries. No production migration, grants, enablement or deletion
is authorized by this source change.
