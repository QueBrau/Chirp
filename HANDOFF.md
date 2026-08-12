# HANDOFF — current state

_Last updated: Aug 12 2026. Redesign merged; `main` is the only branch that matters._

## State of the world

- **`main`** has everything: verified backend scaffold (43 routes; keys/messages
  functional, Stripe stubbed), the redesigned Expo app (DESIGN.md v2 "Campus Modern" —
  emoji-free Feather-icon UI, floating tab bar, Orgs tab with member + find-your-org
  states, user-arrangeable profile, all-students copy), and `board.html` (the task
  board — open in a browser, drag cards, Export & commit to sync).
- Verified on merge: `tsc --noEmit` clean, zero emoji anywhere, every screen
  screenshot-QA'd in light mode at 390px, Edit-layout interaction tested live.
  Dark palette is token-complete but visually unverified (headless browser can't
  emulate prefers-color-scheme; check by flipping OS dark mode on localhost:8081).
- `frontend-redesign` branch is merged — safe to delete.

## Contracts (read before coding)

`SPEC.md` (product/schema/API), `CONVENTIONS.md` (naming, frozen signatures),
`app-mobile/DESIGN.md` (design system — binding; no emojis, tokens only).

## Top of the board (unclaimed — see board.html)

1. **Verify DB on a Docker machine**: `docker compose up -d && cd backend &&
   alembic upgrade head && pytest`. The migration + 10 skipped tests have NEVER
   run against real Postgres 16.
2. **libsignal RN spike** on two physical devices (milestone 3, riskiest unknown;
   needs EAS dev build).
3. **Real Firebase auth** (milestone 1): mobile sign-in + backend `auth_mode=firebase`.

## Working agreements

- All students, not just greek — greek = orgs you join (Orgs tab).
- board.html = who's doing what (J = Jose, Q = QueBrau).
- Claude sessions: subagents run Sonnet 5; resume context lives in Claude's
  project memory.
