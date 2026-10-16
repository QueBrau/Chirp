# Account-deletion write-hold primitive

This migration adds `account_deletion_holds` and the transaction-scoped primitive
in `backend/app/services/account_deletion_hold.py`. The HTTP enrollment below
is disabled by default. WebSocket and outbox paths use separate default-off flags
and lifetime registries described below.
The deletion executor now requires the exact active hold and a drained operation
registry before nonterminal work and again before local cleanup/tombstoning.
No production caller or activation workflow is wired, and this gate does not prove
complete producer coverage. The primitive and partial enrollment do not freeze the
product.

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
back. The deletion executor is an internal hold owner: it checks the exact active
user/request hold and registered-operation drain before provider work, then repeats
the hold/drain check under the exclusive gate held through the tombstone commit.
Ordinary routes must never bypass the admission check.

`run_deletion()` requires a dedicated executor-owned `READ COMMITTED` session
with only prior unlocked reads. It rejects pending ORM changes and an unsupported
isolation level before provider calls, but cannot detect earlier flushed/raw-SQL
writes or pre-acquired locks. The caller must satisfy that session contract.
The initial hold/drain check precedes request-row locking. Final cleanup takes the
exclusive advisory gate, then the user and request row locks, and retains the gate
through commit; reused provider receipts commit their previous request fence first.
Validated completed requests remain read-only and do not require a new hold.
The executor never activates or releases a hold.

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
Immediate delivery and the outbox worker use the separate delivery registry below
when its flag is enabled; production rollout and drain remain prerequisites.

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

## Polls, lineage and moderator actions

The same disabled flag enrolls four poll mutations:
`POST /chapters/{chapter_id}/polls`,
`POST /chapters/{chapter_id}/polls/{poll_id}/vote`,
`POST /chapters/{chapter_id}/polls/{poll_id}/close`, and
`DELETE /chapters/{chapter_id}/polls/{poll_id}`.
All bind only the actor. Cleanup removes that actor's secret ballots, and a held
actor cannot recreate them. Another officer can still maintain a chapter's
retained poll even if its creator is held. The poll and delivery-intent SQL is
guarded; subsequent Redis dispatch and sweeps use the separate opt-in delivery
registry below. With delivery admission disabled, a post-commit hold can prevent
a later outbox outcome update without proving an earlier publication did not occur.

Four lineage mutations are also enrolled: family creation, edge creation or
replacement, edge deletion, and little-member confirmation under
`/chapters/{chapter_id}/lineage`. Creation/replacement preflights active chapter
membership, e-board authority and both endpoint accounts before binding the actor
and both endpoints. Authorization and current body endpoints are checked again
under the immutable guard. An altered target set fails closed. With admission
enabled, a completed deletion tombstone is not a reusable ghost placeholder. The false-default path
retains the existing validation behavior. Family creation, edge deletion and
confirmation bind only the actor; an unheld officer can remove a retained edge
to another held account. Existing read and little-only confirmation rules remain.

Five remaining moderator mutations are enrolled:
`PATCH /moderation/reports/{report_id}`,
`POST /moderation/chirps/{chirp_id}/remove`,
`POST /moderation/content/remove`, and
`POST /moderation/users/{user_id}/suspend` or `/unsuspend`.
Report resolution, content removal and suspension bind only the moderator/admin
actor, preserving other users' safety actions against held targets. Anonymous
content removal does not inspect its author's hold. Removal updates only a still
visible row; a concurrent cleanup or removal wins without its reason being
overwritten or a second successful-removal audit being recorded.

Unsuspension restores access, so it binds both the admin and target account.
Ordinary admin authorization and target existence are established before a hold
can be disclosed. The guarded request reauthenticates the admin and locks and
refreshes the target before checking the bound scope and clearing suspension.
Suspension also locks and refreshes its target row, but can still enforce a safety
restriction during deletion. Reads keep their original dependencies.

## Profile, campus participation, feeds and events

The same disabled flag enrolls 18 additional SQL mutation routes:

- Profile updates: `PATCH /auth/me`.
- Campus verification: `POST /auth/campus-verification` and
  `POST /auth/campus-verification/redeem`.
- Private votes: `PUT /campuses/{campus_id}/house-ballot` and
  `PUT /chirps/{chirp_id}/vote`.
- Feed posts: `POST /chapters/{chapter_id}/posts`,
  `POST /campuses/{campus_id}/posts`,
  `PATCH` and `DELETE /chapters/{chapter_id}/posts/{post_id}`, and
  `DELETE /posts/{post_id}`.
- Feed interactions: `PUT` and `DELETE /posts/{post_id}/likes`, and
  `POST /posts/{post_id}/comments`.
- Events: `POST /chapters/{chapter_id}/events`, `PATCH /events/{event_id}`,
  `POST /events/{event_id}/cancel`, `PUT /events/{event_id}/rsvps`, and
  `POST /events/{event_id}/invites`.

These bind the actor, except event invitations also lock their immutable recipient
candidate set. Post editing remains author-only; its ordinary authorization is
checked before admission and again in the guarded session. Post interactions lock
the visible post before writing, and authorized deletion uses a conditional update
that cannot overwrite a concurrent cleanup's removal state. Locked post reloads
refresh the identity map, including after media finalization. Profile updates and
feed post/comment/like creation refresh response fields before the final commit;
a hold beginning immediately afterward cannot turn that committed mutation into
a failed response refresh. Media write failures still report orphaned objects.
Other users' retained
likes/comments and an anonymous Chirp author's hold do not become hold-status
disclosures. Cleanup removes all of the caller's house ballots, alongside existing
Chirp/poll votes, while leaving other voters' rows and organization history intact.

Event creation, updates, cancellation and RSVPs refresh their response data before
commit, so a post-commit hold cannot turn a completed mutation into a failed SQL
refresh. Events, invites and RSVPs are retained shared organization history. A held
host does not prevent another authorized officer from maintaining the event.

Invitations preserve the existing atomic batch and 500-recipient limit. Ordinary
authentication, active membership, management permission and unknown-user checks
run before the request session binds all candidates. The filtered guard acquires
globally sorted shared gates for the actor plus every candidate before application
SQL. An active actor hold still rejects the write; held recipients and completed
deletion stubs are silently omitted, as blocked recipients already are. The handler
rechecks authorization, the exact candidate set and blocks, then inserts eligible
recipients in one transaction. Eligibility is specific to that SQL transaction and
recomputed after commit or rollback; stale eligibility or a changed scope fails
closed. This filtered batch API has a separate 500-candidate bound and does not
expand the strict 257-account guard used by messaging.

These route guards fence SQL. Avatar/feed finalizers also have the durable
operation tracking described below. Verification email uses its own registered
send boundary below. Redis rate-limit work and analytics emissions can still
outlive their SQL transactions; those producers and ingestion need separate
drain integration. An active hold blocking a later
SQL write does not undo an earlier external effect.

## Financial records and payment plans

The same disabled flag enrolls seven finance SQL mutations:

- `POST /chapters/{chapter_id}/dues-cycles`
- `POST /chapters/{chapter_id}/ledger`
- `POST /chapters/{chapter_id}/spend-approvals`
- `POST /chapters/{chapter_id}/spend-approvals/{approval_id}/decide`
- `POST /chapters/{chapter_id}/dues-cycles/{cycle_id}/plans`
- `POST /chapters/{chapter_id}/dues-plans/{plan_id}/installments/{seq}/record-payment`
- `POST /chapters/{chapter_id}/dues-plans/{plan_id}/cancel`

All require actor admission and preserve their existing role requirements. New
payment plans additionally bind the member receiving the plan. Ordinary chapter,
dues-admin, cycle and active-member checks run before binding, then repeat in the
guarded request transaction. The actor and target set cannot change during the
request, and completed deletion tombstones cannot receive new plans.

The other six routes bind only the actor. A target's deletion hold must not prevent
another authorized officer from recording money already received, appending a
ledger correction, deciding a retained spend request or canceling an existing
plan. Financial and organization records remain retained history; this change
neither erases ledger entries nor claims that outstanding obligations disappear.
Existing installment, plan and ledger conflict rules still apply. Response data
is loaded before the final commit so a newly activated hold cannot turn a
successfully committed financial action into a failed response refresh. Read and
CSV-export routes retain their existing authorization.

The seven routes above use SQL admission. The separate payment enrollment below
tracks outbound Stripe work; signed webhooks continue accounting for money already
captured under the existing idempotent settlement rules. An actor's deletion hold
must not discard a financial event or prevent another officer maintaining retained
organization history.

## Outbound payment operations (migration 0049)

With HTTP admission enabled, `POST /payments/connect/onboarding-link` and
`POST /payments/dues/{cycle_id}/intent` authorize their actor and domain scope,
then commit a durable `payment_request` parent before their first mutation.
This request marker replaces a transaction-spanning shared advisory lock: the
request may finish admitted work during a hold, while the executor waits for it.
Individual Account, Customer, PaymentIntent create/cancel, AccountLink and
CustomerSession calls register owned child attempts before provider I/O.
Read-only Stripe retrievals stay covered by the parent marker and final response
guard; they do not create another remote effect.

The final actor/hold and current membership/resource binding checks run under a
fresh, short SQL guard before payment capabilities are returned. The existing
dues-row NOWAIT serialization and 15-second provider budget remain unchanged.
Unknown child outcomes block deletion even after their request drains. Only an
exact same-effect confirmation and atomic domain binding can reconcile older
uncertain attempts; time passing never clears them. Journal rows intentionally
have no reservation foreign key, so child registration cannot deadlock on its
own caller's locked dues row. Parent completion and child registration share the
parent row lock to prevent late registration after request completion.

See `C436-ROLLOUT.md` for stable creation keys, retry bounds, reviewed grant
preparation and capability retention boundaries. This source enrollment does not
stop legacy writers, revoke previously issued capabilities, activate the feature
flag or authorize a production deletion.

## Privacy requests

`POST /me/data-requests` requires actor write admission when `kind=export`, before
its request transaction can create an export request or artifact. The dependency
retains the privacy route's fresh authentication and access for suspended accounts;
ordinary product-access or legal-acceptance gates are not added. Export generation
is local SQL work within the admitted transaction. The response, including artifact
expiry, is materialized before commit.

Deletion intake and idempotent deletion-request retries remain privacy control
operations and do not require ordinary write admission. They create or reuse the
blocked, journaled request; they do not activate a hold or execute deletion. Request
status and existing export downloads retain their existing owner and authentication
checks during a hold. These read paths do not create new artifacts. System expiry
of old artifacts remains a separate bounded retention job.

## Avatar and feed media operations (migration 0045)

With the same admission flag enabled, avatar updates, chapter/campus post creation
and post editing register each media finalizer before its storage worker starts.
Migration 0045 adds `account_external_operations` and account-scope links. A fresh
short transaction uses the existing sorted shared account gates, revalidates
identity and holds, and commits the operation plus its exact scope. No request
or journal database connection is held across storage I/O.

Each operation has a private owner token and a stable digest of the validated
caller-owned temporary object name, unique per actor and operation kind. Retrying
an existing effect returns HTTP 409 (`media_operation_already_started`) without
another provider call, including after success. This is attempt deduplication,
not a cached successful HTTP response. A new upload has a new temporary object
name. Tokens, filenames, image data and signed URLs are not exported or logged by
this registry; only the digest, account scope, kind, state and timestamps are kept.
The minimal control rows remain through local account cleanup.

The wrapper awaits the actual threaded finalizer. A successful return permits a
fresh token-fenced transition from `running` to `finished`. Any raised exception
is conservatively `uncertain`; this includes errors for which the current storage
adapter does not provide separate proof of no remote effect. A failed journal
update or process crash leaves `running`, which also blocks deletion. Neither
age nor a lease deadline clears these states, and an uncertain result cannot be
upgraded by blindly retrying. Provider readback and a separately reviewed recovery
procedure remain necessary to resolve abandoned or ambiguous work.

Request cancellation shields and drains the worker, including repeated cancels,
before propagating cancellation. It does not stop a thread or claim that remote
work stopped. A successful storage result still passes the route's ordinary
guarded authorization before any profile/post write: if SQL wins the shared gate,
hold activation waits for that transaction; if the hold wins, SQL is rejected.
No owner-token bypass permits a late domain write. A permanent object whose SQL
write is rejected is an orphan, so deletion must enumerate owned media after the
operation drain, including unreferenced objects.

`inspect_registered_operation_drain()` requires the exact active deletion hold
under an exclusive account gate and reports running/uncertain counts for that
account. Its `registered_operations_drained` value describes this registry only;
it does not attest complete producer enrollment or trigger cleanup. Signed PUT
capabilities can remain usable after issuance and are explicitly not covered by
finalizer completion. Payment, analytics ingestion, bootstrap and production
runner/activation wiring remain separate requirements. The separate WebSocket and
delivery enrollment below covers only work registered while each flag is enabled.
Executor counts only cover registrations actually present.

## Campus verification email operations (migration 0046)

Migration 0046 widens the existing registry's kind constraint to include
`campus_verification_email`; it adds no table or grant. A downgrade refuses to
remove that kind while email-operation rows remain. The migration never deletes
those safety records to make a downgrade succeed.

With admission enabled, campus verification still commits its challenge before
sending, then registers the send in a fresh admitted transaction. The effect key
is a digest of that challenge's UUID and the scope is its authenticated owner.
The registry does not retain the email address, code, body or provider message ID.
A hold that wins before registration prevents the send. If registration wins,
the operation remains visible to drain inspection while its provider call runs.
Neither the request session nor the registry retains a SQL connection over the
email await. A repeated operation is refused without a second send; an ordinary
resend creates a fresh challenge and retains the existing rate limits.

A normal provider return records `finished`, meaning the local send was accepted
by the configured provider. It does not prove inbox delivery or that the provider
has deleted its queued message or delivery records. Exceptions and cancellation
record `uncertain`; repeated cancellation drains that journal update before
propagating. A failed update or process crash leaves `running`, which also blocks
drain. No transport error authorizes automatic replay or proves no remote effect.
Normal completion can update the control journal during a hold, without bypassing
ordinary authorization for domain writes. Challenge retirement after a send error
still uses the guarded request session; a hold can refuse that later write.

With the flag disabled, email follows the original path and does not query the
registry. This slice does not send a real email or enable deletion. Provider-side
delivery and retention/readback, plus the remaining producer work, still need
separate acceptance.

## WebSocket lifetime registration (migration 0047)

`ACCOUNT_DELETION_WS_ADMISSION_ENABLED` is a separate flag, defaulting to `false`.
Migration 0047 adds `account_ws_connections`, with 32 database-enforced slots per
account. After ordinary authentication, suspension and legal checks, registration
takes the exclusive account advisory gate, rechecks identity and active holds,
then commits a running row before accepting the socket or opening Redis. The
short SQL transaction ends before transport work. A hold or exhausted slots
rejects the connection; failed or canceled registration never permits acceptance.
A lost commit response may leave a running row, which continues to block drain.

The existing suspension watcher also checks for a hold, using its shared advisory
gate before other SQL. Its normal polling interval is 24–36 seconds, followed by
bounded reconciliation and teardown; this is not immediate revocation. The row
blocks deletion while that connection is still running. Registration and polling
use `WS_RECONCILE_SECONDS`; child termination, Redis closure and the final registry
write each have `WS_CLEANUP_SECONDS`, and ASGI close uses `WS_CLOSE_SECONDS`.
No SQL connection is held across the socket lifetime or network awaits.

Confirmed teardown requires all owned tasks to finish, Redis subscription closure
to return, and either peer disconnect observation or successful local ASGI close
issuance. It deletes only the matching running connection ID, account, slot and
owner token. A reused slot has a new ID/token, so an old handle cannot remove it.
This avoids permanent history for every reconnect. It does not prove a peer
received a close frame or erased frames already delivered or cached.

A failed close, a child that ignores cancellation, or failed Redis teardown leaves
an `uncertain` row. A failed/ambiguous final write can leave `running`; both states
block executor entry and final cleanup under the exclusive hold gate. Unresolved
rows consume slots and never expire or become safe through blind retry. Recovery
requires separately reviewed evidence that the exact connection can no longer
run; this change supplies no recovery command. Cleanup must not sweep these rows.
Its retained coverage classifies safety evidence, not a claim that successful
deletion leaves socket rows: successful deletion requires none for that account.
Downgrade to 0046 refuses any nonempty connection table instead of erasing evidence.

The flag is captured once per connection. Flag-off connections and older instances
are unregistered; enabling the flag cannot enroll them retrospectively. Before
deletion activation, rollout must drain all such instances/connections and prove
every receiving service uses the reviewed configuration. Disabling the flag again
requires stopping deletion execution until those unregistered connections drain.
An empty table alone cannot prove this coverage. The separate delivery flag below
does not replace socket drain. Signed uploads and analytics ingestion also remain
separate gates.

The local tests use real PostgreSQL locks/migrations, a real Redis subscription,
and controlled ASGI transports for cancellation and failure cases. They do not
attest production routing, a real phone session or peer-side frame delivery.

## Queued delivery registration (migration 0048)

The user-approved policy suppresses queued notifications to a held, deleted or
missing recipient while continuing delivery to other recipients. Shared message
history remains available under existing authorization. This applies only when
`ACCOUNT_DELETION_DELIVERY_ADMISSION_ENABLED=true`; the default is `false`.

Migration 0048 adds `account_delivery_attempts` and a nullable outbox claim token.
The live and sweeper paths reconstruct the event from committed source data and
use the same admission protocol. A short preflight reads at most 128 recipient IDs.
A fresh READ COMMITTED transaction acquires sorted exclusive account gates before
the queue row lock, revalidates that prefix and filters held/deleted recipients.
It registers at most eight attempts, with 32 database-enforced unresolved slots per
account, before any transport call. Remaining recipients stay queued; deferred
prefix entries rotate behind the untouched tail. A full account does not prevent
delivery to others. Registration and hold activation serialize on the same gate.

Each attempt has an immutable identity and owner token, independent of the outbox
row. Poll coalescing or source deletion cannot erase its evidence. The claim removes
registered recipients from the pending array and commits before transport I/O;
no SQL connection is held over Redis/dispatcher awaits. V1 ciphertext and v2
content-free message hints retain their existing wire shapes.

Dispatch uses at most eight concurrent singleton-recipient calls and the configured
`outbox_dispatch_timeout_s` budget, followed by a 250 ms cancellation drain.
Confirmed recipient success is recorded independently of a sibling failure. A
successful result means local transport acceptance, not device receipt. Success
deletes only the exact running attempt; failure, cancellation or an invalid result
leaves uncertainty. Cancellation-resistant work remains strongly owned and blocks
drain even after its caller returns. Late termination cannot turn uncertainty into
success. The terminal attempt changes and claim-token-fenced queue update share
one bounded transaction; stale workers cannot alter a newer queue claim.

Unstarted recipients remain eligible for later sweeps; full-slot deferrals back
off. A 30-second queue lease permits a later worker to advance the remaining tail,
but never replays a registered recipient or expires its evidence. Running and
uncertain attempts block both executor entry and final tombstoning indefinitely.
No automatic retry, expiry or recovery command clears them. Review of the exact
effect is required before any recovery. Completed attempts do not accumulate as
permanent delivery history. The registry stores no payload or ciphertext.

Admission/listing SQL has a five-second budget; terminal SQL has a two-second
budget. Repeated cancellation retains ownership of bounded finalization and still
propagates cancellation. Downgrade refuses any attempt row or nonnull claim token.
The new claim column is excluded from the legacy ORM mapping, so the flag-off
dispatcher can still use the older schema. The deletion executor itself requires
0048 and SELECT on the registry for its unconditional drain checks.

Before deletion activation, drain every old/flag-off API and sweeper process and
verify the delivery flag on every dispatcher. Enabling it cannot register prior
in-flight work. Socket lifetime registration and drain remain independently
required for frames already published. Real push is still a log-only stub; this
increment makes no provider-delivery or phone-verification claim.

## Remaining rollout gates

This flag neither activates a hold nor adds an activation endpoint. Ordinary
deletion intake still does not create a hold. Keep the flag disabled until migration
0044 through 0049 plus the runtime's exact hold-read and registry
permissions have been separately reviewed and applied. Keep the separate WS and
delivery flags off until their own rollout requirements above are satisfied.
With all three flags off, request/socket/delivery paths retain their existing
behavior without new registry queries, preserving compatibility with schema 0043.

Before any production deletion, inventory and enroll the remaining HTTP writers
(including GET routes that consume keys), remaining worker writes,
and writes by other actors that target the deleting account. Multi-account paths
need sorted admission keys before row locks; consuming key requests and the enrolled
conversation/message writes, membership updates, lineage creation/replacement and
unsuspension bind multiple accounts. Event invitations use filtered multi-account
admission without weakening the other routes' strict target holds.
Drain in-flight uploads, exports, provider calls and analytics ingestion. SQL
admission and the executor's registry count prove none of the uncovered external
boundaries. No production migration, grants, enablement or deletion is authorized
by this source change.
