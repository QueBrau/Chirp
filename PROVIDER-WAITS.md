# Provider waits and database capacity (c355)

Local probes reproduced the remaining connection retention with a fixed **one
connection, zero overflow, one-second pool timeout**. Each of a paused GCS post
finalization, Stripe account retrieval and Stripe customer creation held that
connection. An unrelated authenticated read returned 503 after approximately
1.01 seconds. The longest observed checkout lasted 1.11–1.22 seconds.

A separate synchronous Firebase fixture injected a 250 ms verification delay. It
ran on the event-loop thread, delayed the loop by approximately 249 ms, and allowed
no heartbeat to run during verification. These are controlled local experiments,
not production latency measurements or cloud load tests. All four regression tests
failed against the original handlers before the fix.

With the initial fix and the same pool budget, all three provider waits held zero
connections and the unrelated read returned 200 in 19–41 ms. The Firebase fixture
ran off-loop with 41 heartbeat ticks and 4.44 ms maximum observed lag. The tests also
record pool acquisition time, connection ownership and database transaction
lifetime separately; total HTTP time is not labeled as pure database queue time.
The thresholds rely on actual ownership and interleaving, not these incidental
laptop timings.

A later run with the separate timing probes recorded the following (milliseconds,
same one-slot pool; all unrelated reads returned 200 and held slots were zero):

| Delayed preparation | Unrelated HTTP read | Maximum pool acquisition | Maximum transaction | Maximum checkout |
| --- | ---: | ---: | ---: | ---: |
| GCS post finalization | 8.70 | 0.40 | 9.52 | 10.92 |
| Stripe account retrieval | 11.78 | 0.31 | 24.78 | 82.88 |
| Stripe customer creation | 10.90 | 0.46 | 35.35 | 127.28 |

That run's Firebase probe recorded 45 heartbeat ticks and 0.95 ms maximum loop lag.

Validation includes 31 new provider/permission/cancellation cases. The broad
regression run passed 195 cases; its remaining legacy orphan fixture was corrected
to inject failure after the copy (instead of before the new read-only release) and
passed separately. Existing money coverage also exercised the unchanged reservation
and webhook helpers. A shortened timeout test retains its original hang/cancellation
proof and restores the normal budget only for its successful recovery phase.
No skips were accepted. These are targeted local checks; CI remains the publication
gate.

## Transaction boundaries

The changed handlers finish their initial **read-only** transaction before provider
I/O and expire their ORM cache. After the provider returns they reload the current
user and authorization/state before publishing a response or applying changes.
No profile field or post body is committed just to free a connection.

| Path | Checks after the provider wait |
| --- | --- |
| Chapter post create/edit | Registered, unsuspended user; active membership; current chapter/campus; current campus verification for campus content; fresh edit target, owner and deletion state |
| Campus post create | Registered, unsuspended user and current verification for the requested campus |
| Avatar finalization | Registered, unsuspended user before applying any staged profile fields; account-type analytics still emit only after the final commit |
| Upload URL signing | Registered, unsuspended user before returning the signed URL |
| Stripe account status | Current member, unsuspended user and the same connected-account association; the retrieved account must identify the requested account |
| Onboarding account/link | Current treasurer/president and unsuspended user before binding an account or returning a link; conditional NULL-only account assignment retains a concurrent winner |
| Dues account/customer preparation | Current user and membership, cycle, active-plan and ledger guards, and unchanged chapter/account routing before reservation work |

The dues context checks run both before and after preparation. A concurrent dues
amount change is loaded before a new reservation snapshots it; an existing
reservation retains c349's own amount/currency snapshot. A competing customer
creation retains the canonical database mapping. A newly inserted mapping commits
before reservation ownership, followed by another fresh eligibility/account check.
This avoids holding a customer uniqueness lock while another request is trying to
reach the reservation's NOWAIT guard. Unused competing Customers remain inert; an
unused competing connected account is not bound or automatically deleted.

The [c366](PAYMENT-CREATE-RECOVERY.md) and [c387](PAYMENT-FAILED-LIFECYCLE.md) money
contracts remain in force. PaymentIntent create/retrieve/cancel still use the
existing reservation lock and shared 15-second provider budget. That section
intentionally holds one database connection for serialization. Unknown outcomes
retain their original key/reservation; failed-but-retryable intents remain held.
CustomerSession creation already follows a commit/release and remains there.
No new retry, cancellation, settlement, ledger, or schema protocol is introduced.

Authorization is checked again at the resumed write boundary. This does not claim
to make external provider work atomic with revocation, nor to freeze account
routing against an out-of-band operator change after the final check. Existing
connected-account associations still require careful operator reconciliation if
changed outside the normal application flow. Customer mappings remain keyed by
user and chapter, not by a connected-account snapshot; this patch does not make
an account rotation into a safe automatic customer migration.

## Verification worker ownership

Firebase token verification uses a dedicated executor with four actual worker
threads. A request acquires one of four submission permits on the server's event
loop before submitting work. Waiting callers submit nothing and hold no database
connection. The **native concurrent future**, rather than its asyncio caller, owns
the permit until the job finishes or is canceled before starting. Native asyncio
request cancellation therefore remains responsive without admitting a fifth
still-running verifier. The cancellation test fills all four workers, cancels
their callers, proves another verification cannot start, then releases the workers
and proves recovery.

The executor is separate from GCS signing/finalization. This is a work-admission
bound for the production process's single event loop; pending HTTP callers still
belong to the server concurrency envelope. It is not a new global request deadline,
an immediate way to kill a blocking SDK call, or a cross-event-loop admission
contract. No explicit blocking worker-drain is added to application shutdown.
Firebase UID/email verification, invalid-token responses, and startup initialization
semantics are unchanged. No token or provider payload is written to telemetry.

## Storage and operational limits

c211 and c223 already offloaded signing and GCS finalization; this change keeps those
implementations. Capability media reads have no database dependency and need no
transaction release. The reconciliation CLI runs in its own process and is not a
request-loop issue.

An offloaded GCS copy may finish after cancellation or after the caller loses
permission. A failed write can consequently leave an unreferenced permanent object.
Post-media orphans remain within the existing posts reconciler's scope and age
floor. Avatar objects use `avatars/`, which that reconciler deliberately does not
scan. This work does not claim avatar cleanup, widen delete grants, or automatically
delete provider objects. Signed uploads not returned to a suspended caller do not
become a successful post; the write routes retain their own authorization gates.

Release/revalidation does not remove every provider latency or supply production
capacity numbers. It trades extra short database reads for making external waits
stop occupying scarce connections, while preserving current money ownership.
The fixed-pool regressions, state-change races, provider-winner cases and existing
security/payment suites are the local evidence. Production deployment and acceptance
remain separate actions; no cloud change, migration, real provider request or funds
movement was performed for this card.
