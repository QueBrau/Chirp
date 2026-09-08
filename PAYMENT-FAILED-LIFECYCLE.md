# Declined payment attempts retain their reservation (c387)

The audit reproduced a second usable checkout after a signed payment failure
webhook: Chirp marked the first reservation `failed`, excluded it from its live
query, and created another intent. The deterministic provider fixture left the
first intent in `requires_payment_method`, with its original client secret still
usable. Both card-to-card and card-to-ACH reproductions failed before the repair.
They demonstrate two confirmable checkout intents, not an executed duplicate
charge. Every provider operation in these tests is simulated; no funds moved.

Stripe documents that a failed payment attempt returns the PaymentIntent to
`requires_payment_method`, where it can be retried. Cancellation is the terminal
state that invalidates further payment attempts. A failure event alone cannot
justify abandoning its intent. ([PaymentIntent lifecycle](https://docs.stripe.com/payments/paymentintents/lifecycle))

## Held reservation contract

`open`, `failed`, and `succeeded` all hold the same `(dues_cycle_id, user_id)` slot.
The status vocabulary does not change. `failed` records the latest observed
unsuccessful attempt; it does not mean the provider intent is unusable.

- A same-rail checkout retrieves the stored intent and returns its current status
  and original client secret. It does not mint another intent or key after a
  decline. A processing or succeeded provider result is not presented as a fresh
  checkout.
- A different rail receives 409 `payment_already_in_progress` while the attempt
  remains held. A new installment plan receives 409 `payment_in_progress`.
- An aged failed intent follows the existing cancellation confirmation path. Only
  a response identifying the stored intent with status `canceled`, or its verified
  cancellation webhook, releases the slot. Unknown or processing outcomes retain
  it. An existing canceled reservation permits a fresh reservation and key.
- A late failure changes only `open` or `failed`; it never revives a canceled row
  beside a newer checkout or demotes a succeeded row. A verified later success
  still goes through c349's binding, transactional receipt and ledger safeguards.
  Conflicting historical captures retain the existing reconciliation behavior.

Migration 0036 extends `uq_dues_intent_live` and **both** cross-table guard functions
with the same held-status set. Their existing advisory lock serializes a failed
reservation and a competing active plan. The checkout and plan readers match the
new predicates. No row is automatically reopened, deleted, selected as a winner,
or canceled by this migration.

The [c366 uncertainty rules](PAYMENT-CREATE-RECOVERY.md) remain: an unknown no-ID
reservation retains its original key; create retries stop at 23 hours, known IDs
are retrieved, and the reservation owner uses NOWAIT locking with one bounded
provider-I/O budget. The named provider-ID uniqueness backstop remains distinct
from other database failures. Provider calls still route through the chapter's
configured connected account. That association is not a reservation snapshot;
an out-of-band account change requires operator reconciliation against the
original account, not release after a retrieval error.

## Coordinated rollout and existing data

Mixed old and new payment writers are unsafe. Old code excludes `failed` in its
reads; old schema permits a new reservation or plan beside it. The new application
requires 0036, and 0036 alone does not provide coherent behavior for old handlers.
Do not use an ordinary rolling money deployment while both versions can serve it.

1. Pause payment-intent creation, payment-plan writes, and Stripe webhook handling
   across **every** old backend instance and any other money writer. Keep webhook
   delivery retryable during the pause; never acknowledge an event without its
   durable processing. Drain requests and database transactions touching either
   `dues_payment_intents` or `dues_payment_plans`, including readers. The migration's
   `ACCESS EXCLUSIVE NOWAIT` locks conflict with reads as well as writes, so a busy
   table fails immediately instead of waiting behind traffic.
2. Run migration 0036 only after that coordinated pause. It locks both tables before
   preflight and retains the locks through installation/commit. Preflight counts
   obligations with multiple held intents and obligations with both a held intent
   and an active plan. It aborts on either condition with nonsecret counts and a
   reconciliation instruction. Failed preflight rolls back all schema changes;
   there is no automatic financial-data repair.
3. If preflight fails, keep money writers paused. An authorized operator must
   identify the conflicting obligations privately and inspect each original
   provider account/intent, ledger, and plan. Determine actual settlement and
   confirm provider cancellation where appropriate before changing reservations.
   Do not infer safety from the newest row, failure status, age, or an empty search.
   Multiple real captures require explicit reconciliation; migration cannot refund
   or decide which capture belongs on the ledger. Retry preflight after reviewed
   corrections. Existing single failed reservations need no data rewrite and
   become held immediately on successful migration.
4. Bring up only the matching reviewed backend revision, verify the schema and
   isolated test-mode acceptance, then resume money traffic and webhook delivery.
   Process deferred events through the normal signed and transactional path.

Downgrade restores the previous index and guard predicates while preserving every
financial row. It also restores the unsafe failed-status semantics: keep payment
traffic paused on rollback until a coherent reviewed version is ready. This work
runs migrations only in disposable local test databases. Production reconciliation,
deployment and payment acceptance remain operator actions.

## Verification boundaries

All 20 new c387 cases passed. Two focused regression runs passed 76 and 108
tests respectively, with zero skips; the guard cases overlap between runs.
These include the existing payments/c366 and settlement/plan/analytics suites.

Executed coverage includes real PostgreSQL, real Stripe signature verification,
deterministic provider lifecycle responses, unchanged receipt/ledger backstops,
concurrent failed-intent versus plan insertion in both orderings, and populated
0036 downgrade/upgrade and conflict rollback. The migration lock test pauses after
preflight and before index installation to prove a competing writer cannot enter
that gap. Provider errors and a hanging retrieval retain the failed reservation;
a subsequent same-intent retry can reacquire ownership.

These tests do not claim a live provider charge, deployed infrastructure acceptance,
a global request deadline, or automatic recovery of historical conflicts. Separate
operational acceptance is required before collecting live dues.
