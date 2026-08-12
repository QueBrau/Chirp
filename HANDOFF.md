# HANDOFF — where we are and how to continue

_Last updated: Aug 11 2026, ~11:30pm ET. Session ended mid-redesign (token budget)._

## State of the world

- **`main`** = full working scaffold (verified) + `board.html` (the task board — open in a
  browser; drag cards, Export & commit to sync). Backend: 43 routes live, `create_app()`
  clean, pytest 1 passed / 10 skipped (DB tests need a Docker machine — **nobody has run
  the migration against real Postgres yet**). Mobile: 15 screens on mocks, tsc clean,
  `npx expo start --web` → localhost:8081.
- **`frontend-redesign`** (this branch) = WIP visual overhaul per `app-mobile/DESIGN.md`
  (the BINDING design contract — read it before touching UI). Committed here in
  whatever state the agents reached; **may not pass tsc** — finish before merging.

## Redesign progress (3-agent workflow, stopped mid-flight)

- DONE: design core — new theme tokens (`src/theme/*`), all components rebuilt +
  GradientAvatar / Chip / VotePill / HeroCard / SectionHeader, floating pill tab bar
  (Home / Yak / Messages / Orgs / Profile labels).
- PARTIAL: Home + Yak + Messages screens restyled BUT contain emojis (written before
  the no-emoji rule landed in DESIGN.md).
- INCOMPLETE: Orgs reframe + Profile + auth screens agent was killed mid-write.

## Remaining to finish the redesign (in order)

1. **Emoji sweep** — DESIGN.md now bans emojis everywhere ("Don'ts"). Replace with
   @expo/vector-icons Feather set (tab bar: home/radio/message-circle/grid/user;
   post actions: heart/message-circle; encrypted preview: lock icon + "Message").
   Yak: no masks/avatars — yakTint background + small tinted dot only.
2. **Orgs tab** (`app/(tabs)/chapter/`, label "Orgs") — member state (HeroCard + role
   chip + tool grid, treasurer/secretary tiles role-gated) AND non-member "Find your
   org" state behind `mockIsOrgMember` flag in `src/mocks/data.ts` (additive).
3. **Profile** — USER-ARRANGEABLE section cards (About / My Orgs / Activity / Alumni /
   Settings): Edit-layout mode, chevron up/down reorder, eye show/hide, mock
   persistence. Spec in DESIGN.md §7.
4. **Auth screens** restyle + copy reframe ("I'm a student / I'm in a fraternity or
   sorority / I'm an alum").
5. QA: `npx tsc --noEmit` clean, then screenshot every screen light+dark at 390px
   (headless browser vs localhost:8081), fix, merge to main.

Claude session note: prior workflow run id `wf_1d0aa482-6ce` (script in the old
session dir) — simplest is a FRESH small workflow for items 1–4; design core is
already on disk so agents just read `src/theme` + `src/components` and follow
DESIGN.md. **All subagents = Sonnet 5** (Jose's standing rule, in Claude's memory).

## Decisions locked in (also in board.html decisions log)

- Chirp is for ALL students; greek = orgs you join under the Orgs tab.
- DESIGN.md v2 is binding; default-looking UI and emojis are both banned.
- board.html is the source of truth for who's doing what (Jose=J, Q=QueBrau).

## Next up on the board (unclaimed)

- Verify DB on Docker machine: `docker compose up -d && cd backend && alembic upgrade head && pytest`
- libsignal RN spike on two physical devices (riskiest unknown, milestone 3)
- Real Firebase auth (milestone 1)
