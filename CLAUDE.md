# Chirp — instructions for agent sessions (both devs)

Applies to any coding agent, not just Claude Code — Cursor reads AGENTS.md, which
points here. One file, both tools.

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
- Surface blockers as their own card, including ones you created. A problem only you
  know about is the same as no board at all.
- **Run `scripts/board-check` before you commit board.html, and
  `scripts/board-check --pushed` after you push.** Not optional with several
  sessions on this repo. Four board changes vanished silently in one day and a
  JSON-parse check caught none of them: a card whose `col` was the column TITLE
  ("next") instead of its id ("backlog") rendered nowhere — seven at once, twice
  more after that, two of them DO-NOT-DEPLOY warnings; a stale copy reverted
  someone else's card with no conflict; and commits landed on whatever branch the
  shared tree happened to be checked out to and never reached origin. The script
  checks all of it. board.html also shows a red banner for unrenderable cards now,
  but that only helps whoever is looking at the page.

## Shared resources — claim on the board BEFORE you use them

- **Alembic migration numbers.** Two branches writing `0006` produce duplicate
  revision ids, and if one is already applied to prod the other is skipped SILENTLY —
  the tables never get created and nothing errors. Claim the next number on the board
  before writing the file. (This already happened once: c41.)
- **Card ids.** Same shape as migration numbers, and it already bit us: c113 was
  used by two branches and c114 by a third, none of them with a card, so the
  board's highest id read 112 and a session took 114 for unrelated work that was
  already two days old on someone else's branch (c116). **Write the card before you
  cut the branch.** Do NOT eyeball the board for the highest number —
  `scripts/board-check` prints the next genuinely free id, counting ids claimed in
  branch names and commit subjects as well as on the board, and FAILS if any id is
  in use without a card.
- Shared files — api/client, theme, components, mocks: touch sparingly, and say on the
  board when you do.

## Environment quirks (Jose's Intel Mac, macOS 12)

- NO Docker. Local Postgres 14: `pg_ctl -D /usr/local/var/postgresql@14 start`
  (brew services is broken). Roles chirp/chirp, dbs chirp + chirp_test.
- Backend venv: backend/.venv. Expo web: `npx expo start --web` on :8081 (Metro can
  get OOM-killed; just restart). gcloud lives at ~/google-cloud-sdk/bin/gcloud.
- Metro's transform cache does NOT invalidate on a plain restart — after editing a
  file, a restarted Metro can keep serving pre-edit bytes for several cycles. Pass
  `--clear` when a change refuses to show up (cost braul a phantom-bug chase, Aug 22).
  Port 8081 is also the ONLY origin in prod's CORS allowlist — a prod-pointed web
  session must own that port; use 8082+ for local-only render checks.
- Prod runbook + credentials: INFRA-PRIVATE.html at the repo root (gitignored —
  never commit it; Jose shares it dev-to-dev).

## Environment quirks (Q's Apple Silicon Mac)

- Docker IS available here, and there is NO native Postgres on 5432. Backend tests run
  against a disposable container, `chirp-test-pg` (postgres:16) on host port **5434**:
  `docker run -d --name chirp-test-pg -e POSTGRES_USER=chirp -e POSTGRES_PASSWORD=chirp -e POSTGRES_DB=chirp_test -p 5434:5432 postgres:16`
  Ports 5433 and 6379 belong to unrelated `leadgen-*` containers — do not take them.
- conftest defaults to 5432, so tests need the URL passed explicitly:
  `TEST_DATABASE_URL=postgresql+asyncpg://chirp:chirp@localhost:5434/chirp_test backend/.venv/bin/python -m pytest -q`
- backend/.venv is Python **3.12** (it was 3.9, which pyproject's `requires-python
  >=3.11` rejects; rebuilt Aug 13 on Homebrew python3.12).
- Because that container is postgres:16, test runs here match prod's PG16.
- The EAS dev build is STALE: expo-file-system, expo-sharing and
  @stripe/stripe-react-native were all added after it was cut (board c39).

## Multi-session lessons (Aug 23-24, Jose-approved) — ALWAYS ON

- **Worktree mobile checks.** A fresh worktree has no node_modules; `npx tsc` there
  fetches the npm PLACEHOLDER package named `tsc`, prints a red banner, exits 1, and
  type-checks NOTHING — the danger is a human misreading the red as an environment
  hiccup and reporting "tsc ran". Copy node_modules in with an APFS clone
  (`cp -Rc` from the main checkout) — a symlink satisfies tsc but BREAKS Metro's
  serverRoot. Any typecheck claim must cite `tsc --version`; no version, no check.
- **Never read an exit code through a pipe.** A pipeline reports its LAST stage's
  status — `cmd | head` returning 0 proves nothing about cmd. Capture unpiped or use
  PIPESTATUS. This exact mistake produced a false bug report once already.
- **Grep before build.** Cards record what was true the day they were written. Before
  designing from any card older than a couple of days: grep for the thing the card
  says does not exist, and `git log --oneline --grep` the card id AND its PREV-ID
  (renumbered cards' commits carry the old id). Three same-day tickets were already
  done on main; two commands would have caught each in minutes.
- **Board edits are edit+commit in one breath.** In the shared tree, the uncommitted
  window between editing board.html and committing is where a concurrent session's
  `git add` sweeps your lines into their commit — or a stale-copy commit drops them.
  Order: edit, commit immediately, `scripts/board-check` the COMMITTED state, amend
  if it fails, push, `board-check --pushed`.
- **Full test suites are serialized.** Five concurrent suites drove load to 384 and
  made clean runs blow 10-minute timeouts. Full backend runs go through
  `scripts/with-suite-lock`; targeted files run unlocked during development; a suite
  that timed out under load is a rerun, not a failure.

## Conventions that keep biting

- No emojis anywhere in code, UI, docs, or commits.
- DESIGN.md is law — no default-looking UI.
- Subagents/workflows run on Sonnet (cost).
- Every backend change that should go live needs a Cloud Run redeploy (human step,
  command in INFRA-PRIVATE.html) — say "redeploy now" plainly when it's time.
