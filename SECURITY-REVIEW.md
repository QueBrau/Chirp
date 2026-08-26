# Chirp — Security Review (narrative index, last full review Aug 25 2026)

`board.html` is the live source of truth for security work — every card, the
decisions log, and the Aug 25 c182 decisions entry in particular. This file is
a narrative index over that board: a snapshot of what has run, what shipped,
and what is still open, written for someone who does not want to read 190
cards. When the two disagree, the board is right and this file is stale;
re-run this refresh (c188's pattern) rather than trusting a paraphrase here
over the board.

This review covers backend, mobile client, web/hosting, and infra/deploy on
`main` as of this refresh. It does not restate secrets, connection strings, or
other infra values — those live only in the gitignored `INFRA-PRIVATE.html`,
referenced here by name.

---

## Coverage map — all nine lenses

| Lens | Last ran | Verdict | Card refs |
|---|---|---|---|
| Backend auth | Aug 13 (5-lens review) → Aug 16 (re-confirmed clean) → Aug 22 (.edu race fix) | All 15 Aug-13 findings closed and test-pinned; no new findings since Aug 16 | c27, c28, c29, c85, c88, c138 |
| Money path | Aug 16 (webhook lens) + Aug 15 (double-charge guard) + Aug 24 (definitional fix) | No dedicated adversarial pass since dues/Stripe shipped (Aug 14+) — see note below | c51, c11, c40, c172 |
| Authorization | Aug 13 → Aug 16 (.edu campus gate) → Aug 22 (moderation-tier fix) | Clean; actively hardened, org-scoping tests extended | c27, c85, c88, c142 |
| Tokens / logging | Aug 22 (WS auth move) → Aug 23 (scrub widened) → Aug 24 (log purge) → Aug 25 (app logger fix) | Clean; four successive hardening rounds | c143, c146, c145, c176 |
| Media | Aug 22 (upload validation) → Aug 24 (privacy flip + orphan cleanup) | Clean, verified live | c139, c140, c153, c155 |
| Dependencies | Aug 24 (npm audit review) | No runtime-exploitable findings; toolchain debt tracked | c170, c174 (in progress) |
| Mobile client | Aug 25 (c182 pass) | Clean aside from one finding, closed same day | c182, c184 |
| Web / hosting | Aug 22 (CSP) → Aug 25 (c182 pass + repo hygiene) | Clean | c128, c182, c185 |
| Infra / deploy | Aug 16 (initial findings) → Aug 23 (docs/root hardening) → Aug 25 (re-verified) | Three items still open, queued to Jose | c122, c123, c182 |

Money path note: the Aug 16 session's record explicitly says "the Stripe
webhook audited clean (fails closed at 503, verifies before parsing, two-layer
replay dedup)" — that check is real and current code still matches it
(`backend/app/routers/payments.py:359-394`: `missing_stripe_signature` on no
header, `stripe_service.verify_webhook_event` before any parsing,
`ProcessedStripeEvent` row + a unique partial index on
`ledger_entries.stripe_payment_intent_id` as two independent replay guards).
But that is a webhook-shaped check, not a full adversarial sweep of the money
path as it exists today (reservation flow, cross-rail retry, refund/correction
interaction). c172 (Aug 24) found and resolved a real definitional gap between
the double-charge guard and the treasurer dashboard while building an
unrelated feature — evidence the path has real edges, not that it has been
swept for them. **Unverified as of this refresh: whether a dedicated
adversarial money-path lens has ever run end to end.** Recommend one before
real dues volume.

---

## Shipped mitigations

- **Aug 13 — original 5-lens review (c27):** authz, secrets/crypto, backend
  correctness, mobile correctness, spec-compliance. 23 raised, 15 confirmed.
  Fixed in the same pass: moderation campus-scoping, invite-role escalation,
  email-squatting at bootstrap, the CORS/credentials default, WS-token
  logging, four TOCTOU check-then-insert races, message pagination. 33 tests
  added.
- **Aug 13 — chapter creation gated (c28):** platform-admin only, no
  self-serve grant API; adversarial audit found no bypass via
  invites/role-PATCH/join/bootstrap. Closes the mechanism finding 1 depended
  on (self-serve presidency).
- **Aug 14 — deferred items shipped (c29):** prekey-bundle rate limiting
  (per caller/target pair, `backend/app/routers/keys.py:259-265`), Firebase ID
  token auto-refresh (`app-mobile/src/auth/session.ts:58-60`,
  `onIdTokenChanged` wired to `setAuthToken`), compound `(created_at, id)`
  message-pagination index.
- **Aug 15 — dues double-charge guard (c51):** reserve-before-charging;
  `dues_payment_intents` written before Stripe is called, one live reservation
  per (cycle, member) across both rails, failed/canceled payments release the
  reservation, ledger backstop via a unique constraint.
- **Aug 16 — campus_id made server-owned (c85):** removed from client-writable
  schemas; only the .edu verification flow (c86) and chapter-join derivation
  can set it. Closed a self-asserted-campus hole that a 5-lens authorization
  pass had missed because it checked consistency, not trustworthiness.
- **Aug 16 — .edu campus verification gate (c88):** one shared dependency
  gating six call sites, closing three separately-copied checks plus two
  bypasses the sweep found on its own (a campus-audience *write* with no
  campus check at all, and an inline copy in yak voting).
- **Aug 22 — .edu attempt-cap race fixed (c138):** atomic
  `UPDATE ... WHERE attempts < MAX ... RETURNING`, so a refused guess is never
  miscounted; proven under a real concurrent-thread test.
- **Aug 22 — moderation tier by content audience (c142):** report severity now
  follows the target's actual audience (yak/post/comment) instead of a
  one-size gate.
- **Aug 22 — process hardening, not a vulnerability fix (c120, c121):**
  named here because the ticket that requested this refresh grouped them with
  security hardening, so worth being precise about what they actually are.
  c120 fixed an unhandled-promise-rejection bug in the chapter like/unlike
  flow (missing error handling, not an auth or data-exposure issue). c121
  built a static request/route contract checker
  (`app-mobile/scripts/verify-contract.mjs`) that diffs every client API call
  against every backend route at CI time — its first real run caught a live
  bug (the secretary attendance screen was calling a GET that the backend
  only ever registered as PUT) and, in a sabotage test, caught the exact
  wrong-HTTP-verb regression class that c115 shipped once already. It is a
  correctness/regression gate rather than a security control, but it is the
  kind of gate that would have caught c115 before merge.
- **Aug 22 — media upload validation (c139):** `media_urls` validated
  server-side on all three write sites — exact-prefix bucket allowlist, count
  cap, host-spoof and prefix-extension attempts both verified blocked.
- **Aug 22 — WS auth moved off the URL (c143):** `Sec-WebSocket-Protocol`
  subprotocol with an `Authorization: Bearer` fallback; query-string auth
  removed entirely rather than deprecated.
- **Aug 23 — credential log scrub widened (c146):** regex now matches any
  `[a-z0-9_]*token=` query param, not three enumerated literals; also fixed a
  latent bug where the filter's plain-message branch was unreachable.
- **Aug 23 — /docs, /redoc, /openapi.json closed on real deployments
  (`backend/app/main.py:148,153-155`):** `docs_enabled = settings.env ==
  "local"`. These were live on prod from Aug 13 until this shipped.
- **Aug 23 — container drops root; prod safety guard un-strippable.**
- **Aug 24 — repository media privacy flip (c140):** bucket moved to
  public-access-prevention enforced + `allUsers` read removed; the runtime
  service account gets a `posts/`-conditioned read grant; the API now issues
  app-owned HMAC capability URLs (`backend/app/services/storage_service.py`)
  instead of exposing the bucket. Verified live: capability URL serves exact
  bytes and is stable on re-check, direct GCS access 403s, the app renders
  normally. `c155` confirms the same flip also closed `tmp/`.
- **Aug 24 — media orphan-cleanup dry run (c153):** dedicated runner with
  read-only DB access and delete-only IAM conditioned to `tmp/`; first dry run
  scanned=1, referenced=1, eligible=0, deleted=0 — no media deletion
  attempted.
- **Aug 24 — Cloud Logging leak purged (c145):** 6,328 request-log and 17,275
  stderr entries removed after the WS-token exposure window; unrelated logs
  preserved; post-delete cutoff checks confirmed zero remaining.
- **Aug 24 — dependency audit (c170):** all 9 high / 13 moderate `npm audit`
  findings trace to three build-toolchain packages, none reachable at
  runtime in the shipped app. Real fix needs a major Expo SDK bump, carded and
  in progress as c174 as of this refresh.
- **Aug 24 — dues status definitions reconciled where they diverge (c172):**
  found while building c171 — the double-charge guard treats any payment row
  as `already_paid` and ignores corrections, while the president dashboard
  nets corrections, so a refunded member reads as owing on one screen and
  blocked from re-paying on the other. Both behaviors are individually
  correct (the guard erring toward "paid" prevents a double charge; the
  dashboard erring toward "owes" tells the treasurer the truth) — left as a
  documented seam pending real refund volume, not silently reconciled.
- **Aug 25 — app-level logger actually wired to stdout (c176):** root cause
  was an unconfigured `app` ancestor logger, so `logger.info`/`logger.warning`
  calls were built but never emitted under Cloud Run — including the
  send-confirmation line for verification emails. Fixed via `dictConfig` on
  the `app` logger, called first in `create_app`. This is an observability
  fix, not a data-exposure one — nothing sensitive was ever in those lines.
- **Aug 25 — URL validation on client-supplied link fields (c184):** shared
  `validate_public_url` (`backend/app/core/validation.py`) enforces http/https
  scheme, required host, 2048-char cap, applied to `AlumniProfile.linkedin_url`
  / `apply_url` plus two more the sweep found unvalidated
  (`EventCreate.cover_url`, `User.avatar_url`). Closes a phishing / intent-URI
  vector: the mobile client opens these blind via `Linking.openURL`.
- **Aug 25 — repository hygiene (c185):** the comment explaining
  infra-owner reasoning was moved out of public source into
  `INFRA-PRIVATE.html` (the repo went public Aug 24); `ci.yml` now pins
  explicit least-privilege `permissions: contents: read`.
- **Standing — Stripe webhook verification:** signature required and verified
  before any event parsing (`missing_stripe_signature` 400 with no signature;
  `stripe_service.verify_webhook_event` gates everything else), replay safety
  layered at both the event level (`ProcessedStripeEvent`) and the payment
  level (unique partial index on `ledger_entries.stripe_payment_intent_id`),
  event payloads never logged. Verified in code this session
  (`backend/app/routers/payments.py:358-394`).

---

## Open items

| Item | Severity | Owner | Card ref |
|---|---|---|---|
| Default compute SA holds `roles/editor` project-wide AND `secretAccessor` on `DATABASE_URL` + all three Stripe secrets, while nothing runs as that identity (the runtime SA is `chirp-api-run`) | HIGH | Jose (standing credential-shaped exception) | c182 |
| Cloud SQL: public IPv4 stays for now (Jose's laptop proxy path needs it); `sslMode` should move to `ENCRYPTED_ONLY` — safe because the Auth Proxy always encrypts regardless | Not rated in source; one `gcloud` config change | Jose | c182 |
| Firebase browser key has no referrer restrictions; needs per-platform keys before a naive allowlist can be applied (a naive fix would break native auth) | Not rated in source; parked deliberately, not a quick-fix | launch-prep | c182 |
| .edu verification email cannot reach a real student: Resend has no verified sending domain, so delivery is restricted to the account's own address (proven both directions — a real send to the account owner succeeds, a send to an `.edu` address 502s) | Blocking dependency, not a vulnerability | Jose (c73, domain purchase, currently backlog) | c87, c73 |
| Messaging is transport-encrypted only today and must not be described as private or end-to-end encrypted in any user-facing or internal text; the E2EE epic (libsignal stubs, key handling) is stale relative to main post-rename and is being re-scoped | Standing labeling discipline, not a severity-rated finding | pool (c190's audit feeds a re-scoped epic for Jose + Q) | c190 |

---

## Superseded from the Aug 13 review

The Aug 13 doc's top-line status read "findings recorded, fixes NOT yet
applied." **That line is now wrong, not merely stale** — c27 (same day) and
c29 (Aug 14) shipped fixes for all 15 confirmed findings, and each fix left an
explicit `SECURITY-REVIEW finding N` comment at its call site, all of which
were re-opened and read this session:

- Finding 1 (moderator self-escalation / unscoped `list_reports`) —
  `backend/app/routers/moderation.py:217-262` now resolves and checks
  `campus_id` server-side before any report is readable.
- Finding 2 (invite-role escalation) —
  `backend/app/routers/chapters.py:579-589`: minting an e-board-role invite
  now requires the creator to already be president.
- Finding 3 (email squatting at bootstrap) —
  `backend/app/routers/auth.py:35-43`: in firebase mode, a `body.email` that
  disagrees with the verified token claim is rejected with 400.
- Finding 4 (auth tokens in logs) — already marked closed in the Aug 13 text
  itself (WS auth moved to subprotocol); still true, reinforced by c146.
- Finding 5 (CORS + credentials default) —
  `backend/app/main.py:158-178`: wildcard origin and credentialed CORS can
  never co-exist, and any non-local `env` refuses to boot with emulated auth
  or a wildcard origin (`RuntimeError`, not `assert`, deliberately).
- Findings 6-8 (TOCTOU check-then-insert races) — `IntegrityError` handling
  now present at every write site named in the original finding (`auth.py`,
  `chapters.py`, `chirps.py`, `feed.py`, `messages.py`, `moderation.py`) plus
  more added since (`events.py`, `house.py`, `payments.py`).
- Finding 9 (prekey pool drain) — per-(caller, target) rate limit, confirmed
  live at `backend/app/routers/keys.py:259-265`.
- Finding 10 (pagination losing tied-timestamp messages) — compound
  `(created_at, id)` cursor, confirmed at `backend/app/routers/messages.py:242`.
- Finding 11 (token never refreshed) — `onIdTokenChanged` wired in
  `app-mobile/src/auth/session.ts:58-60`.
- Finding 12 (no way to sign out) — `onPress` wired in
  `app-mobile/app/(tabs)/profile/index.tsx:387-390`, with a comment citing
  this exact finding.
- Finding 13 (account-type routing ignores selection) — fixed in
  `app-mobile/app/(auth)/account-type.tsx:97-100`, comment cites this finding.
- Finding 14 (chirp anonymity untested) —
  `backend/tests/test_chirps.py` now exists, header comment cites this
  finding explicitly.
- Finding 15 (cross-chapter 403 coverage gap) —
  `backend/tests/test_org_scoping.py` now covers dues-cycles, spend-approvals,
  and invites in addition to the original five groups.

The "Refuted / not findings" and "Verified CLEAN" sections of the Aug 13 doc
were a snapshot of that single review and are not re-verified here; treat
them as historical rather than current status. Nothing in them is known to be
wrong, but nothing in them has been re-checked against `main` as it stands
today either — **unverified as of this refresh.**

---

## What this refresh did not check

This is a docs-only refresh built from board.html plus a targeted set of
cheap, direct code reads (cited by file:line above). It is not a new
adversarial pass. In particular:

- Money path: no dedicated end-to-end adversarial sweep since dues/Stripe
  shipped, per the coverage-map note above. **Unverified as of this
  refresh.**
- The Aug 13 review's "Refuted / not findings" and "Verified CLEAN" items —
  historical, not re-checked. **Unverified as of this refresh.**
- E2EE / messaging crypto state — actively being re-audited in parallel
  (c190); do not treat this file as current on that lens until c190's output
  lands.
