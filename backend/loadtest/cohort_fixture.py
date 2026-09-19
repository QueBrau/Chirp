"""Plan or create synthetic local cohorts in an EMPTY disposable database."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import uuid

from loadtest.cohort_contract import parse_manifest
from loadtest.message_receipts import PrivateArgumentParser
from loadtest.receipt_contract import ReceiptError
from loadtest.receipt_fixture import FixtureError, validate_database_url


def profiles(cohorts: int, recipients: int) -> list[tuple[int, int]]:
    if (type(cohorts) is not int or not 2 <= cohorts <= 4
            or type(recipients) is not int or not 1 <= recipients <= 4
            or cohorts * recipients > 10 or cohorts * (recipients + 1) > 20):
        raise FixtureError("cohort_fixture_budget_invalid")
    return [(100, 51) if index % 2 == 0 else (300, 201) for index in range(cohorts)]


async def create_fixture(session, cohort_count: int = 2, recipients: int = 2) -> dict:
    from sqlalchemy import select
    from app import models
    from app.db import Base

    layout = profiles(cohort_count, recipients)
    # Some application tables (for example webhook dedup records) have no FK
    # to a user/chapter. Root-table emptiness alone is not an empty database.
    for table in Base.metadata.sorted_tables:
        if (await session.execute(select(1).select_from(table).limit(1))).first() is not None:
            raise FixtureError("database_contains_application_rows")
    now = datetime.now(timezone.utc)
    seeded_at, history_before = now - timedelta(days=1), now - timedelta(hours=12)
    result = {"schema_version": 1, "cohorts": []}
    for member_count, page_rows in layout:
        run = uuid.uuid4().hex
        campus_id, chapter_id, device_id = (uuid.uuid4() for _ in range(3))
        session.add(models.Campus(id=campus_id, name="Local cohort fixture", slug="cohort-" + run))
        await session.flush()
        session.add(models.Chapter(id=chapter_id, campus_id=campus_id,
                                   org_name="Local cohort fixture", chapter_name="Disposable"))
        await session.flush()
        users = []
        for index in range(member_count):
            uid = f"receipt-{run}-{index}"
            user = models.User(id=uuid.uuid4(), firebase_uid=uid, email=uid + "@loadtest.invalid",
                               display_name="Synthetic cohort member", account_type="greek",
                               campus_id=campus_id, campus_verified_at=now)
            session.add(user)
            users.append(user)
        await session.flush()
        for user in users:
            session.add(models.Membership(user_id=user.id, chapter_id=chapter_id,
                                          role="member", status="active"))
        active = users[:recipients + 1]
        session.add(models.Device(id=device_id, user_id=active[0].id,
                                  device_label="c363 synthetic cohort sender",
                                  registration_id=1, identity_key=os.urandom(32)))
        conversations, posts, chapter_posts, messages = ([uuid.uuid4() for _ in range(page_rows)] for _ in range(4))
        for identifier in conversations:
            session.add(models.Conversation(id=identifier, chapter_id=chapter_id, kind="group",
                                            title="Synthetic cohort inbox", created_at=seeded_at))
        for identifier in posts:
            session.add(models.Post(id=identifier, chapter_id=chapter_id, campus_id=campus_id,
                                    author_id=active[0].id, body="Local synthetic cohort post",
                                    audience="campus", post_type="text", created_at=seeded_at))
        for identifier in chapter_posts:
            session.add(models.Post(id=identifier, chapter_id=chapter_id, campus_id=campus_id,
                                    author_id=active[0].id, body="Local synthetic chapter post",
                                    audience="org", post_type="text", created_at=seeded_at))
        await session.flush()
        for conversation_id in conversations:
            for user in active:
                session.add(models.ConversationMember(conversation_id=conversation_id, user_id=user.id))
        for identifier in messages:
            session.add(models.Message(id=identifier, conversation_id=conversations[0],
                                       sender_device_id=device_id, ciphertext=os.urandom(48),
                                       message_type="signal", created_at=seeded_at))
        await session.flush()
        result["cohorts"].append({
            "receipt": {"schema_version": 1, "campus_id": str(campus_id), "chapter_id": str(chapter_id),
                        "users": [{"uid": user.firebase_uid, "user_id": str(user.id)} for user in active],
                        "message_workload": {
                            "sender_uid": active[0].firebase_uid, "sender_device_id": str(device_id),
                            "conversation_id": str(conversations[0]),
                            "recipient_uids": [user.firebase_uid for user in active[1:]],
                        }},
            "dataset": {"member_ids": [str(user.id) for user in users],
                        "post_ids": list(map(str, posts)), "chapter_post_ids": list(map(str, chapter_posts)),
                        "conversation_ids": list(map(str, conversations)),
                        "message_ids": list(map(str, messages)), "history_before": history_before.isoformat()},
        })
    parse_manifest(json.dumps(result))
    return result


async def seed_database(url: str, cohort_count: int, recipients: int) -> dict:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    actual = validate_database_url(url)
    profiles(cohort_count, recipients)
    engine = create_async_engine(actual, pool_size=1, max_overflow=0,
                                 connect_args={"timeout": 5, "command_timeout": 15})
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            async with session.begin():
                return await create_fixture(session, cohort_count, recipients)
    finally:
        await engine.dispose()


def main() -> int:
    parser = PrivateArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--cohorts", type=int, default=2)
    parser.add_argument("--recipients", type=int, default=2)
    parser.add_argument("--apply", action="store_true", help="Create the planned local fixture")
    parser.add_argument("--out", help="New private manifest file, required with --apply")
    try:
        args = parser.parse_args()
        layout = profiles(args.cohorts, args.recipients)
        if not args.apply:
            print(json.dumps({"status": "PLAN_ONLY", "cohorts": args.cohorts,
                              "active_users": args.cohorts * (args.recipients + 1),
                              "selected_recipients": args.cohorts * args.recipients,
                              "chapter_members": [members for members, _ in layout],
                              "rows_per_paginated_dataset": [rows for _, rows in layout]}))
            return 0
        if not args.out:
            raise FixtureError("output_required")
        if os.environ.get("ENV") != "local" or os.environ.get("AUTH_MODE") != "emulated":
            raise FixtureError("requires_explicit_local_emulated_environment")
        url = os.environ.get("DATABASE_URL", "")
        validate_database_url(url)
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            raw = asyncio.run(seed_database(url, args.cohorts, args.recipients))
            json.dump(raw, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except (ReceiptError, FixtureError) as error:
        print(json.dumps({"status": "refused", "reason": str(error)}))
        return 2
    except (Exception, KeyboardInterrupt):
        print(json.dumps({"status": "NOT_PROVEN", "reason": "fixture_or_output_failed"}))
        return 2
    print(json.dumps({"status": "local_cohort_fixture_created", "cohorts": args.cohorts}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
