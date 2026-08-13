# HANDOFF — current state

_Last updated: Aug 13 2026. Everything below is on `main` (single source of truth).
Stale feature branches deleted. `board.html` = live task board._

## What's on main

Full Chirp app + today's work, all verified:
- **Backend**: 28-table schema + PQXDH kyber prekeys (migrations 0001–0003),
  12 routers, WS gateway, append-only ledger. **Security-hardened** per
  `SECURITY-REVIEW.md` (see below). 33 tests pass against real Postgres.
- **Mobile**: UNC Greensboro identity (navy/gold, campus-tint default), per-org
  greek colors via OrgAccentScope, media FYP (photo/video/text, story tiles,
  filter pills), navy campus-night Yak board, media-first craft pass (DESIGN §10),
  **Orgs space** = Feed / Events (Partiful-style RSVP) / Tools segments in org
  colors, org posts private to the org (never on FYP), user-arrangeable profile,
  Appearance screen, Firebase auth wired w/ demo fallback. tsc clean, zero emoji.

## Security review (SECURITY-REVIEW.md)

15 confirmed findings from the multi-agent review. **Fixed + tested this session:**
moderation campus-scoping (was: platform-wide E2EE-report plaintext leak),
invite-role escalation, bootstrap email-squatting, CORS/emulated dangerous
defaults, WS token log-redaction, 4× check-then-insert TOCTOU races
(join/vote/like/block/receipt), message pagination tie-break, + new tests
(moderation-scope, yaks, pagination, extended cross-chapter 403).

**Deferred to board (not quick fixes — need a decision or infra):**
- Prekey-drain rate limiting (needs a throttling layer)
- Firebase token auto-refresh (pairs with creating the Firebase project)
- **Fully gating chapter/president creation** — campus-scoping contains the
  moderation leak, but self-serve presidency is a PRODUCT decision (how do orgs
  legitimately get created + moderators anointed?). Jose/Q to design.
- Message index optimization: pagination query is correct but idx_messages_convo_time
  should become (conversation_id, created_at DESC, id DESC) via a 0004 migration.

## Environment notes (this Mac)

No Docker. Local Postgres 14 runs via `pg_ctl -D /usr/local/var/postgresql@14`
(brew services is broken here) — role chirp/chirp, dbs chirp + chirp_test.
Backend venv: `backend/.venv` (has firebase-admin, compiled once). Expo web:
`cd app-mobile && npx expo start --web` → localhost:8081 (Metro gets OOM-killed
when many agents run; just restart). Tests target real PG (verified on PG14;
prod is PG16 — re-run on a Docker/CI machine, carded).

## Top of the board for Q

Create the Firebase project (SETUP-FIREBASE.md), then real auth goes live;
libsignal on two physical devices (needs phones + Expo account); WS/Redis
real-time fan-out verify; PG16 test run. All carded in board.html.
