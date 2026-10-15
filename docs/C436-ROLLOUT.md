# Account data rollout boundary

This change provides authenticated account-data request intake, owner-scoped status, and a downloadable export of the supported server-side records. Export artifacts are immutable, explicitly marked `partially_completed`, and served only through the authenticated download route with `Cache-Control: private, no-store`.

The current export covers the account profile, memberships, authored content and interactions, events and attendance, alumni and lineage records, owned devices and message metadata, caller-owned message receipts, payment and ledger rows linked to the account, role terms, moderation/report actions authored by the caller, legal policy versions plus the caller's acceptance records, and organization/payment authority declarations made by the caller. Ciphertext remains opaque; key material and credential-shaped fields are redacted.

The export explicitly omits provider-held Firebase/Auth, payment, email, object storage, logging, and backup copies; plaintext that is never stored by the server; prekey byte material; dormant client-only SQLite data; and outbox delivery payloads or recipient identifiers. Role terms are currently limited to terms changed by the caller. Installment details are represented by caller-linked payment-plan rows; provider payment records remain omitted. Shared organization, safety, append-only financial, provider-backup, and logging records have documented retention reasons. User-block export includes only blocks initiated by the caller and never exposes the other account's identifier. New tables or columns require an allowlist review before they can enter an export.

Deletion requests are accepted and tracked with a durable provider-step plan (migration 0043). The request route remains blocked until an operator supplies all five reviewed provider adapters: payment provider, media storage, email/logs/backups, account-linked analytics, and Firebase/Auth. Each step has a stable idempotency key, attempt count, sanitized outcome, and manual-review state; missing or ambiguous provider confirmation cannot reach the database tombstone. The injected executor preserves shared organization, safety/legal, authored encrypted-message, and append-only financial rows, removes the caller's server-side prekey rows for revoked devices, and removes direct Firebase/email identity linkage only after every provider step is confirmed. The default deployment has no configured adapters and therefore cannot perform deletion; operator credentials, grants, and adapters must be reviewed before any enablement. Export artifacts have a bounded maintenance CLI (`python -m app.jobs.account_data`) that previews by default and requires explicit `--delete`; this release does not enable a production schedule.

Rollout order:

1. Apply migration 0043 only after the c438 migrations 0040–0042 are present. Review the API role's request/artifact and `account_fulfillment_steps` SELECT/INSERT grants needed to create and report a blocked plan. Review the separate operator workflow role for narrowly scoped request/step lease updates and owned-prekey DELETE grants; grant no API cleanup DELETE permission and no client role direct table access.
2. Deploy backend models, migrations, auth freshness dependency, request/status/download routes, export service, and focused database tests before shipping either client screen. Before accepting an export request, run the read-only c454 privilege preflight against the actual API database connection with `--expected-role chirp_api`; it verifies the inherited export column ACLs (including column-level grants), real zero-row ORM selects, and request/artifact write privileges. See `docs/C454-ROLLOUT.md`.
3. Verify suspended users can still read their own request status while sensitive creation/download remains fresh-auth protected; verify owner isolation and rate limits. Re-run the same preflight after any schema, role, or deployment-template change; a missing column grant must block release rather than surface as an export-route 500.
4. Ship the mobile settings screen and web support fallback after backend behavior is observable.
5. Treat deletion as manual processing until each provider step has a durable journal, an idempotent retry contract, and provider-specific confirmation.

Sensitive request creation and downloads check both Firebase token revocation and
recent authentication. Verify the API identity can read Firebase account status
before release; existing certificate-only token verification does not prove that
permission. No new cloud permission is granted by this code change.

Firebase SDK calls run in a worker thread with a fixed timeout. Timeout or caller
cancellation drains that worker before the provider result is returned. This can
extend beyond the nominal 30-second SDK timeout, and it cannot prevent the
separate five-minute orchestration lease from expiring if the SDK itself hangs.
The outcome remains sanitized and uncertain; a lease deadline is not proof that
external work stopped. The step lease is not intentionally released while the
Firebase worker is running.

The c458 executor repair applies the no-retry boundary to every provider step.
An inherited running step or a previously attempted unsuccessful step is held
for review, including legacy rows whose older error text did not say the outcome
was unknown. Running state, timestamps, lease data, stored provider responses and
unknown-outcome markers also block retry when a legacy attempt counter is zero.
Firebase retains `firebase_outcome_unknown`; the other providers use
`provider_outcome_unknown`. An exception or invalid confirmation after invocation
cannot authorize a second call. A stale runner cannot overwrite the durable
outcome or reach local cleanup. Cancellation propagates without proceeding to
later providers or the account tombstone; existing adapter worker-drain rules
still apply.

Never-attempted pending work can resume after missing adapters are supplied.
Already-succeeded steps are reused only after their stored receipt is validated.
Resolving an uncertain attempted step requires a separately reviewed operator
procedure that confirms the original worker stopped and verifies provider state;
this source change adds no marker-clearing command or automatic retry path.
The read-only preflight reports both unknown-outcome markers and ambiguous legacy
attempts without disclosing provider error bodies. These changes do not configure
adapters or enable deletion.

## Stripe customer adapter boundary

The Stripe customer adapter is a dormant operator component. It is not imported
by the request executor and makes no provider call by default. Its input is a
bounded, caller-owned set of `chapter_stripe_customers` rows joined to the exact
chapter connected account. It rejects malformed IDs, duplicate or foreign
ownership, and active payment plans or in-flight payment intents before any
Stripe call. A stable step key is accepted for the surrounding journal; Stripe
customer deletion itself is retried only through exact readback.

The adapter requires complete `Subscription.list(status=all)` results with no
unknown status or pagination remainder. Active, pending, or malformed payment
activity requires operator review. It requires the exact connected account and
customer ID plus `object=customer` and boolean `deleted=true` on both the delete
response and the subsequent readback. A 404, string-valued `deleted`, mismatched
ID/type, timeout, or lost response is a blocked provider result rather than a
success. Stripe's retained deleted-customer shell and local ledger, payment
intent, plan, installment, and chapter mapping history remain retained; this
adapter does not claim whole-provider or financial-record erasure.
After confirming mapped customer deletion/readback, it returns the fixed
`stripe_deleted_customer_history` retention code to the journal. An empty local
mapping returns an empty list, without claiming an observed retained customer.

Provider adapters may also return a `retained_with_reason` list containing only
the fixed code allowed for that provider step: `stripe_deleted_customer_history`,
`gcs_soft_deleted_versions`, `email_delivery_records`, `operational_logs`, or
`database_backups`. The deletion journal validates and durably stores those codes
in the step receipt, then includes their fixed explanations in the final request
status alongside local retention reasons. A provider step cannot claim another
step's retention code. This is an acknowledgment of a declared retention
boundary; it is not evidence of deletion, measured expiry, backup expiration,
or whole-provider erasure. Unknown, duplicate, nested, or free-form values block
the step and are never persisted.

Before enablement, operators must independently review the ownership snapshot,
connected-account credentials and quiescence, pending payment obligations, and
the separate Firebase, media, email/log, and backup adapters. No production SDK
call, automatic wiring, or provider permission is included in this release.

The adapter follows Stripe's documented Customer deletion and retrieval behavior
and connected-account scoping: [delete a Customer](https://docs.stripe.com/api/customers/delete),
[retrieve a Customer](https://docs.stripe.com/api/customers/retrieve), and
[Connect authentication](https://docs.stripe.com/connect/authentication).

## Durable hold primitive (migration 0044)

Migration 0044 adds the dormant account-deletion hold primitive after 0043.
See `docs/C436-DELETION-HOLD.md`. It grants no permissions. The first HTTP admission
enrollment covers the explicitly listed profile, job, device/key, Chirp,
receipt/leave and legal-acceptance mutations in that document, behind
`ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED=false`. It checks each request-session
transaction, including transactions after commit/rollback, without activating any
hold. Consuming key requests additionally bind the actor and authorized target
accounts before request SQL; their immutable scope is revalidated before key
consumption. Existing reads and privacy status remain available. Leave the flag off until
migrations 0044 through 0048 and the runtime hold-read and registry grants have
separate review and rollout.
Conversation creation/reuse and legacy/v2 message SQL also bind their participant
scope. Conversation-row locks serialize enrolled membership changes with sends,
and the roster is rechecked before new message, leg and outbox-intent writes.
The scope supports the existing 256-invitee creation limit. Local cleanup removes
the deleted account from the active roster while preserving shared history.
The listed chapter/member, meeting/attendance and reporter/blocker mutations are
also enrolled. Member changes guard both actor and target; retained organization
and safety records use actor admission, preserving other users' authorized work
and anonymous target privacy. Poll SQL mutations, lineage mutations and the
remaining moderator actions are also enrolled as listed in the hold document.
New/replacement lineage edges bind both endpoints; unsuspension binds the target
whose access would be restored. Other officers can still remove retained edges,
maintain polls and perform safety removals against held targets. Conditional
content removal preserves a concurrent cleanup's redaction reason and avoids
duplicate successful-removal audit rows.

Profile, campus verification, feed/comment/like, Chirp vote, house ballot and
event/invite/RSVP SQL mutations are also enrolled. Event invitations silently
exclude held/deleted recipients under a separate atomic 500-candidate guard;
actor admission and existing strict target admission remain unchanged. Local
cleanup now removes the caller's house ballots, with event history explicitly
retained. Review the cleanup executor's DELETE permission on `house_ballots`
before rollout; this source change adds no database grant.

The seven finance mutations listed in the hold document are also enrolled. New
payment plans bind the actor and target member; other officers can still maintain
retained financial history for a held member. The ledger remains append-only,
existing payment/conflict rules remain in force, and response reads occur before
commit. This does not enroll Stripe provider operations or settlement webhooks.

Export creation is also enrolled before its request transaction and artifact
generation. Fresh-auth privacy access for suspended users is preserved. Deletion
intake/retries, request status and existing downloads remain available under their
privacy authorization rules; intake still cannot activate a hold or run cleanup.

Unlisted writers still require review, including payment/webhook paths and
upload capabilities. Avatar/feed finalizers are registered and awaited as described
below; their caller SQL still requires fresh admission. Campus verification sends
are registered, but provider-side queued delivery and retention are not drained;
analytics emission still needs external fencing/drain. Bootstrap identity
recreation also needs its provider-boundary review. WebSocket enrollment has its
own disabled flag and rollout gates below. Outbox dispatch now has separate
default-off enrollment described below; analytics ingestion remains un-enrolled.
No production caller or activation/run
workflow is wired. A complete release must enroll every writer and drain provider
operations, uploads, exports and analytics ingestion before an operator uses
provider cleanup. The executor gate is necessary but does not establish that
quiescence or authorize running the migration or deletion in production.

The journal now requires five providers, including account-linked analytics,
and a sixth local tombstone step. Successful analytics receipts preserve the
request UUID, snapshot digest and BigQuery retention metadata. Those fields do
not replace the operator's immutable reviewed snapshot or external readback.


## External media-operation registry (migration 0045)

Migration 0045 follows 0044 and adds the operation and account-scope tables only;
it creates no grants, schedule, executor or production hold. The API needs reviewed
SELECT/INSERT on both tables and UPDATE limited to the operation state/completed
columns before the existing HTTP admission flag can be enabled. Scope links,
effect identity and owner tokens are immutable through the service API and must
not receive routine update/delete privileges. Confirm the exact production-role
SQL separately; nothing in this source change applies it.

The enabled path enrolls actual avatar and feed storage finalizers, deduplicates
attempts and waits for their real worker completion without retaining a SQL
connection. Provider errors or abandoned running records block drain pending
review. No automatic lease expiry, takeover or recovery command is provided.
Keep the flag false on deployed schema 0043; that path performs the original
finalization without querying either registry table.

Before using this registry for deletion, integrate all external producers, drain
signed-upload capabilities, and take the immutable media snapshot after drain.
Review a provider-readback recovery path for ambiguous records, including failure
before a remote write. Local registered-work counts alone cannot attest complete
quiescence. The executor now requires the matching active hold and a zero count of
running/uncertain registered operations before provider cleanup and again before
the local tombstone. This necessary gate does not establish complete producer
coverage or prove that an issued signed upload capability has stopped working.

## WebSocket connection registry (migration 0047)

Migration 0047 adds a dedicated connection table with 32 slots per account; it
does not change the permanent provider-operation journal or grant privileges.
`ACCOUNT_DELETION_WS_ADMISSION_ENABLED=false` keeps existing socket behavior.
The enabled path registers before accept/Redis, rejects active holds, and closes
registered connections when the periodic watcher observes a hold. Confirmed local
teardown deletes its fenced row; running/uncertain rows block both executor gates
and consume a slot indefinitely until separately reviewed recovery. Downgrade
refuses a nonempty table. See `C436-DELETION-HOLD.md` for timing and failure semantics.

Before any activation, review the actual serving runtime identities and schema.
The WS runtime needs existing identity/legal reads, hold SELECT, and SELECT/INSERT,
UPDATE restricted to `state`, and DELETE on `account_ws_connections`. Registration
does not need UPDATE on `users`. The deletion executor needs SELECT on the new
table for both drain checks, even when the WS flag is disabled. A process that
serves both roles needs their union; do not copy permissions to unrelated roles.
Prepare and review exact grant SQL separately. This source change applies none.

Roll out the schema before running the new executor. Drain all old or flag-off
socket instances before enabling deletion execution: the flag is captured at
handshake and cannot enroll existing connections. Verify API/WS routing and every
receiving revision, then establish complete producer drain through the remaining
delivery/payment/upload/analytics rollout. Flag rollback again creates unregistered
connections and requires stopping deletion execution. Neither empty registry counts
nor the read-only operator preflight establish complete drain; that preflight
does not inspect live socket counts and always reports `ready_for_deletion: false`.

Local PostgreSQL/Redis and controlled ASGI tests establish source behavior only.
They do not prove peer receipt of close frames, device-cache removal, production
rollout or whole-ticket acceptance. No migration, grants, deployment, activation,
provider request or account deletion is included in this source increment.

## Delivery attempt registry (migration 0048)

Migration 0048 follows 0047, adding `account_delivery_attempts` and
`delivery_outbox.claim_token`. It changes no grants or production configuration.
`ACCOUNT_DELETION_DELIVERY_ADMISSION_ENABLED=false` preserves legacy immediate
dispatch and sweeper behavior. The claim column is excluded from legacy ORM
mapping so ordinary outbox INSERT/SELECT operations work before 0048. The new
executor still requires 0048 and SELECT on the attempt registry at both drain gates.

Before enabling delivery admission, review each actual API/worker runtime's
identity/user and hold reads, existing message/outbox access, SELECT/INSERT/DELETE
on `account_delivery_attempts`, UPDATE limited to its `state`, and SELECT/UPDATE
on the outbox `claim_token` column. Attempt identity, account, slot, source and
owner token are immutable through the service API. Prepare exact role grants
separately; this source increment applies none. Roll out migration and reviewed
permissions before the new executor or any enabled dispatcher.

The approved policy suppresses held/deleted recipients and retains others in the
queue. New attempts commit before transport calls. Confirmed completion removes
the exact running attempt; running or uncertain attempts block both executor
gates without expiry or automatic replay. Poll coalescing cannot erase them.
Both attempt transitions and queue completion are fenced; an old worker cannot
release a newer claim. See `C436-DELETION-HOLD.md` and `DELIVERY-OUTBOX.md` for bounds
and cancellation semantics. Downgrade refuses any attempt or outstanding claim
token; do not delete safety evidence to force a downgrade.

All API and worker dispatchers must use the same reviewed flag configuration.
Drain old/flag-off processes and their in-flight publishes before deletion can
rely on registry counts. Disabling admission again requires stopping deletion
execution. Separately establish socket drain for published frames and finish
payment, upload, analytics and activation/runner integration. Empty local tables
alone do not establish production quiescence. Local PostgreSQL/Redis regressions
prove source behavior; they do not attest device receipt or real push delivery.
No deployment, provider request, migration application, grant or deletion is
included here.

## Verification-email registration (migration 0046)

Migration 0046 adds `campus_verification_email` to the existing operation-kind
constraint. It preserves existing rows and privileges. Downgrade fails atomically
if email-operation rows remain; it does not discard safety evidence. Apply this
schema change only in a separately reviewed rollout before enabling admission.

The registered send uses a digest of the committed verification challenge ID and
the authenticated owner's scope. No raw address, code, body or provider message ID
is added to the registry. A hold prevents new registration, while already-admitted
sends remain visible until their local provider call finishes. Errors and canceled
calls remain uncertain; failures recording completion leave running work. Both
states block the registry drain without automatic expiry or retry. Provider
acceptance is not proof of inbox delivery or provider-side retention expiry.

The same default-off flag preserves the deployed email path without registry
queries. No real email, production migration, permission change or deletion is
included. The executor enforces its hold and registry-count prerequisite when
called, but no production caller or activation workflow is wired. Other
producer/ingestion boundaries still require integration before a complete deletion
can be accepted.
