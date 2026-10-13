# Account-deletion write-hold primitive

This migration adds `account_deletion_holds` and the transaction-scoped primitive
in `backend/app/services/account_deletion_hold.py`. The first HTTP enrollment below
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
All multi-account integrations must acquire keys in sorted UUID order before any
row lock to avoid advisory/row-lock cycles.

The primitive does not attest ingestion quiescence, provider deletion, Redis
delivery, active WebSocket closure, or complete write-path coverage. Those are
explicit follow-up integration gates. Tests in `backend/tests/test_c436_deletion_hold.py`
use real PostgreSQL, observe actual blocking PIDs, and coordinate an admitted writer, an activation blocked behind
its shared lock, a fresh post-activation writer, rollback, isolation rejection, and
independent-account behavior.

## First HTTP enrollment (disabled)

`ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED` defaults to `false`. When enabled,
`get_current_user_for_write()` protects these actor-owned mutations:

- `PUT /alumni/profile`
- `POST /jobs`
- `DELETE /jobs/{job_id}` (the existing poster-only check still applies)

The dependency resolves the verified Firebase UID to a database user UUID in a
separate short read session, then closes that session before using the request
session. It binds `account_write_guard.py` only to a pristine request session.
An instance-local SQLAlchemy `after_begin` handler acquires the shared advisory
lock before application SQL, checks the current UUID/UID association, and reads
the active hold with a fresh READ COMMITTED snapshot. This repeats after each
commit or rollback, including a flush with pending ORM changes. A nested savepoint
keeps the outer transaction's admission lock. Ordinary suspension, legal and
membership authorization still apply.

The shared-lock wait is capped at five seconds and preserves a stricter existing
`lock_timeout`; the caller's timeout is restored after lock acquisition. Rejected
admission invalidates the connection and marks the session failed, so catching the
exception cannot permit another statement, flush or commit. An outer rollback
clears that marker; the next transaction must pass admission again.

An active hold returns HTTP 423 (`account_deletion_in_progress`); invalid admission
state returns a generic HTTP 503. Reads and account-data request status remain
available. A hold activated after a successful commit can block the response's
subsequent refresh; callers must not interpret every 423 as proof that no earlier
transaction committed. No background/provider work is added to these routes.

This flag neither activates a hold nor adds an activation endpoint. Ordinary
deletion intake still does not create a hold. Keep the flag disabled until migration
0044 and the runtime's required read permission have been separately reviewed and
applied. With the default flag off, these dependencies use the existing auth path
and do not query the new table, preserving compatibility with deployed schema 0043.

Before any production deletion, inventory and enroll the remaining HTTP writers
(including GET routes that consume keys), WebSocket writes, outbox/worker writes,
and writes by other actors that target the deleting account. Multi-account paths
need sorted admission keys before row locks; this pilot binds only one actor per
session. Drain in-flight uploads, exports, provider calls and analytics ingestion
and review the executor's hold-owner path. SQL admission alone proves none of
those external boundaries. No production migration, grants, enablement or deletion
is authorized by this source change.
