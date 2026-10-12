# Account data rollout boundary

This change provides authenticated account-data request intake, owner-scoped status, and a downloadable export of the supported server-side records. Export artifacts are immutable, explicitly marked `partially_completed`, and served only through the authenticated download route with `Cache-Control: private, no-store`.

The current export covers the account profile, memberships, authored content and interactions, events and attendance, alumni and lineage records, owned devices and message metadata, caller-owned message receipts, payment and ledger rows linked to the account, role terms, moderation/report actions authored by the caller, legal policy versions plus the caller's acceptance records, and organization/payment authority declarations made by the caller. Ciphertext remains opaque; key material and credential-shaped fields are redacted.

The export explicitly omits provider-held Firebase/Auth, payment, email, object storage, logging, and backup copies; plaintext that is never stored by the server; prekey byte material; dormant client-only SQLite data; and outbox delivery payloads or recipient identifiers. Role terms are currently limited to terms changed by the caller. Installment details are represented by caller-linked payment-plan rows; provider payment records remain omitted. Shared organization, safety, append-only financial, provider-backup, and logging records have documented retention reasons. User-block export includes only blocks initiated by the caller and never exposes the other account's identifier. New tables or columns require an allowlist review before they can enter an export.

Deletion requests are accepted and tracked with a durable provider-step plan (migration 0043). The request route remains blocked until an operator supplies all four reviewed provider adapters: Firebase/Auth, payment provider, media storage, and email/logs/backups. Each step has a stable idempotency key, attempt count, sanitized outcome, and manual-review state; missing or ambiguous provider confirmation cannot reach the database tombstone. The injected executor preserves shared organization, safety/legal, authored encrypted-message, and append-only financial rows, removes the caller's server-side prekey rows for revoked devices, and removes direct Firebase/email identity linkage only after every provider step is confirmed. The default deployment has no configured adapters and therefore cannot perform deletion; operator credentials, grants, and adapters must be reviewed before any enablement. Export artifacts have a bounded maintenance CLI (`python -m app.jobs.account_data`) that previews by default and requires explicit `--delete`; this release does not enable a production schedule.

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

Provider adapters may also return a `retained_with_reason` list containing only
the fixed retention codes already defined by the local cleanup boundary. The
deletion journal validates and durably stores those codes in the step receipt,
then includes their fixed explanations in the final request status alongside
local retention reasons. This is an acknowledgment of a declared retention
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
