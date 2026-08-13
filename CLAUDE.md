# Chirp — instructions for Claude sessions (both devs)

Read first: SPEC.md, CONVENTIONS.md, app-mobile/DESIGN.md (binding), HANDOFF.md,
SECURITY-REVIEW.md, DEPLOY.md. The live task board is board.html (open in a browser).

## Board discipline — ALWAYS ON

board.html is the source of truth for who is doing what, and BOTH devs watch it to
see what is going on. Update it at EVERY step, not just at the end of a task:

- **Claiming**: the moment you start a card, move it to "In Progress" with a one-line
  status appended to its title, commit, push.
- **Progress**: whenever a card's real-world state changes (built, tests green, blocked
  on a human step, deployed, verified), update the title to say so and push again.
  A card title should always answer "what is true right now and what is left".
- **Done**: move to Done only with evidence in the title (commit hash, test count,
  verified-live note).
- Board commits go STRAIGHT TO MAIN (the one exception to branch workflow), so the
  other dev always sees the live board. Feature code goes on per-dev branches
  (jose/*, q/*) and merges via PR.
- After every board change on main, merge main back into your working branch so the
  branches never diverge on board.html.
- Record product/process decisions in the board's Decisions log the day they happen.

## Environment quirks (Jose's Intel Mac, macOS 12)

- NO Docker. Local Postgres 14: `pg_ctl -D /usr/local/var/postgresql@14 start`
  (brew services is broken). Roles chirp/chirp, dbs chirp + chirp_test.
- Backend venv: backend/.venv. Expo web: `npx expo start --web` on :8081 (Metro can
  get OOM-killed; just restart). gcloud lives at ~/google-cloud-sdk/bin/gcloud.
- Prod runbook + credentials: INFRA-PRIVATE.html at the repo root (gitignored —
  never commit it; Jose shares it dev-to-dev).

## Conventions that keep biting

- No emojis anywhere in code, UI, docs, or commits.
- DESIGN.md is law — no default-looking UI.
- Subagents/workflows run on Sonnet (cost).
- Every backend change that should go live needs a Cloud Run redeploy (human step,
  command in INFRA-PRIVATE.html) — say "redeploy now" plainly when it's time.
