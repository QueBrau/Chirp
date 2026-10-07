# Account data rollout boundary

This change provides authenticated account-data request intake, owner-scoped status, and a downloadable export of the supported server-side records. Export artifacts are immutable, explicitly marked `partially_completed`, and served only through the authenticated download route with `Cache-Control: private, no-store`.

The current export covers the account profile, memberships, authored content and interactions, events and attendance, alumni and lineage records, owned devices and message metadata, caller-owned message receipts, payment and ledger rows linked to the account, role terms, moderation/report actions authored by the caller, and legal policy versions plus the caller's acceptance records. Ciphertext remains opaque; key material and credential-shaped fields are redacted.

The export explicitly omits provider-held Firebase/Auth, payment, email, object storage, logging, and backup copies; plaintext that is never stored by the server; prekey byte material; and dormant client-only SQLite data. Shared organization, safety, append-only financial, provider-backup, and logging records have documented retention reasons. User-block export includes only blocks initiated by the caller and never exposes the other account's identifier. New tables or columns require an allowlist review before they can enter an export.

Deletion requests are accepted and tracked, but remain `blocked` with `manual_processing_required`. No provider adapter, credential, account tombstone, row deletion, email, or external call is reachable from this release. Firebase/Auth removal, payment-provider confirmation, email/object-storage cleanup, and logging/backup expiry require a later durable operator workflow with provider-step journaling, retry safety, and verified completion evidence. The artifact purge helper is available to an explicitly scheduled caller; this release does not claim that a purge schedule is active.

Rollout order:

1. Apply migration 0041 only after the c438 migration 0040 parent is present.
2. Deploy backend models, auth freshness dependency, request/status/download routes, export service, and focused database tests.
3. Verify suspended users can still read their own request status while sensitive creation/download remains fresh-auth protected; verify owner isolation and rate limits.
4. Ship the mobile settings screen and web support fallback after backend behavior is observable.
5. Treat deletion as manual processing until each provider step has a durable journal, an idempotent retry contract, and provider-specific confirmation.
