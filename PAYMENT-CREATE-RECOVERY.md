# Payment creation after an uncertain response (c366)

Two deterministic PostgreSQL tests confirmed the audit candidate before changing
the handler. With the same reservation and idempotency key, request A returned a
usable client secret, then request B's delayed connection error marked that row
canceled. A cross-rail request returned 200 and obtained a second intent. In the
single lost-response case, the simulated provider created the first intent before
raising a connection error; the retry created a second reservation, key and intent.
These are demonstrated duplicate checkout paths, not executed duplicate charges.
The fixtures replace Stripe's SDK boundary; no provider network calls or funds
are involved.

Validation includes 21 new executed backend cases covering ownership conflicts,
lost responses, error classification, the 23/24/25-hour boundaries, timeout cleanup,
a shared cancel/retrieve budget, released CustomerSession pool slots, and mismatched
provider IDs. Existing payment invariants, signed c349 settlement, analytics and
cross-table dues guards passed their focused runs. The final identity/aging run
passed 25 tests with zero skips. Six executed mobile copy/status cases and
TypeScript 5.9.3 typechecking passed. These local checks do not replace CI or device
acceptance.

## Reservation ownership and recovery

The reservation remains durable before provider creation. Each resolving request
locks and reloads it with PostgreSQL `FOR UPDATE NOWAIT`; a competing owner gets
409 `payment_already_in_progress` before another create call. Ownership is acquired
again after the initial reservation commit and after a collision rollback, since
both release database locks. Existing live-reservation, payment-plan and ledger
uniqueness guards remain in force.

A provider exception cannot prove that no intent exists. Connection errors,
timeouts, server errors and idempotency conflicts produce 503
`payment_outcome_unconfirmed`. Typed validation/auth rejections produce 503
`payment_provider_rejected`, describing this request only. Both retain the open
reservation and original key: a rejection today cannot disprove an earlier lost
response. There is no new status or schema migration, and no automatic retry loop.

A same-rail retry with no stored provider ID uses the original key and the stored
amount/currency. At 23 hours from reservation creation, it stops calling create
and returns 409 `payment_reconciliation_required`. Age never releases a no-ID
reservation. Stripe documents that connection failures should retry the same key
and parameters, that 500 outcomes can be indeterminate, and that keys can be
pruned after at least 24 hours. The one-hour margin is a conservative application
policy. ([Error handling](https://docs.stripe.com/error-low-level),
[Idempotency](https://docs.stripe.com/api/idempotent_requests))

Once an ID is stored, retries retrieve it, including after the key-retention
window. Aged known intents release their reservation only after a confirmed
`canceled` status. Cancellation and retrieval responses must identify that exact
stored intent; another intent's status or secret cannot release or replace it.
Processing/succeeded responses remain honest response statuses;
the client does not open a fresh checkout for them. Stripe's cancellation API
defines the canceled state as preventing further charges, including the rare
processing cases it allows cancelling. Earlier secrets never become safe to
abandon just because a later request failed.
([Cancellation](https://docs.stripe.com/api/payment_intents/cancel))

## Resource and user-facing boundaries

One 15-second asynchronous budget covers all provider awaits while owning the
reservation: create, retrieve, and cancel/fallback retrieval. No detached provider
task outlives the handler intentionally. Timeout rolls back and releases the row
and connection while leaving the durable reservation intact. Cancellation cannot
recall an already-sent provider request; this is why later retries retain its key.

This rare money path deliberately holds a database connection during bounded
provider I/O to obtain simple serialization. It is a documented exception to
c355's broader provider/pool separation work; a durable lease/worker protocol would
be a separate design. This is not a global endpoint deadline: account/customer
setup precedes ownership, and CustomerSession creation follows an explicit
commit/release. Database availability and cooperative SDK cancellation still matter.

Payment dialogs use mapped copy, never raw API/provider detail. An unknown outcome
says it could not be confirmed and suggests retrying with the same payment method.
An aged unresolved attempt directs the member to the treasurer before another
payment. No unknown outcome is presented as proof that a payment failed.

Declined payment attempts are a separate lifecycle case: `failed` remains held
under c387. See [failed-intent lifecycle and coordinated migration rollout](PAYMENT-FAILED-LIFECYCLE.md).

## Remaining operational work

Unknown old rows need operator reconciliation against the original connected
account and reservation/key. Do not replace them based solely on age, an absent
client secret, or an empty provider search. Existing unmatched settlement events
remain subject to c349's quarantine review; this change does not invent ownership
from webhook metadata or add an automatic repair/refund endpoint. Account/customer
routing must remain consistent across retries, as in the current integration.

Deploy the reviewed code before exercising c11's separately owned test-mode card
and ACH device acceptance. No production deploy, migration, provider call, refund,
or live payment was performed by this work.
