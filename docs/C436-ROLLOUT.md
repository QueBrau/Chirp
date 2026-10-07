# Account data rollout boundary

This change provides authenticated account-data request intake, owner-scoped status, and a downloadable export of the supported server-side records. Export artifacts are immutable, explicitly marked `partially_completed`, and served only through the authenticated download route with `Cache-Control: private, no-store`.

The current export covers the account profile, memberships, authored content and interactions, events and attendance, alumni and lineage records, owned devices and message metadata, caller-owned message receipts, payment and ledger rows linked to the account, role terms, moderation/report actions authored by the caller, legal policy versions plus the caller's acceptance records, and organization/payment authority declarations made by the caller. Ciphertext remains opaque; key material and credential-shaped fields are redacted.

The export explicitly omits provider-held Firebase/Auth, payment, email, object storage, logging, and backup copies; plaintext that is never stored by the server; prekey byte material; dormant client-only SQLite data; and outbox delivery payloads or recipient identifiers. Role terms are currently limited to terms changed by the caller. Installment details are represented by caller-linked payment-plan rows; provider payment records remain omitted. Shared organization, safety, append-only financial, provider-backup, and logging records have documented retention reasons. User-block export includes only blocks initiated by the caller and never exposes the other account's identifier. New tables or columns require an allowlist review before they can enter an export.

Deletion requests are accepted and tracked, but remain `blocked` with `manual_processing_required`. No provider deletion adapter, account tombstone, account-data erasure, or email notification is reachable from these request routes. Firebase authentication still verifies identity and revocation with the provider. Firebase/Auth removal, payment-provider confirmation, email/object-storage cleanup, and logging/backup expiry require a later durable operator workflow with provider-step journaling, retry safety, and verified completion evidence. Export artifacts have a bounded maintenance CLI (`python -m app.jobs.account_data`) that previews by default and requires explicit `--delete`; this release does not enable a production schedule.

Rollout order:

1. Apply migration 0041 only after the c438 migration 0040 parent is present. Grant the authenticated application role access to the request/artifact tables through the backend only; grant no client role direct table access.
2. Deploy backend models, migrations, auth freshness dependency, request/status/download routes, export service, and focused database tests before shipping either client screen.
3. Verify suspended users can still read their own request status while sensitive creation/download remains fresh-auth protected; verify owner isolation and rate limits.
4. Ship the mobile settings screen and web support fallback after backend behavior is observable.
5. Treat deletion as manual processing until each provider step has a durable journal, an idempotent retry contract, and provider-specific confirmation.

Sensitive request creation and downloads check both Firebase token revocation and
recent authentication. Verify the API identity can read Firebase account status
before release; existing certificate-only token verification does not prove that
permission. No new cloud permission is granted by this code change.
