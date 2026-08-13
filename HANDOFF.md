# HANDOFF — current state

_Last updated: Aug 13 2026, end of session. Branch: `family-tree` (pushed).
`board.html` = live task board._

## Session summary

Three workstreams. Two complete and verified, one deliberately stopped at a clean
scaffolding point (session usage limit).

| Work | State | Verified |
|---|---|---|
| Yak → real API (list/create/vote, report/block) | **Done** | 49 backend tests green, tsc clean |
| Treasurer + Secretary dashboards on real data + CSV export | **Done** | 80 backend tests green, tsc clean |
| Stripe Connect onboarding + dues PaymentSheet | **Scaffolding only** — SDK installed, no code | tsc clean (dep unused) |

---

## 1. Yak on real API — DONE

**Backend:** `POST /moderation/blocks/by-yak/{yak_id}` (204, empty body),
blocked-author filtering in `list_yaks`, `GET /auth/me`.

**Mobile:** real campus scope, server-owned `my_vote`, composer, report/block UI.

**The anonymity invariant is structural, not conventional.** `YakOut` declares no
author field, no schema uses `extra=allow`, and nothing publishes yaks to Redis —
so the WS gateway isn't an exposure surface either. Tests sweep every yak response
for author keys.

Two non-obvious design points, both load-bearing — **don't "simplify" them**:
- Block-by-yak returns **204 always**, even for an already-existing block. A
  409-vs-204 split is a one-bit oracle for "have I already blocked this author",
  which would let a caller test whether two yaks share an author.
- The filter is an anti-join keyed on `author_id` (not `yak_id`) AND scoped to
  `blocker_id`. Tests cover both; a filter keyed to `yak_id` passes every other test.

**Accepted by design:** blocking hides that author's *other* yaks too, which tells
the blocker those posts shared an author. Unavoidable in any block feature,
disclosed only to the blocker, only about content they can no longer see.

---

## 2. Treasurer + Secretary dashboards + export — DONE

**Backend:** `GET /me/memberships`, `GET /chapters/{id}/ledger/export.csv`
(treasurer/president), `GET /chapters/{id}/meetings/export.csv`
(secretary/president), shared `app/core/csv_export.py`.

**Mobile:** both dashboards fully on real data (zero mock imports), spend-approval
decisions, ledger append, meeting create/minutes/attendance, CSV export via
`src/lib/export.ts` → share sheet.

**CSV injection is defused, and the fix is subtle.** Exports get emailed to
accountants who open them in Excel; a member-supplied description starting with
`= + - @` is a live formula. `sanitize_csv_text()` neutralizes those — but it is
applied to **text fields only**. The ledger's amount column is legitimately
negative ("positive = in, negative = out"), so blanket sanitizing would turn every
expense into `'-500` and corrupt the export. There are tests for both halves.
**If you touch the CSV writer, keep that split.**

**`MembershipOut.display_name`** was added and joined into
`GET /chapters/{id}/members`. There is no `GET /users/{id}` anywhere, so this is
the ONLY way to render a member's name on real data — without it the secretary's
attendance roster is a column of UUIDs.

**Open product question (not a bug):** taking attendance defaults every unmarked
member to `absent` on save. Conventional, and correctable since attendance is an
upsert — but if that's wrong for your chapters, change it in
`app/(tabs)/chapter/secretary.tsx`.

---

## 3. Stripe Connect + dues — PICK UP HERE

**Nothing is implemented.** What exists:
- `@stripe/stripe-react-native@0.50.3` installed, registered in `app.json` plugins.
- `backend/app/routers/payments.py` + `app/services/stripe_service.py` are still
  the original **501 stubs**.
- `src/payments/dues.tsx` is still the original placeholder screen.
- `src/api/payments.ts` does not exist.

### Decisions already made (don't re-litigate — these were the hard part)

1. **Express accounts + direct charges.** The chapter is merchant of record; funds
   land in the chapter's balance; chapter carries chargeback liability. Keeps Chirp
   out of money custody. PaymentIntents are created with `stripe_account=<acct_id>`.
2. **Saved cards via Stripe Customers.**
3. **Platform fee: 1% card, 2% ACH** as `application_fee_amount`. (Sanity check:
   ACH still lands cheaper for the chapter all-in, ~2.8% vs ~3.9%.)
4. **Member picks the rail BEFORE the intent is created.** `application_fee_amount`
   is immutable at PaymentIntent creation, but PaymentSheet would let the member
   pick card-vs-ACH *after* — so there's no moment where you know the rail and can
   still set the matching fee. The UI therefore asks "Card or Bank?" first, then
   the server creates the intent with `payment_method_types` locked to that rail.

### Must-get-right list (these protect real money)

- **Webhook replay → permanent ledger corruption.** Stripe delivers at-least-once
  and retries for days. The ledger is append-only (no update, no delete), so a
  replayed `payment_intent.succeeded` appends a SECOND permanent dues payment.
  Guard with a **UNIQUE partial index** on
  `ledger_entries(stripe_payment_intent_id) WHERE stripe_payment_intent_id IS NOT NULL`
  — a DB constraint, not check-then-insert, which races. Plus a
  `processed_stripe_events(event_id PK)` table for event-level dedup.
- **Customers live on the connected account under direct charges.** A member in two
  chapters needs a Customer per chapter, so this must be a
  `chapter_stripe_customers (user_id, chapter_id) → stripe_customer_id` mapping
  table. A `users.stripe_customer_id` column works for the first chapter and
  silently breaks the second.
- **Verify the webhook signature BEFORE parsing the body.** An unverified payload
  is attacker-controlled input. Use the raw bytes from `await request.body()`.
- **Amount always from `dues_cycles.amount_cents`**, server-side. Never the client.
- **Idempotency key** on PaymentIntent creation derived from
  `(cycle_id, user_id, rail)` so a client retry doesn't create a second intent.
- **Return 2xx for unhandled webhook event types** or Stripe retries forever.
- **Never log event payloads** (customer PII) or the client secret.
- Guard double payment: 409 if a `dues_payment` ledger entry already exists for
  `(dues_cycle_id, related_user_id)`.

### Mobile must-get-right

- `StripeProvider`/`initPaymentSheet` need `stripeAccountId` alongside
  `publishableKey`. Omitting it sends real money to the **platform** account
  instead of the chapter's, silently.
- `allowsDelayedPaymentMethods: true` — ACH is a delayed method and the sheet fails
  without it.
- **ACH success ≠ paid.** A successful sheet on the ACH rail means *initiated*;
  `payment_intent.succeeded` (and the ledger entry) can be days later. Success copy
  must be rail-aware — don't tell a member dues are paid when they aren't.
- `presentPaymentSheet` returns `code === "Canceled"` for user cancellation — that's
  not an error, don't alert on it.

### Planned API contract (mobile was to be built against this)

- `POST /payments/connect/onboarding-link` `{chapter_id}` → `{url, expires_at}`
- `GET /chapters/{chapter_id}/payments/status` →
  `{onboarded, charges_enabled, details_submitted}`
- `POST /payments/dues/{cycle_id}/intent` `{rail: "card"|"ach"}` →
  `{payment_intent_client_secret, ephemeral_key_secret, customer_id,
  publishable_key, stripe_account_id, amount_cents, application_fee_cents, rail}`
  — 409 `already_paid`, 409 `chapter_not_onboarded`

No real Stripe keys are needed to build this: tests mock the SDK, keys stay in env
(`stripe_secret_key` / `stripe_webhook_secret` already in `app/config.py`).

---

## Environment notes (this Mac) — UPDATED, previous notes were stale

- **Docker IS available** (the old "No Docker / local PG14 via pg_ctl" note was
  wrong). Host ports in use: 5432 (native Postgres), 5433 + 6379 (unrelated
  `leadgen-*` containers). Test runs used disposable containers on **5434/6381**.
- **`backend/.venv` is Python 3.9 and unusable** — `pyproject.toml` requires >=3.11,
  and the models use `X | None` unions that don't evaluate on 3.9. Homebrew
  `python@3.12` is installed (added this session). Every test run this session built
  a throwaway venv. **Worth fixing properly — it's pure friction.**
- Test command that works:
  ```
  cd backend
  TEST_DATABASE_URL=postgresql+asyncpg://chirp:chirp@localhost:5434/chirp_test \
  DATABASE_URL=postgresql+asyncpg://chirp:chirp@localhost:5434/chirp_test \
  REDIS_URL=redis://localhost:6381/0 AUTH_MODE=emulated \
  <venv>/bin/python -m pytest -q
  ```
- **EAS dev build must be rebuilt** before export or Stripe work on device — three
  native modules were added this session (`expo-file-system`, `expo-sharing`,
  `@stripe/stripe-react-native`). Mock flows and `tsc` work without it.
- **iOS Simulator is currently broken on this Mac**: `launchd_sim` fails to bind a
  session ("could not bind to session"). Killing CoreSimulator processes doesn't
  clear it; a Mac reboot is the known fix. Xcode 15.3, iOS 17.4 runtime only.
- `USE_MOCKS` in `src/api/client.ts` is still `true`. Everything built this session
  works under both branches.
