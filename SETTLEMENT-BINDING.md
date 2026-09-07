# Stripe settlement binding (c349)

A valid Stripe signature authenticates the payload. It does not establish that
the payment belongs to this member, chapter, environment or dues reservation.
The webhook now requires that binding before changing payment state.

The creation transaction stores amount and currency on the reservation before
calling Stripe. Both the provider create and the client response use that amount.
Settlement finds and locks the reservation by its stored PaymentIntent ID, checks
the event and intent mode against the configured key, and checks the connected
account against the owning chapter. A success must also match the stored member,
cycle, chapter, rail, currency, amount and amount received. The ledger is populated
from the reservation and cycle, never from unvalidated metadata or a later price.

An unmatched event carrying Chirp metadata, or a mismatched reservation, produces
one append-only diagnostic row with bounded identifiers and numeric evidence.
It does not change the reservation, create a ledger entry or emit payment analytics.
Foreign events without a stored reservation or Chirp metadata are acknowledged
without inventing ownership. Unknown key mode fails closed into quarantine.

The processed-event receipt and quarantine/settlement commit atomically. Database
write errors remain retryable; a failed diagnostic insert cannot be swallowed into
a 200 response. Explicit uniqueness handling preserves event and PaymentIntent
replay safety. A second distinct capture that cannot be recorded still raises a
reconciliation error containing internal IDs only. Late failed/canceled events
cannot demote a succeeded reservation.

The SDK verifies the signature over raw bytes. We then parse those same bytes as
JSON rather than relying on SDK resource wrappers supporting dictionary methods.
Actual signed tests caught that incompatibility after legacy stub tests passed.
No raw payload, descriptions, billing data, secret key or customer email is stored
in quarantine or payment logs.

## Migration and operational acceptance

Migration 0033 follows 0032 and precedes reserved 0034/0035. It backfills existing
reservations from their cycles and the integration's existing USD currency, then
requires non-null snapshots. It adds the diagnostic table, declared reason checks,
event uniqueness and an UPDATE/DELETE refusal trigger. Seeded up/down/up tests
exercise existing rows, not just an empty schema. Downgrade destroys diagnostic
evidence and snapshots; take that loss into account before rollback.

Deploy the compatible payment writer with the migration: an old writer omitting
the new non-null columns will fail. Inspect outstanding provider intents before
cutover; a missing stored ID or historical metadata mismatch is quarantined, never
guessed into the ledger. An event arriving before the create path persists its
provider ID can also be quarantined. Such cases need operator reconciliation.

Before closing c349, inspect quarantine after deployment and verify controlled
test-mode card/ACH settlement, replay and mismatch handling with the correct
connected account. A signature fixture is not a live payment test. This PR does
not apply a production migration, send funds or fix provider-cancellation ambiguity
(c366), nor does it provide an automatic quarantine repair/refund operation.

Stripe's [Connect webhook documentation](https://docs.stripe.com/connect/webhooks)
defines the originating account and the possibility of live and test events at a
production endpoint. The [PaymentIntent object](https://docs.stripe.com/api/payment_intents/object)
defines the settlement amount, amount received, currency and mode fields.
