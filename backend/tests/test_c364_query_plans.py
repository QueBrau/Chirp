"""Opt-in, read-only EXPLAIN evidence harness for four hot query families (board c364).

RUN THIS FILE ON ITS OWN, NEVER AS PART OF A NORMAL SUITE RUN:

    CHIRP_EXPLAIN=1 .venv/bin/python -m pytest tests/test_c364_query_plans.py -q

Every test below is skipped unless CHIRP_EXPLAIN=1 (see pytestmark). This module
defines its OWN module-scoped seed fixture (`explain_dataset`) rather than using the
per-test `client` fixture from conftest.py: `client` TRUNCATEs every table before each
test, which would wipe this file's bulk-seeded dataset between tests. Running this file
inside a `pytest -q` invocation that ALSO selects other test files is unsupported --
another file's `client` fixture truncating the shared run database mid-session would
empty this module's seed out from under a later test here.

DRIFT GUARD (manager ruling R5 on the c364 plan): nothing here hand-copies a WHERE
clause from production code. Every family calls the actual production coroutine
(`search_users`, `app.core.blocks.blockers_of`, `list_polls`, `keys.fetch_prekey_bundle`,
`keys._prekey_count_out`) through a real AsyncSession bound to the seeded database, with
a `before_cursor_execute` listener capturing the exact SQL text and parameters the
production code emits. EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) is then run against that
captured statement, verbatim, inside its own SAVEPOINT that is rolled back afterward --
so a mutating statement (the prekey consume UPDATE...RETURNING) is measured for real
(EXPLAIN ANALYZE always executes) without leaving its second consumption committed.

CARDINALITY (manager ruling R1): 3 campuses (fixed) x CHIRP_EXPLAIN_USERS users each
(default 5000, so 15,000 total) x CHIRP_EXPLAIN_CHAPTERS chapters/campus (default 30);
one "deep" chapter seeded with ~80 polls and ~130 votes/poll; a ~3% user_blocks rate;
1-2 devices per user with a ~20-of-400 prekey pool. The exact numbers used by a given
run are recorded inside the evidence file this harness writes, not just the defaults
documented here -- see `_CARDINALITY` below and infra/evidence/c364-query-plans-*.json.

NO INDEX AND NO MIGRATION ARE ADDED HERE (ruling R3). This harness only measures.
Any index decision is a separate, evidence-gated follow-up card.

Board c399's scratch-database fixture forces every test run's database to
`ENCODING 'UTF8' TEMPLATE template0`, regardless of what this Mac's own template1
defaults to (SQL_ASCII, ruling R6) -- so every seeded display name, email and poll
question/option here is still plain ASCII on purpose, but the connection itself is
UTF8, matching prod/CI. The evidence file records `server_version`/`server_encoding`
from the connection; prod and CI run Postgres 16 rather than this Mac's local 14, so
a run here is informative but NOT authoritative -- see PERFORMANCE-EVIDENCE.md for how
to obtain the authoritative PG16 numbers.
"""
from __future__ import annotations

import json
import os
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy import event, insert, text as sql_text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.db as app_db
from app import models
from app.core.blocks import blockers_of
from app.routers.keys import _prekey_count_out, fetch_prekey_bundle
from app.routers.messages import search_users
from app.routers.polls import list_polls

pytestmark = pytest.mark.skipif(
    os.environ.get("CHIRP_EXPLAIN") != "1",
    reason="opt-in performance harness (board c364) -- set CHIRP_EXPLAIN=1 to run",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_PATH = REPO_ROOT / "infra" / "evidence" / "c364-query-plans-2026-09-11.json"

CAMPUS_COUNT = 3
USERS_PER_CAMPUS = int(os.environ.get("CHIRP_EXPLAIN_USERS", "5000"))
CHAPTERS_PER_CAMPUS = int(os.environ.get("CHIRP_EXPLAIN_CHAPTERS", "30"))
BLOCK_RATE = 0.03
DEEP_CHAPTER_POLLS = 80
DEEP_CHAPTER_VOTE_RATE = 0.8
OTK_POOL = 20
KYBER_OTK_POOL = 5
NEEDLE_TOKEN = "Zephyrion"
NEEDLE_COUNT = 15
BLOCK_CANDIDATE_POOL = 30
BLOCK_CANDIDATE_BLOCKERS = 5

_CARDINALITY = {
    "campuses": CAMPUS_COUNT,
    "users_per_campus": USERS_PER_CAMPUS,
    "total_users": CAMPUS_COUNT * USERS_PER_CAMPUS,
    "chapters_per_campus": CHAPTERS_PER_CAMPUS,
    "total_chapters": CAMPUS_COUNT * CHAPTERS_PER_CAMPUS,
    "block_rate_target": BLOCK_RATE,
    "deep_chapter_polls": DEEP_CHAPTER_POLLS,
    "deep_chapter_vote_rate_target": DEEP_CHAPTER_VOTE_RATE,
    "one_time_prekey_pool_per_device": OTK_POOL,
    "kyber_one_time_pool_per_device": KYBER_OTK_POOL,
    "device_distribution": {"0_devices": 0.1, "1_device": 0.7, "2_devices": 0.2},
}


# --------------------------------------------------------------------------------
# EXPLAIN plumbing: capture the exact SQL production code emits, then EXPLAIN it.
# --------------------------------------------------------------------------------


@dataclass
class Captured:
    statements: list[tuple[str, Any]] = field(default_factory=list)


async def run_captured(engine: Engine, coro: Any) -> tuple[Any, list[tuple[str, Any]]]:
    """Await `coro` (which issues queries through a session bound to `engine`),
    capturing every statement the DBAPI cursor actually executes, in order."""
    captured = Captured()

    def _listener(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        captured.statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", _listener)
    try:
        result = await coro
    finally:
        event.remove(engine, "before_cursor_execute", _listener)
    return result, captured.statements


async def explain(session: AsyncSession, sql: str, params: Any) -> dict:
    """Run EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) on a captured statement+params,
    inside its own SAVEPOINT that is rolled back afterward -- EXPLAIN ANALYZE always
    executes the statement for real, so a mutating UPDATE...RETURNING (the prekey
    consume calls) is measured honestly without leaving a second consumption committed.
    """
    conn = await session.connection()
    nested = await session.begin_nested()
    try:
        cursor_result = await conn.exec_driver_sql(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params
        )
        raw = cursor_result.scalar_one()
    finally:
        await nested.rollback()
    plan = json.loads(raw) if isinstance(raw, str) else raw
    return plan[0]


def top_node_type(plan: dict) -> str:
    return plan["Plan"]["Node Type"]


def execution_time_ms(plan: dict) -> float:
    return plan.get("Execution Time", -1.0)


def has_seq_scan(plan: dict) -> bool:
    def _walk(node: dict) -> bool:
        if node.get("Node Type") == "Seq Scan":
            return True
        return any(_walk(child) for child in node.get("Plans", []))

    return _walk(plan["Plan"])


# --------------------------------------------------------------------------------
# Evidence accumulation + write-on-teardown (ruling R7).
# --------------------------------------------------------------------------------


@dataclass
class Evidence:
    entries: list[dict] = field(default_factory=list)

    def record(
        self,
        *,
        family: str,
        query: str,
        sql: str,
        row_count: int | None,
        plan: dict,
    ) -> None:
        self.entries.append(
            {
                "family": family,
                "query": query,
                "sql": sql,
                "row_count": row_count,
                "plan_top_node_type": top_node_type(plan),
                "execution_time_ms": execution_time_ms(plan),
                "has_seq_scan": has_seq_scan(plan),
                "plan": plan,
            }
        )


@pytest.fixture(scope="module")
def evidence() -> Evidence:
    return Evidence()


# --------------------------------------------------------------------------------
# Seeding.
# --------------------------------------------------------------------------------


def _ascii_name(*parts: str) -> str:
    return " ".join(parts)


@dataclass
class Handles:
    engine: Engine
    factory: async_sessionmaker[AsyncSession]
    pg_version: str
    pg_encoding: str

    # people search
    search_caller_id: uuid.UUID
    search_needle: str
    search_needle_match_ids: list[uuid.UUID]
    search_campus_population: int

    # blockers_of
    blockers_subject_id: uuid.UUID
    blockers_candidate_ids: list[uuid.UUID]
    blockers_expected_blocker_ids: set[uuid.UUID]

    # polls
    deep_chapter_id: uuid.UUID
    deep_chapter_poll_ids: list[uuid.UUID]
    designated_poll_id: uuid.UUID
    designated_poll_option_counts: dict[uuid.UUID, int]
    designated_poll_total_votes: int
    polls_caller_id: uuid.UUID
    polls_caller_option_id: uuid.UUID

    # prekey counts
    prekey_count_user_id: uuid.UUID
    prekey_count_device_id: uuid.UUID
    prekey_count_otk_unconsumed: int
    prekey_count_otk_consumed: int
    prekey_count_kyber_unconsumed: int

    # prekey bundle
    prekey_bundle_requester_id: uuid.UUID
    prekey_bundle_target_id: uuid.UUID
    prekey_bundle_device_ids: list[uuid.UUID]
    prekey_bundle_revoked_device_id: uuid.UUID


async def _bulk(session: AsyncSession, model: type, rows: list[dict], chunk: int = 5000) -> None:
    if not rows:
        return
    stmt = insert(model)
    for i in range(0, len(rows), chunk):
        await session.execute(stmt, rows[i : i + chunk])


async def _seed(session: AsyncSession) -> Handles:
    now = datetime.now(timezone.utc)
    rng = random.Random(20260911)

    version_row = (
        await session.execute(sql_text("SELECT version(), current_setting('server_encoding')"))
    ).one()
    pg_version, pg_encoding = version_row[0], version_row[1]

    # ---- campuses + chapters ----
    campus_ids = [uuid.uuid4() for _ in range(CAMPUS_COUNT)]
    await _bulk(
        session,
        models.Campus,
        [
            {"id": cid, "name": f"Campus {i}", "slug": f"c364-campus-{i}", "email_domains": None}
            for i, cid in enumerate(campus_ids)
        ],
    )
    chapter_ids: list[list[uuid.UUID]] = []
    chapter_rows = []
    for ci, cid in enumerate(campus_ids):
        chs = [uuid.uuid4() for _ in range(CHAPTERS_PER_CAMPUS)]
        chapter_ids.append(chs)
        for chi, chid in enumerate(chs):
            chapter_rows.append(
                {
                    "id": chid,
                    "campus_id": cid,
                    "org_name": f"Org {ci}-{chi}",
                    "chapter_name": f"Chapter {ci}-{chi}",
                    "moderation_approved": False,
                }
            )
    await _bulk(session, models.Chapter, chapter_rows)
    deep_chapter_id = chapter_ids[0][0]

    # ---- users (campus 0 gets the reserved deterministic indices) ----
    RESERVED_NEEDLE = list(range(0, NEEDLE_COUNT))  # 0..14
    RESERVED_SEARCH_CALLER = NEEDLE_COUNT  # 15
    RESERVED_BLOCK_SUBJECT = NEEDLE_COUNT + 1  # 16
    RESERVED_BLOCK_CANDIDATES = list(
        range(NEEDLE_COUNT + 2, NEEDLE_COUNT + 2 + BLOCK_CANDIDATE_POOL)
    )  # 17..46
    RESERVED_PREKEY_COUNT_USER = RESERVED_BLOCK_CANDIDATES[-1] + 1  # 47
    RESERVED_PREKEY_BUNDLE_TARGET = RESERVED_PREKEY_COUNT_USER + 1  # 48
    RESERVED_PREKEY_BUNDLE_REQUESTER = RESERVED_PREKEY_BUNDLE_TARGET + 1  # 49
    reserved = set(
        RESERVED_NEEDLE
        + [RESERVED_SEARCH_CALLER, RESERVED_BLOCK_SUBJECT]
        + RESERVED_BLOCK_CANDIDATES
        + [RESERVED_PREKEY_COUNT_USER, RESERVED_PREKEY_BUNDLE_TARGET, RESERVED_PREKEY_BUNDLE_REQUESTER]
    )

    user_ids: list[list[uuid.UUID]] = []
    home_chapter: dict[uuid.UUID, uuid.UUID] = {}
    verified: dict[uuid.UUID, bool] = {}
    user_rows = []
    for ci, cid in enumerate(campus_ids):
        campus_user_ids = []
        for i in range(USERS_PER_CAMPUS):
            uid = uuid.uuid4()
            campus_user_ids.append(uid)
            is_verified = rng.random() < 0.6
            display = _ascii_name("Student", f"{ci}", f"{i:05d}")
            if ci == 0 and i in RESERVED_NEEDLE:
                display = _ascii_name(NEEDLE_TOKEN, f"{i:03d}")
                is_verified = True
            elif ci == 0 and i == RESERVED_SEARCH_CALLER:
                is_verified = True
            chapter = chapter_ids[ci][i % CHAPTERS_PER_CAMPUS]
            if ci == 0 and i == RESERVED_SEARCH_CALLER:
                chapter = deep_chapter_id
            home_chapter[uid] = chapter
            verified[uid] = is_verified
            user_rows.append(
                {
                    "id": uid,
                    "firebase_uid": f"c364-explain-{ci}-{i}",
                    "email": f"c364-explain-{ci}-{i}@campus{ci}.edu.invalid",
                    "display_name": display,
                    "account_type": "greek",
                    "campus_id": cid,
                    "is_ghost": False,
                    "suspended_at": None,
                    "campus_verified_at": now if is_verified else None,
                }
            )
        user_ids.append(campus_user_ids)
    await _bulk(session, models.User, user_rows)

    campus0 = user_ids[0]
    search_needle_match_ids = [campus0[i] for i in RESERVED_NEEDLE]
    search_caller_id = campus0[RESERVED_SEARCH_CALLER]
    blockers_subject_id = campus0[RESERVED_BLOCK_SUBJECT]
    blockers_candidate_ids = [campus0[i] for i in RESERVED_BLOCK_CANDIDATES]
    blockers_expected = set(blockers_candidate_ids[:BLOCK_CANDIDATE_BLOCKERS])
    prekey_count_user_id = campus0[RESERVED_PREKEY_COUNT_USER]
    prekey_bundle_target_id = campus0[RESERVED_PREKEY_BUNDLE_TARGET]
    prekey_bundle_requester_id = campus0[RESERVED_PREKEY_BUNDLE_REQUESTER]

    # ---- memberships (one active membership per user, their home chapter) ----
    membership_rows = []
    deep_chapter_member_ids: list[uuid.UUID] = []
    for ci, campus_user_ids in enumerate(user_ids):
        for uid in campus_user_ids:
            chid = home_chapter[uid]
            membership_rows.append(
                {
                    "id": uuid.uuid4(),
                    "user_id": uid,
                    "chapter_id": chid,
                    "role": "member",
                    "status": "active",
                }
            )
            if chid == deep_chapter_id:
                deep_chapter_member_ids.append(uid)
    await _bulk(session, models.Membership, membership_rows)

    # polls_caller: the first deep-chapter member that is not another reserved id.
    polls_caller_id = next(
        uid for uid in deep_chapter_member_ids if uid != search_caller_id
    )

    # ---- user_blocks: ~3% general rate (random pairs, excluding reserved ids that
    # the blockers_of test controls deterministically) + the 5 explicit blockers. ----
    all_ids = [uid for campus_user_ids in user_ids for uid in campus_user_ids]
    reserved_ids = set(search_needle_match_ids) | {
        search_caller_id,
        blockers_subject_id,
        *blockers_candidate_ids,
        prekey_count_user_id,
        prekey_bundle_target_id,
        prekey_bundle_requester_id,
    }
    general_pool = [uid for uid in all_ids if uid not in reserved_ids]
    n_blocks = int(len(all_ids) * BLOCK_RATE)
    block_rows = []
    seen_pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()
    attempts = 0
    while len(block_rows) < n_blocks and attempts < n_blocks * 4:
        attempts += 1
        blocker = rng.choice(general_pool)
        blocked = rng.choice(general_pool)
        if blocker == blocked or (blocker, blocked) in seen_pairs:
            continue
        seen_pairs.add((blocker, blocked))
        block_rows.append(
            {
                "blocker_id": blocker,
                "blocked_id": blocked,
                "source": "named",
                "anonymous_created_at": None,
            }
        )
    for blocker in blockers_expected:
        block_rows.append(
            {
                "blocker_id": blocker,
                "blocked_id": blockers_subject_id,
                "source": "named",
                "anonymous_created_at": None,
            }
        )
    await _bulk(session, models.UserBlock, block_rows)

    # ---- devices + prekeys ----
    device_rows = []
    signed_rows = []
    otk_rows = []
    kyber_rows = []

    def _add_device(
        uid: uuid.UUID,
        *,
        otk_unconsumed: int,
        otk_consumed: int,
        kyber_unconsumed: int,
        revoked_at: datetime | None = None,
    ) -> uuid.UUID:
        did = uuid.uuid4()
        device_rows.append(
            {
                "id": did,
                "user_id": uid,
                "device_label": "c364-explain",
                "registration_id": rng.randint(1, 1_000_000),
                "identity_key": b"c364-identity-key",
                "revoked_at": revoked_at,
            }
        )
        signed_rows.append(
            {
                "id": uuid.uuid4(),
                "device_id": did,
                "key_id": 1,
                "public_key": b"c364-signed-public",
                "signature": b"c364-signed-signature",
            }
        )
        for k in range(otk_unconsumed):
            otk_rows.append(
                {
                    "id": uuid.uuid4(),
                    "device_id": did,
                    "key_id": k + 1,
                    "public_key": b"c364-otk-public",
                    "consumed_at": None,
                }
            )
        for k in range(otk_consumed):
            otk_rows.append(
                {
                    "id": uuid.uuid4(),
                    "device_id": did,
                    "key_id": otk_unconsumed + k + 1,
                    "public_key": b"c364-otk-public-consumed",
                    "consumed_at": now - timedelta(days=1),
                }
            )
        kyber_rows.append(
            {
                "id": uuid.uuid4(),
                "device_id": did,
                "key_id": 0,
                "public_key": b"c364-kyber-lastresort-public",
                "signature": b"c364-kyber-lastresort-sig",
                "is_last_resort": True,
                "consumed_at": None,
            }
        )
        for k in range(kyber_unconsumed):
            kyber_rows.append(
                {
                    "id": uuid.uuid4(),
                    "device_id": did,
                    "key_id": k + 1,
                    "public_key": b"c364-kyber-otk-public",
                    "signature": b"c364-kyber-otk-sig",
                    "is_last_resort": False,
                    "consumed_at": None,
                }
            )
        return did

    for campus_user_ids in user_ids:
        for uid in campus_user_ids:
            if uid in (prekey_count_user_id, prekey_bundle_target_id):
                continue  # handled deterministically below
            roll = rng.random()
            n_devices = 1 if roll < 0.7 else (2 if roll < 0.9 else 0)
            for _ in range(n_devices):
                _add_device(uid, otk_unconsumed=OTK_POOL, otk_consumed=0, kyber_unconsumed=KYBER_OTK_POOL)

    prekey_count_device_id = _add_device(
        prekey_count_user_id, otk_unconsumed=OTK_POOL, otk_consumed=5, kyber_unconsumed=OTK_POOL - 5
    )
    prekey_bundle_device_ids = [
        _add_device(prekey_bundle_target_id, otk_unconsumed=OTK_POOL, otk_consumed=0, kyber_unconsumed=KYBER_OTK_POOL)
        for _ in range(2)
    ]
    # A third device on the same target, revoked, with its own nonzero available
    # prekey pool -- fetch_prekey_bundle's `Device.revoked_at.is_(None)` filter must
    # exclude it. Without a revoked device actually present in the seed, that filter
    # has zero discriminating test coverage (a dropped/narrowed filter would still
    # return exactly the 2 active devices and every assertion below would stay green).
    prekey_bundle_revoked_device_id = _add_device(
        prekey_bundle_target_id,
        otk_unconsumed=OTK_POOL,
        otk_consumed=0,
        kyber_unconsumed=KYBER_OTK_POOL,
        revoked_at=now,
    )

    await _bulk(session, models.Device, device_rows)
    await _bulk(session, models.SignedPrekey, signed_rows)
    await _bulk(session, models.OneTimePrekey, otk_rows)
    await _bulk(session, models.KyberPrekey, kyber_rows)

    # ---- polls: the deep chapter gets DEEP_CHAPTER_POLLS, spread over ~2 years so
    # the most recently created one (index DEEP_CHAPTER_POLLS-1) sorts first under
    # (created_at DESC, id DESC) and is guaranteed to land on page one. Every OTHER
    # chapter gets exactly one poll, so chapter_id selectivity in the EXPLAIN is real
    # rather than "poll_id is basically the whole table". ----
    poll_rows = []
    option_rows = []
    vote_rows = []
    base_time = now - timedelta(days=730)

    deep_poll_ids = []
    designated_poll_id = None
    designated_option_counts: dict[uuid.UUID, int] = {}
    designated_total_votes = 0
    designated_caller_option: uuid.UUID | None = None

    n_voters = min(int(len(deep_chapter_member_ids) * DEEP_CHAPTER_VOTE_RATE), len(deep_chapter_member_ids))

    for p in range(DEEP_CHAPTER_POLLS):
        pid = uuid.uuid4()
        deep_poll_ids.append(pid)
        created_at = base_time + timedelta(minutes=p)
        poll_rows.append(
            {
                "id": pid,
                "chapter_id": deep_chapter_id,
                "meeting_id": None,
                "question": f"c364 explain poll {p}",
                "status": "open" if p == DEEP_CHAPTER_POLLS - 1 else "closed",
                "created_by": polls_caller_id,
                "created_at": created_at,
                "closed_at": None,
            }
        )
        option_ids = [uuid.uuid4() for _ in range(4)]
        for pos, oid in enumerate(option_ids):
            option_rows.append({"id": oid, "poll_id": pid, "text_": f"Option {pos}", "position": pos})

        is_designated = p == DEEP_CHAPTER_POLLS - 1
        voters = deep_chapter_member_ids[:n_voters]
        if is_designated:
            designated_poll_id = pid
            # Exact, deliberately uneven split across all 4 options so the tally
            # assertion discriminates a dropped GROUP BY from a real per-option count.
            base_counts = [max(1, n_voters // 4 + 3), max(1, n_voters // 4), max(1, n_voters // 4 - 2), 0]
            base_counts[3] = max(0, n_voters - sum(base_counts[:3]))
            idx = 0
            for oi, count in enumerate(base_counts):
                for _ in range(count):
                    voter = voters[idx]
                    idx += 1
                    vote_rows.append(
                        {
                            "poll_id": pid,
                            "user_id": voter,
                            "option_id": option_ids[oi],
                            "created_at": created_at,
                        }
                    )
                    if voter == polls_caller_id:
                        designated_caller_option = option_ids[oi]
                designated_option_counts[option_ids[oi]] = count
            designated_total_votes = sum(designated_option_counts.values())
            if designated_caller_option is None:
                # polls_caller must always be a voter here -- force it deterministically
                # rather than leaving my_option_id accidentally None.
                designated_caller_option = option_ids[0]
                vote_rows.append(
                    {
                        "poll_id": pid,
                        "user_id": polls_caller_id,
                        "option_id": designated_caller_option,
                        "created_at": created_at,
                    }
                )
                designated_option_counts[designated_caller_option] += 1
                designated_total_votes += 1
        else:
            for voter in voters:
                option_id = option_ids[hash((p, voter)) % 4]
                vote_rows.append(
                    {"poll_id": pid, "user_id": voter, "option_id": option_id, "created_at": created_at}
                )

    for ci, chs in enumerate(chapter_ids):
        for chi, chid in enumerate(chs):
            if chid == deep_chapter_id:
                continue
            pid = uuid.uuid4()
            poll_rows.append(
                {
                    "id": pid,
                    "chapter_id": chid,
                    "meeting_id": None,
                    "question": f"c364 explain sparse poll {ci}-{chi}",
                    "status": "open",
                    "created_by": polls_caller_id,
                    "created_at": now,
                    "closed_at": None,
                }
            )
            for pos in range(4):
                option_rows.append(
                    {"id": uuid.uuid4(), "poll_id": pid, "text_": f"Option {pos}", "position": pos}
                )

    await _bulk(session, models.Poll, poll_rows)
    await _bulk(session, models.PollOption, option_rows)
    await _bulk(session, models.PollVote, vote_rows)

    await session.commit()

    assert designated_poll_id is not None
    assert designated_caller_option is not None

    return Handles(
        engine=app_db.get_engine().sync_engine,
        factory=None,  # filled in by the fixture below
        pg_version=pg_version,
        pg_encoding=pg_encoding,
        search_caller_id=search_caller_id,
        search_needle=NEEDLE_TOKEN,
        search_needle_match_ids=search_needle_match_ids,
        search_campus_population=USERS_PER_CAMPUS,
        blockers_subject_id=blockers_subject_id,
        blockers_candidate_ids=blockers_candidate_ids,
        blockers_expected_blocker_ids=blockers_expected,
        deep_chapter_id=deep_chapter_id,
        deep_chapter_poll_ids=deep_poll_ids,
        designated_poll_id=designated_poll_id,
        designated_poll_option_counts=designated_option_counts,
        designated_poll_total_votes=designated_total_votes,
        polls_caller_id=polls_caller_id,
        polls_caller_option_id=designated_caller_option,
        prekey_count_user_id=prekey_count_user_id,
        prekey_count_device_id=prekey_count_device_id,
        prekey_count_otk_unconsumed=OTK_POOL,
        prekey_count_otk_consumed=5,
        prekey_count_kyber_unconsumed=OTK_POOL - 5,
        prekey_bundle_requester_id=prekey_bundle_requester_id,
        prekey_bundle_target_id=prekey_bundle_target_id,
        prekey_bundle_device_ids=prekey_bundle_device_ids,
        prekey_bundle_revoked_device_id=prekey_bundle_revoked_device_id,
    )


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def explain_dataset(migrated_db: str) -> AsyncIterator[Handles]:
    app_db._engine = None
    app_db._session_factory = None
    factory = app_db.get_session_factory()
    async with factory() as session:
        handles = await _seed(session)
    handles.factory = factory
    yield handles
    await app_db.get_engine().dispose()


@pytest.fixture(scope="module", autouse=True)
def _write_evidence_on_teardown(request: pytest.FixtureRequest, evidence: Evidence, explain_dataset: Handles) -> Iterator[None]:
    yield
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "cardinality": _CARDINALITY,
        "postgres": {
            "server_version": explain_dataset.pg_version,
            "server_encoding": explain_dataset.pg_encoding,
            "note": (
                "Local Homebrew Postgres -- informative only. Prod and CI run "
                "Postgres 16; see PERFORMANCE-EVIDENCE.md for the authoritative run."
            ),
        },
        "queries": evidence.entries,
    }
    EVIDENCE_PATH.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------------
# Tests: one per query family.
# --------------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="module")
async def test_people_search_ilike_plan(explain_dataset: Handles, evidence: Evidence) -> None:
    ds = explain_dataset
    async with ds.factory() as session:
        caller = await session.get(models.User, ds.search_caller_id)
        assert caller is not None

        result, statements = await run_captured(
            ds.engine, search_users(q=ds.search_needle, user=caller, session=session)
        )

        # Constructed discriminating condition, asserted BEFORE trusting the EXPLAIN:
        # exactly NEEDLE_COUNT users share the needle token and all of them are inside
        # the caller's reachable (whole-campus, since the caller is verified) set,
        # which is far larger -- proves the ILIKE clause is selective, not a no-op.
        returned_ids = {row.id for row in result}
        assert returned_ids == set(ds.search_needle_match_ids)
        assert len(returned_ids) == NEEDLE_COUNT
        assert len(returned_ids) < ds.search_campus_population
        assert all(ds.search_needle in row.display_name for row in result)

        assert len(statements) == 2, "search_users should emit exactly 2 statements: the ILIKE select, then blockers_of"
        sql, params = statements[0]
        plan = await explain(session, sql, params)
        evidence.record(
            family="people_search",
            query="ilike_reachable_select",
            sql=sql,
            row_count=len(returned_ids),
            plan=plan,
        )

        blocker_sql, blocker_params = statements[1]
        blocker_plan = await explain(session, blocker_sql, blocker_params)
        evidence.record(
            family="people_search",
            query="blockers_of_from_search",
            sql=blocker_sql,
            row_count=0,
            plan=blocker_plan,
        )
        await session.rollback()


@pytest.mark.asyncio(loop_scope="module")
async def test_blockers_of_plan(explain_dataset: Handles, evidence: Evidence) -> None:
    ds = explain_dataset
    async with ds.factory() as session:
        result, statements = await run_captured(
            ds.engine,
            blockers_of(session, subject_id=ds.blockers_subject_id, candidate_ids=ds.blockers_candidate_ids),
        )

        # Constructed: exactly BLOCK_CANDIDATE_BLOCKERS of the candidate pool block the
        # subject; the rest do not. The result must be exactly that subset -- neither
        # the full pool nor empty.
        assert result == ds.blockers_expected_blocker_ids
        assert 0 < len(result) < len(ds.blockers_candidate_ids)

        assert len(statements) == 1
        sql, params = statements[0]
        plan = await explain(session, sql, params)
        evidence.record(
            family="blockers_of",
            query="blockers_of_select",
            sql=sql,
            row_count=len(result),
            plan=plan,
        )
        await session.rollback()


@pytest.mark.asyncio(loop_scope="module")
async def test_list_polls_page_plan(explain_dataset: Handles, evidence: Evidence) -> None:
    ds = explain_dataset
    async with ds.factory() as session:
        membership = models.Membership(user_id=ds.polls_caller_id, chapter_id=ds.deep_chapter_id, role="member", status="active")

        result, statements = await run_captured(
            ds.engine,
            list_polls(
                chapter_id=ds.deep_chapter_id,
                meeting_id=None,
                before=None,
                before_id=None,
                limit=50,
                membership=membership,
                session=session,
            ),
        )

        # Constructed: the deep chapter has DEEP_CHAPTER_POLLS (80) polls, more than
        # the page limit (50), so a real page must genuinely truncate.
        assert len(result) == 50
        assert all(poll.chapter_id == ds.deep_chapter_id for poll in result)
        # Newest-first ordering actually holds (created_at DESC, id DESC).
        created_ats = [poll.created_at for poll in result]
        assert created_ats == sorted(created_ats, reverse=True)

        assert len(statements) == 4, "list_polls should emit exactly 4 statements: page select, options selectinload, tally, mine"
        sql, params = statements[0]
        plan = await explain(session, sql, params)
        evidence.record(family="list_polls", query="page_select", sql=sql, row_count=len(result), plan=plan)
        await session.rollback()


@pytest.mark.asyncio(loop_scope="module")
async def test_poll_tally_and_mine_plan(explain_dataset: Handles, evidence: Evidence) -> None:
    ds = explain_dataset
    async with ds.factory() as session:
        membership = models.Membership(user_id=ds.polls_caller_id, chapter_id=ds.deep_chapter_id, role="member", status="active")

        result, statements = await run_captured(
            ds.engine,
            list_polls(
                chapter_id=ds.deep_chapter_id,
                meeting_id=None,
                before=None,
                before_id=None,
                limit=50,
                membership=membership,
                session=session,
            ),
        )
        designated = next(p for p in result if p.id == ds.designated_poll_id)

        # Constructed: the designated poll has votes on ALL 4 options at an exact,
        # known, deliberately uneven split, plus the caller's own single vote.
        actual_counts = {opt.id: opt.votes for opt in designated.options}
        assert actual_counts == ds.designated_poll_option_counts
        assert designated.total_votes == ds.designated_poll_total_votes == sum(actual_counts.values())
        assert designated.my_option_id == ds.polls_caller_option_id
        # Ballot secrecy shape: my_option_id is never a DIFFERENT voter's choice --
        # it equals exactly what this harness assigned to polls_caller, nothing else.

        # statements: [0]=page select, [1]=options selectinload, [2]=tally, [3]=mine.
        assert len(statements) == 4
        tally_sql, tally_params = statements[2]
        tally_plan = await explain(session, tally_sql, tally_params)
        evidence.record(family="poll_tally", query="tally_group_by", sql=tally_sql, row_count=len(actual_counts), plan=tally_plan)

        mine_sql, mine_params = statements[3]
        mine_plan = await explain(session, mine_sql, mine_params)
        evidence.record(family="poll_tally", query="mine_select", sql=mine_sql, row_count=1, plan=mine_plan)
        await session.rollback()


@pytest.mark.asyncio(loop_scope="module")
async def test_prekey_count_plan(explain_dataset: Handles, evidence: Evidence) -> None:
    ds = explain_dataset
    async with ds.factory() as session:
        result, statements = await run_captured(
            ds.engine, _prekey_count_out(session, ds.prekey_count_device_id)
        )

        # Constructed: exactly OTK_POOL unconsumed + 5 already-consumed one-time
        # prekeys were seeded for this device -- the count must reflect only the
        # unconsumed ones, not the full retained pool (25).
        assert result.one_time_prekeys_available == ds.prekey_count_otk_unconsumed
        assert result.one_time_prekeys_available < ds.prekey_count_otk_unconsumed + ds.prekey_count_otk_consumed
        assert result.kyber_one_time_prekeys_available == ds.prekey_count_kyber_unconsumed
        assert result.kyber_last_resort_registered is True

        assert len(statements) == 3, "_prekey_count_out should emit exactly 3 statements: otk count, kyber count, last-resort check"
        names = ["otk_available_count", "kyber_available_count", "last_resort_kyber_exists"]
        for name, (sql, params) in zip(names, statements):
            plan = await explain(session, sql, params)
            evidence.record(family="prekey_quota", query=name, sql=sql, row_count=None, plan=plan)
            # These four query families are already index-backed by migrations
            # 0001/0002/0035 -- an established fact from this card's grounding pass,
            # so (unlike the ILIKE/blockers_of/poll families) a Seq Scan here is a
            # real regression, not an open question.
            assert not has_seq_scan(plan), f"{name} plan unexpectedly contains a Seq Scan: {plan}"
        await session.rollback()


@pytest.mark.asyncio(loop_scope="module")
async def test_prekey_bundle_plan(explain_dataset: Handles, evidence: Evidence) -> None:
    ds = explain_dataset
    async with ds.factory() as session:
        requester = await session.get(models.User, ds.prekey_bundle_requester_id)
        assert requester is not None

        bundle_out, statements = await run_captured(
            ds.engine,
            fetch_prekey_bundle(user_id=ds.prekey_bundle_target_id, user=requester, session=session),
        )
        result = bundle_out.devices

        # Constructed: the target has exactly 2 active devices, each with a nonzero
        # available one-time-prekey pool, PLUS a third, revoked device on the same
        # target with its own nonzero pool -- the bundle must cover only the 2 active
        # devices and hand back a real (non-null) one-time prekey for each, not degrade
        # silently, and the revoked device's `Device.revoked_at.is_(None)` filter must
        # actually exclude it (there IS something present for it to exclude).
        result_device_ids = {b.device_id for b in result}
        assert result_device_ids == set(ds.prekey_bundle_device_ids)
        assert ds.prekey_bundle_revoked_device_id not in result_device_ids, (
            "revoked device must never receive a prekey bundle"
        )
        assert len(result) == 2
        assert all(b.one_time_prekey is not None for b in result)

        # statements[0] = fetch_prekey_bundle's own target-user existence check
        # (session.get(models.User, user_id)); [1] = devices select; then per device:
        # signed_prekey select, OTK consume UPDATE...RETURNING, Kyber consume
        # UPDATE...RETURNING (index 2..7). Still 2 devices' worth of per-device
        # statements even with 3 devices seeded on the target, because the revoked
        # one must never reach the per-device loop at all.
        assert len(statements) == 2 + 2 * 3
        sql, params = statements[1]
        plan = await explain(session, sql, params)
        devices_actual_rows = plan["Plan"]["Actual Rows"]
        assert devices_actual_rows == 2, (
            "devices_select must return exactly the 2 non-revoked devices, not the "
            f"revoked third -- got {devices_actual_rows} actual rows"
        )
        evidence.record(
            family="prekey_bundle", query="devices_select", sql=sql, row_count=devices_actual_rows, plan=plan
        )
        assert not has_seq_scan(plan), f"devices_select plan unexpectedly contains a Seq Scan: {plan}"

        labels = ["signed_prekey_select", "otk_consume", "kyber_consume"]
        for device_index in range(2):
            for offset, label in enumerate(labels):
                idx = 2 + device_index * 3 + offset
                s_sql, s_params = statements[idx]
                s_plan = await explain(session, s_sql, s_params)
                evidence.record(
                    family="prekey_bundle",
                    query=f"{label}_device{device_index}",
                    sql=s_sql,
                    row_count=1,
                    plan=s_plan,
                )
                assert not has_seq_scan(s_plan), f"{label} (device {device_index}) plan unexpectedly contains a Seq Scan: {s_plan}"
        await session.rollback()
