# Account-deletion write-hold primitive

This migration adds `account_deletion_holds` and the transaction-scoped primitive
in `backend/app/services/account_deletion_hold.py`. It is intentionally unused by
HTTP, WebSocket, outbox, and deletion-executor paths until those paths are migrated
as one reviewed integration. The primitive alone does not freeze the product.

`activate_deletion_hold()` takes the per-user PostgreSQL transaction advisory lock
exclusively, refreshes the user and deletion-request columns under row locks, and
inserts one permanent active hold. Repeating the same request is idempotent; a
different active request is rejected. It never commits. The returned row is
provisional until the caller commits, and rollback removes an uncommitted hold.
There is no public activation or release route and no automatic expiry.

Every future user-owned write must call `admit_user_write()` before its first write.
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
use real PostgreSQL and coordinate an admitted writer, an activation blocked behind
its shared lock, a fresh post-activation writer, rollback, isolation rejection, and
independent-account behavior.
