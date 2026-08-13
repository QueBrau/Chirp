# Chirp — Security & Code Review (Aug 12 2026)

Multi-agent review of everything on `main` at commit `09c4492`. Five parallel
review lenses (authz, secrets/crypto, backend correctness, mobile correctness,
spec-compliance) → every medium+ finding re-checked by an adversarial verifier
told to refute it. 23 raised, 1 refuted, **15 confirmed** below. Many were
reproduced live against a real Postgres, not just read.

Status: **findings recorded, fixes NOT yet applied.** Fix work is carded on the
board. Fix on a branch off `main`, re-run `backend/` pytest, and add a regression
test per fix.

---

## CRITICAL

### 1. Platform-wide moderator self-escalation → reads plaintext of every reported E2EE message
`backend/app/routers/moderation.py:27` (`_require_any_eboard`, `list_reports`, `remove_yak`)

Anyone can `POST /chapters` and is auto-inserted as that chapter's **president**
(an EBOARD role) with no approval. Moderation routes then check only "is the
caller EBOARD of *any* chapter" — zero campus/chapter scoping. `list_reports`
does `select(ContentReport)` with **no WHERE clause**, returning every report
system-wide including `forwarded_plaintext` (the decrypted content of reported
private E2EE messages, per SPEC §6.7), and `remove_yak` can remove any campus's
yaks. Violates SPEC §2.3 and the whole "server never reads message content" promise.
**Fix:** scope moderation to the target's campus/chapter — resolve the report
target's chapter_id (or yak's campus_id) and require the caller's active EBOARD
membership *in that specific org*. Gate chapter creation too (self-serve
presidency is the enabler). Add moderation tests (currently zero).

---

## HIGH

### 2. Invite-role privilege escalation
`backend/app/routers/chapters.py:110` — `create_invite` is open to all EBOARD roles
and `ChapterInviteCreate.role` accepts any role incl. `president`; `join_chapter`
copies it verbatim. A historian can mint a president invite; anyone redeems it →
president. Asymmetric with `update_member` (president-only). **Fix:** cap invite
role at the creator's own role, or require president for EBOARD-granting invites.

### 3. Email squatting at bootstrap
`backend/app/routers/auth.py:21` — `POST /auth/bootstrap` trusts the client body
`email` and never checks it against the verified Firebase token's `email` claim.
An attacker bootstraps first with a victim's real email; the UNIQUE constraint then
permanently blocks the victim's own signup. **Fix:** in firebase mode, thread the
token's verified `email` claim through and ignore/verify `body.email`.

### 4. Auth tokens written to logs (SPEC §8.6 violation)
`backend/app/ws/gateway.py:26` — WS auth accepts `?token=<id-token>` in the URL;
uvicorn's default access log records the full request line, so real Firebase ID
tokens land in stdout/Cloud Run logs verbatim (reproduced live). The mobile client's
`wsUrl()` always uses the query param (RN WS can't set headers), so this is the
primary path. **Fix:** install a log filter that redacts `token=` from access logs
(and/or move WS auth to a subprotocol/first-frame). Add a "no token in logs" test.

### 5. Dangerous default config → cross-origin user impersonation
`backend/app/main.py:28` + `config.py` — defaults are `cors_origins=["*"]` +
`allow_credentials=True` + `auth_mode="emulated"` (trusts `X-Debug-Firebase-Uid`).
Starlette reflects any origin with credentials; any website's JS can send the debug
header and impersonate any uid (reproduced: evil-origin preflight echoed back
`allow-origin` + `allow-credentials`). **Fix:** never pair `*` with credentials
(force credentials off when origins is `*`); add an `env` setting (default `local`)
and refuse emulated mode + `*` origins when `env != local`. Keeps dev/tests working.

### 6-8. Check-then-insert TOCTOU races → 500 instead of clean 409/200 (all reproduced live)
Same shape in four spots — SELECT-then-INSERT with no `IntegrityError` guard, so a
double-tap/retry crashes with a bare 500:
- `chapters.py:146` `join_chapter` (memberships unique) — 4/40 concurrent → 500
- `yaks.py:90` `vote_yak` (yak_votes pk) — 4/20 → 500
- `feed.py:139` `like_post` (post_likes pk) + `moderation.py` `create_block` (user_blocks pk) — 4/20 each → 500
- (also noted: `messages.py` `upsert_receipt`, same shape, lower impact)
**Fix:** wrap in `try/except IntegrityError → rollback + conflict()`, or use
`INSERT ... ON CONFLICT`, matching the pattern `lineage.py create_edge` already uses.

---

## MEDIUM

### 9. Prekey pool drain (no rate limiting)
`backend/app/routers/keys.py:235` — `GET /users/{id}/prekey-bundle` consumes a
one-time prekey (EC + Kyber) on *every* call with no throttle; an attacker drains a
victim's pool without ever starting a session, forcing weaker last-resort-only
X3DH. **Fix:** rate-limit per (caller,target), and/or consume OTK on first message
rather than on bundle fetch. (Needs a rate-limit layer — larger effort.)

### 10. Message pagination silently loses tied-timestamp messages
`backend/app/routers/messages.py:208` — `before=` cursor uses `created_at <` only,
index is `created_at`-only. Messages sharing a timestamp at a page boundary appear
on neither page (reproduced: 5 same-timestamp msgs → 3 vanished from paginated view).
**Fix:** compound `(created_at, id)` cursor + matching index; client round-trips both.

### 11. Firebase token never refreshed
`app-mobile/src/auth/session.ts:45` — `onAuthChanged`/`getIdToken` are exported but
never subscribed; the ~1hr ID token goes stale with no refresh/retry path. **Fix:**
subscribe `onIdTokenChanged` at root → `setAuthToken`; retry once on 401. (Pairs with
the Firebase-project work.)

### 12. No way to sign out
`app-mobile/app/(tabs)/profile/index.tsx:294` — the "Sign out" row has no `onPress`
(and `ListRow` renders no Pressable without one), so `signOutUser()` is unreachable
from the UI. **Fix:** wire `onPress` → `signOutUser()` + redirect. (Quick.)

### 13. Account-type routing ignores selection
`app-mobile/app/(auth)/account-type.tsx:110` — button label branches on the choice
but `onPress` always routes to `/join-chapter`; a student/alum still gets the
chapter-code screen. **Fix:** branch onPress — only greek → join-chapter. (Quick.)

### 14 & 15. Test-coverage gaps against SPEC §8 (non-negotiables)
- **Yak anonymity untested** (`schemas/yak.py:24`): enforcement is correct
  (YakOut has no author field) but no test guards it. Add `test_yaks.py`.
- **Cross-chapter 403 test incomplete** (`tests/test_org_scoping.py:8`): covers 5 of
  8 `/chapters/{id}/*` groups — misses dues-cycles, spend-approvals, invites. SPEC
  §8.4 mandates the test for every such route. Extend it.

---

## Refuted / not findings
1 finding refuted in verification. Low-severity noted-not-filed: join-chapter deep
link stale param; api client can send both auth headers (no live harm today);
treasurer/secretary screens role-gate by hiding the entry card only (not the
destination) — worth server-trust hardening later; no test asserts ciphertext
absent from logs; no full-ASGI firebase-mode integration test.

## Verified CLEAN (explicitly checked, no issue)
Ledger append-only (route absence + DB trigger + test), private keys never stored
(only public material), prekey consumption atomicity (`FOR UPDATE SKIP LOCKED`,
incl. Kyber), Kyber last-resort never consumed, message/feed/lineage/finance
org-scoping, invite code entropy + expiry, Stripe webhook stub doesn't fake
verification, no raw-SQL injection, migration↔model parity, all CONVENTIONS frozen
contracts (Settings, `publish_to_user`, Role enum, 61 mounted routes).
