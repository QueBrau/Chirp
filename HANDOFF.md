# HANDOFF — where everything actually is

_Last updated: Aug 14 2026, night. main is through PR #6 (session provider +
/auth/me, Orgs on real memberships + invite UI + roster names); prod redeployed to
rev chirp-api-00006-smv and live-verified. `family-tree` is merged with main as of
PR #6 — the 8-file conflict is resolved, PLUS a real (non-textual) conflict PR #6
introduced: it added its own `MemberOut`/avatar_url solution to the same
"roster needs display names" problem this branch had already solved with a
`display_name` field bolted onto `MembershipOut`. Kept main's `MemberOut` (it's
the better shape — real INNER JOIN, non-null `display_name`, plus `avatar_url`
which this branch's version didn't have) and removed the now-dead
`MembershipOut.display_name` field on both backend and mobile. `q/social-msg`
still only has main through PR #5 — needs the same PR #6 catch-up repeated._

**board.html is the source of truth for tasks.** This file only answers the question the
board can't: which copy of Chirp you are looking at.

## Current state

| Where | What's there | State |
| --- | --- | --- |
| `main` | Everything through **PR #6**: Firebase live, session provider + /auth/me, Orgs on real memberships + invite UI + roster names (`MemberOut` with `avatar_url`), events backend, platform-admin chapter gating, CI workflow, migrations **0001-0007** | Canonical; prod matches it |
| Cloud Run (prod) | Live backend at `chirp-api-593616178468.us-central1.run.app` (rev 00006-smv) | `alembic_version` at **0007** = main's head (PR #6 shipped no migration) |
| `jose/auth-orgs` | Synced to main, kept for Jose's future work | Merged via PR #6 |
| `family-tree` | Yak + treasurer/secretary dashboards + export, merged with main through PR #6 | **PR #2**, was conflicting, now caught up locally (both conflict rounds) — needs push |
| `q/social-msg` | Everything in `family-tree`, plus Stripe Connect dues; merged with main through **PR #5 only** | **PR #3**, was DRAFT/blocked, migration collision resolved — still needs the PR #6 catch-up before push |
| CI | `.github/workflows/ci.yml`: backend pytest vs PG16 + mobile tsc on every push/PR | Green. No branch protection (deliberate, Aug 13) — CI is advisory |

Creds, runbooks, QA account, and live fixture ids: `INFRA-PRIVATE.html` at the repo root
(gitignored — get a copy from Jose, never commit it).

## The Stripe migration number (board c41) — RESOLVED

PR #3 shipped the Stripe migration as `0006`. That number was taken by Jose's events
migration, already applied to prod, with `0007` taken by `is_platform_admin`.

Left as a plain merge, prod (already past `0006`) would have had Alembic mark the
Stripe migration as already-applied and never run it — no `processed_stripe_events`,
no unique partial index on `ledger_entries.stripe_payment_intent_id`. Those are the
webhook-replay guards, so a Stripe retry would have appended a second dues payment to
an append-only ledger. Silent, and on the money path.

**Fixed**, on both `family-tree` and `q/social-msg`: renamed to `0008_stripe_dues.py`,
`revision = "0008"`, `down_revision = "0007"`. Verified the resulting chain is linear
with a single head (`0001` → ... → `0008`), not just that the file parses. Both
branches merged with `origin/main` at that point.

**Rule that came out of this (now in CLAUDE.md): claim your migration number on the board
before you write the file.**

## Verification status

- Both `family-tree` and `q/social-msg`, post-merge with main: **116 backend tests
  green** against postgres:16 (matches prod), run fresh through the full 0001-0008
  migration chain — not just "the file parses." tsc clean on both.
- Stripe is code-complete but **has never talked to real Stripe**. Nothing in the dues
  flow is proven until the keys are set (c40) and a test-mode payment runs on both card
  and ACH. Treat it as unverified.
- The mobile half of Stripe additionally can't be exercised until the EAS dev build is
  rebuilt (c39) — `expo-file-system`, `expo-sharing` and `@stripe/stripe-react-native`
  were all added after the current build was cut.
- Neither branch has been pushed yet as of this update.

## Environment

Two very different Macs, and the differences bite. Both are written up in **CLAUDE.md**:
Jose's Intel Mac (no Docker, local PG14) and Q's Apple Silicon Mac (Docker, postgres:16
on port 5434, venv on Python 3.12; `backend/.venv` itself is still Python 3.9 and
unusable against `pyproject.toml`'s `>=3.11` — every test run builds a throwaway venv
with Homebrew `python@3.12`). Prod credentials and the redeploy runbook live in
INFRA-PRIVATE.html at the repo root — gitignored, never commit it.

iOS Simulator was broken on Q's Mac this session (`launchd_sim` failing to bind a
session after an `xcode-select` switch) — a reboot is the known fix, not yet confirmed
resolved.

Every backend change that should go live needs a Cloud Run redeploy. It is a human step.
