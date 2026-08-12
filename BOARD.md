# Chirp — Task Board

In-repo kanban. Both devs edit this file like code: claim a card by putting your
name on it, move the line between sections as it progresses, commit with your
change ("board: claim E2EE spike"). Keep cards one line + optional indented notes.
Merge conflicts here are cheap — resolve in favor of whoever actually did the thing.

Owners: **Jose** (JoesyP) · **Q** (QueBrau) · unassigned = up for grabs.

---

## 🔨 In Progress

- [ ] **Frontend redesign** — `frontend-redesign` branch — @Jose (w/ Claude)
  - Dribbble-inspired modern/clean pass on theme + components + all screens.
  - Reframe: Chapter tab → Orgs tab (all-students product, greek opt-in).

## 🎯 Next (ready to pick up)

- [ ] **Verify DB layer on a Docker machine** — `docker compose up -d && alembic upgrade head && pytest` in `backend/`. 10 tests currently skip without Postgres; nobody has run the migration against real PG16 yet.
- [ ] **libsignal RN spike (milestone 3)** — encrypt/decrypt between two PHYSICAL devices. Highest-risk unknown in the whole plan; start early. Needs dev build (EAS), not Expo Go.
- [ ] **Real Firebase auth (milestone 1)** — mobile sign-in (Apple + Google + email) and backend `auth_mode=firebase` verification path; wire `POST /auth/bootstrap`.

## 📋 Backlog (build order = SPEC §7 milestones)

- [ ] Invite deep links end-to-end (`chirp://join-chapter`) — m1
- [ ] Role-gated org tab wired to real memberships API — m2
- [ ] E2EE DMs + groups + sender-key rotation + offline queue + push — m4 (after spike)
- [ ] Feed + Yak wired to real API, moderation flows (report/block/remove) — m5
- [ ] Lineage tree Skia canvas (interactive pan/pinch) — m6
- [ ] TestFlight with a real chapter — m7
- [ ] Stripe Connect onboarding + dues PaymentSheet — m8
- [ ] Treasurer + Secretary dashboards on real data + export — m9
- [ ] Alumni network + job board — m10
- [ ] Encrypted backups / new-phone recovery — m11

## ✅ Done

- [x] Full monorepo scaffold pushed to `main` (`5276103`) — backend (28-table schema, migration w/ append-only ledger trigger, 12 routers, WS gateway, §8 test suite) + Expo app (SDK 54, theme, 15 screens on mocks) — Jose + Claude, Aug 11
- [x] Backend verified: `create_app()` 43 routes, unauth→401, ledger GET/POST-only, yak API author-free; `tsc --noEmit` clean — Aug 11
- [x] Expo web preview running (`npx expo start --web` → localhost:8081) — Aug 11

## 🧭 Decisions log

- **Aug 11 — Product**: Chirp is for ALL students, not just greek. Greek chapters are orgs you join/register under the Orgs tab; same auth for everyone (matches SPEC §2.4 account types). Frontend copy/navigation reflects this; backend schema already supports it.
- **Aug 11 — Design**: scaffold default theme rejected as bland. Redesign direction from dribbble recon: soft neutral canvas, rounded cards, pill chips, floating tab bar, single confident accent + gradient moments, bold type scale. Tokens live in `app-mobile/src/theme`, spec in `app-mobile/DESIGN.md`.
- **Aug 11 — Process**: this file is the source of truth for who's doing what. If it's not on the board, it's not claimed.
