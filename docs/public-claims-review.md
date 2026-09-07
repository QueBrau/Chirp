# Public product claims review — September 7, 2026 (c367)

These corrections describe current product behavior. They do not complete the
separate counsel review in c75 or change the existing legal allocation of rights,
fees, liability, jurisdiction, minimum age or response commitments.

| Public claim corrected | Source of current behavior |
| --- | --- |
| Campus is self-selected and unverified | `backend/app/routers/campus_verification.py`, `backend/app/services/campus_verification.py`, `backend/app/core/campus_access.py`, `backend/app/models/identity.py`: supported email domain resolves campus; code redemption records mailbox verification. This is not proof of enrollment. |
| Message content is unreadable to Chirp | Standing labeling decision in `SECURITY-REVIEW.md`; `app-mobile/app/(tabs)/messages/[id].tsx` has a disabled composer. An opaque database column or protocol-version name does not establish deployed end-to-end encryption. |
| Delivery and read receipts exist | `backend/app/models/messaging.py`: `MessageReceipt.delivered_at`, with no separate read-receipt field; `backend/app/models/e2ee.py` stores device public keys. |
| Anonymous author is absent from every app response | `backend/app/schemas/chirp.py` omits author from student board output; `backend/app/models/chirp.py` retains `author_id`. Narrow the statement to student-facing board responses and acknowledge authorized operational access. |
| Officers can review campus reports because they are officers | `backend/app/routers/moderation.py` and `backend/app/core/permissions.py`: approved-chapter eligibility and per-content scope, report access requires active eligible officer membership in an approved chapter. Platform-admin suspension privileges alone do not grant report-list access. |
| Any data can be removed, account deletion is self-service, ledger names can simply be detached | No account deletion/export or chapter self-leave route exists. `backend/app/routers/auth.py` supports profile updates; `backend/app/routers/finance.py` and ledger triggers preserve financial records. Describe support requests and retained records without promising an implemented erasure workflow. |
| Purge always erases content exactly 30 days later | `backend/app/config.py` sets retention eligibility; `backend/app/jobs/purge.py` performs deletion. A configured duration does not prove a successful scheduled job. The infrastructure audit found the scheduled purge failing secret access (c369). Removed content is hidden before physical deletion; backups have separate retention. |

Public copy avoids internal job names, migration numbers and engineering milestones.
The shared legal-update date changes with these factual corrections. Existing
response and takedown commitments still require an operational support process;
this copy change does not prove that process or any production retention execution.

Validation: web TypeScript and production bundle build. Re-check these claims when
messaging, account erasure/export, moderation access or retention behavior changes.
The signed settlement quarantine in c349 and the bounded purge in c369 are separate
changes and are not represented here as already deployed.
