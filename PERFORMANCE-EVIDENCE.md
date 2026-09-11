# Query-plan evidence (c364)

Four query families have been in production since well before this card with no
EXPLAIN evidence on disk anywhere: people search (`GET /users/search`, c322), poll
reads (`GET /chapters/{id}/polls`, c162), the prekey bundle fetch (`GET
/users/{id}/prekey-bundle`), and the prekey/device quota counts (`GET
/devices/{id}/prekeys/count`). c208's "5,000 posts / 100,000 comments" evidence lives
only as prose on that board card, and c322's card carries no EXPLAIN numbers at all.
This card adds `backend/tests/test_c364_query_plans.py`, an opt-in harness
(`CHIRP_EXPLAIN=1`, never part of CI or the default suite) that seeds a realistic
multi-campus dataset, calls the actual production coroutines for each family through a
real session, captures the exact SQL each one emits, and runs `EXPLAIN (ANALYZE,
BUFFERS, FORMAT JSON)` against it. This document records what that run showed. It adds
no index and no migration — see "Recommendation" per family below.

## Cardinality and environment

Numbers actually used by the run this document reports (recorded exactly in
`infra/evidence/c364-query-plans-2026-09-11.json`'s `cardinality` and `postgres`
objects, and matching the plan's default cardinality, manager ruling R1, exactly — no
override was needed): 3 campuses x 5,000 users each (15,000 total), 30 chapters/campus
(90 total), the deep chapter carrying 80 polls (the most recent one seeded with an
exact, uneven 4-option vote split plus the harness caller's own vote), a 3% target
`user_blocks` rate, and 1-2 devices/user each with a 20-prekey one-time pool (the
designated quota-count device additionally carries 5 already-consumed prekeys, seeded
directly, to prove the count excludes them). Local Postgres on this Mac is
**PostgreSQL 14.20 (Homebrew)**, `server_encoding` **SQL_ASCII** (board c399) — every seeded display name, email and
poll question/option is plain ASCII on purpose, and prod/CI both run Postgres 16, so
these numbers are informative, not authoritative. A PG16 run is the one to trust before
acting on any of the recommendations below; obtain it by running this same harness
(`CHIRP_EXPLAIN=1 .venv/bin/python -m pytest tests/test_c364_query_plans.py -q`)
against a local PG16 instance, or a throwaway Cloud SQL PG16 instance, pointed at via
`TEST_DATABASE_URL`. There is no CI opt-in job for this in the current pass (manager
ruling R6).

## People search (`GET /users/search`)

`search_users` (`app/routers/messages.py`) runs one SELECT of `users` filtered by
`id IN (reachable_off_chapter_ids(caller))` and `display_name ILIKE '%needle%'`, then
`blockers_of` (`app/core/blocks.py`) to drop candidates who blocked the caller.
Constructed: a caller verified on a campus of 5,000 users, with exactly 15 of them
sharing a distinctive display-name token and the rest not — proving the ILIKE clause is
selective rather than a no-op (15 returned, not 0 and not the full campus).

- **Plan**: `Limit` at the top, with a `Seq Scan` on `users` underneath — no functional
  index backs the leading-wildcard ILIKE, so the planner has no choice but to scan the
  table.
- **Execution time**: 5.4ms at 15,000 users.
- **Recommendation**: no index added in this pass. 5.4ms at 15,000 users is not the
  "large execution time" this card's STOP condition (ruling R3) is about, so no
  follow-up card is opened from this run. There is no index on `users.display_name`
  today, and a leading-wildcard `ILIKE '%...%'` cannot use a plain B-tree index
  regardless of table size — only a trigram (`pg_trgm` GIN) index changes this plan's
  shape. If a future PG16 run at a larger, production-like campus population shows this
  climbing to an unacceptable execution time, that is the evidence-based trigger for a
  separate follow-up card proposing that migration — not something this card builds.

## `blockers_of` (shared by search and message/conversation creation)

Constructed: a subject with a candidate pool of 30 users, exactly 5 of whom block the
subject and 25 who do not — the result must be exactly those 5, not the full pool and
not empty.

- **Plan**: `Seq Scan` on `user_blocks`, both from within `search_users` (0 rows, the
  search caller has no blockers) and standalone (5 rows, the constructed case).
- **Execution time**: 0.066ms (from search) / 0.095ms (standalone).
- **Recommendation**: no index. `blocker_id` leads `user_blocks`' composite primary key
  `(blocker_id, blocked_id)`, and the plan's earlier guess (in the approved plan's
  `proposed_contract`) was that this would make an Index Scan the likely shape — that
  guess did NOT hold. At the seeded 3% block rate, `user_blocks` holds roughly 450 rows
  total at 15,000 users, and Postgres correctly prefers a Seq Scan over an index probe
  for a table this small, which is cheaper here regardless of the available index. The
  harness records the actual shape rather than trusting the assumption, which is the
  whole point of measuring instead of arguing from a docstring. No index is proposed;
  this table would need to grow by roughly two orders of magnitude before an index scan
  became the cheaper plan, and there is nothing in the product that would drive
  `user_blocks` to that size for one query's IN-list.

## Poll reads (`GET /chapters/{id}/polls`)

`list_polls` (`app/routers/polls.py`) runs the page SELECT (`chapter_id` filter,
`ORDER BY created_at DESC, id DESC`, `LIMIT`), a `selectinload` for each poll's
options, a `GROUP BY` tally over `poll_votes`, and a "mine" SELECT for the caller's own
ballots — three queries fetched once for the whole page regardless of poll count, per
the router's own docstring. Constructed: the deep chapter carries 80 polls (more than
the page limit of 50), so a real page must genuinely truncate; the most recent poll
carries an exact, deliberately uneven 4-option vote split plus the caller's own single
vote, so the tally and "mine" assertions check the real SHAPE (exact per-option counts,
`my_option_id` equal to exactly what this caller cast) rather than a status code.

- **Page select plan**: `Limit` at the top, no Seq Scan anywhere in the plan; 0.1ms.
- **Tally plan**: `Aggregate` at the top, WITH a `Seq Scan` on `poll_votes`
  underneath; 4.3ms.
- **Mine plan**: `Bitmap Heap Scan` on `poll_votes`, no Seq Scan; 0.27ms.
- **Recommendation**: no index added. The page select is fast (0.1ms) even without a
  composite `(chapter_id, created_at, id)` index backing the `ORDER BY` — today only a
  plain single-column index on `chapter_id` backs the equality filter, and a
  LIMIT-bounded sort over the matching rows is evidently cheap enough at this scale.
  The tally's `GROUP BY` chooses a Seq Scan over `poll_votes` because the 50-poll
  `IN (...)` list at this deep chapter's vote density covers a large fraction of that
  chapter's rows — the same "a Seq Scan is the CORRECT plan for low selectivity"
  behavior seen in `blockers_of` above, not evidence of a missing index. The
  caller's-own-ballot query adds a `user_id =` predicate that is selective enough that
  Postgres switches to a `Bitmap Heap Scan` on its own, using the `(poll_id, user_id)`
  composite primary key. No index is proposed for either query.

## Prekey bundle (`GET /users/{id}/prekey-bundle`) and quota counts (`GET
/devices/{id}/prekeys/count`)

`fetch_prekey_bundle` and `_prekey_count_out` (`app/routers/keys.py`) are the one
family where the correct plan shape is already an established fact from this card's
grounding pass: migrations 0001/0002/0035 already index every column these queries
filter on (`idx_devices_user_revoked_created`, `idx_signed_prekeys_device_created`,
the partial `idx_otk_available`/`idx_kyber_otk_available` on `consumed_at IS NULL`).
The harness therefore hard-codes a regression assertion here — `not has_seq_scan(...)`
— that the other three families deliberately do not. Constructed: a quota-count device
with exactly 20 unconsumed and 5 already-consumed one-time prekeys (available count
must read 20, not 25); a bundle-fetch target with two active devices, each with a
nonzero unconsumed pool, so the bundle covers both devices and hands back a real
(non-null) one-time prekey for each.

- **All 11 sub-plans** (devices select, 2x signed-prekey select, 2x OTK consume, 2x
  Kyber consume across the bundle's two devices, plus the 3 quota-count selects):
  **zero Seq Scans**, at 15,000 users / roughly 16,500 devices / roughly 330,000
  one-time-prekey rows and roughly 66,000 Kyber-prekey rows. The harness's own
  assertion (`not has_seq_scan(...)`) enforces this as a hard regression guard for this
  one family, unlike the other three above.
- **Recommendation**: none. The existing indexes (migrations 0001/0002/0035) already
  back every one of these queries; the harness's assertion is the regression guard
  against someone narrowing or dropping one of them later.

## What this run did NOT do

- No index and no migration were added, per the card's explicit instruction (manager
  ruling R3). If a follow-up PG16 run at production-scale cardinality shows the ILIKE
  plan as a large-execution-time sequential scan, that is the evidence-based trigger
  for a SEPARATE follow-up card proposing a `pg_trgm` GIN index on
  `users.display_name` — not something this card builds.
- No device-side profiling. The card's Q half (feed/roster render, list
  virtualization on a physical device) is untouched here; see the board card for that
  half's status.
- No CI job runs this harness. It is opt-in only
  (`CHIRP_EXPLAIN=1 .venv/bin/python -m pytest tests/test_c364_query_plans.py -q`,
  run alone, never inside a `pytest -q` invocation that also selects other test files —
  see the module's own docstring for why).
