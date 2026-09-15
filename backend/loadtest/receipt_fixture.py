"""Create one disposable, local-only c363 messaging fixture.

Run after migrations in an EMPTY database named chirp_receipts_<8-32 hex chars>.
This creates synthetic directory rows, not valid cryptographic sessions. It
never provisions Firebase accounts, push tokens, or a remote database.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
import re
from urllib.parse import urlsplit
import uuid

from sqlalchemy.engine import URL, make_url


class FixtureError(ValueError):
    """Diagnostics deliberately exclude connection strings and fixture identities."""


def validate_database_url(url: str) -> URL:
    try:
        parsed = urlsplit(url)
        actual = make_url(url)
        valid = (
            parsed.scheme == "postgresql+asyncpg"
            and parsed.hostname in {"127.0.0.1", "::1"}
            and parsed.port is not None and 1024 <= parsed.port <= 65535
            and re.fullmatch(r"/chirp_receipts_[a-f0-9]{8,32}", parsed.path)
            and not parsed.query and not parsed.fragment
            and not any(char in url for char in "\r\n\t")
            and actual.drivername == parsed.scheme
            and actual.host == parsed.hostname
            and actual.port == parsed.port
            and actual.database == parsed.path[1:]
            and not actual.query
        )
    except (ValueError, TypeError):
        valid = False
    except Exception:
        # SQLAlchemy rejects malformed authorities with its own exception type.
        valid = False
    if not valid:
        raise FixtureError("requires_explicit_disposable_loopback_database")
    return actual


def validate_recipient_count(value: int) -> None:
    if type(value) is not int or not 1 <= value <= 10:
        raise FixtureError("recipient_count_must_be_1_to_10")


async def create_fixture(session, recipient_count: int) -> dict:
    """Stage rows in the caller's transaction; do not update existing identities."""
    from sqlalchemy import select
    from app import models

    validate_recipient_count(recipient_count)
    for model in (models.User, models.Campus, models.Chapter,
                  models.Conversation, models.Device):
        if (await session.execute(select(model.id).limit(1))).first() is not None:
            raise FixtureError("database_contains_application_rows")

    run_id = uuid.uuid4().hex
    campus_id, chapter_id, conversation_id, device_id = (uuid.uuid4() for _ in range(4))
    campus = models.Campus(id=campus_id, name="Local receipt fixture", slug="receipt-" + run_id)
    session.add(campus)
    await session.flush()
    chapter = models.Chapter(id=chapter_id, campus_id=campus_id,
                             org_name="Local receipt fixture", chapter_name="Disposable")
    session.add(chapter)
    await session.flush()

    now = datetime.now(timezone.utc)
    users = []
    for index in range(recipient_count + 1):
        suffix = "sender" if index == 0 else f"recipient-{index}"
        uid = f"receipt-{run_id}-{suffix}"
        user = models.User(id=uuid.uuid4(), firebase_uid=uid, email=uid + "@loadtest.invalid",
                           display_name="Local receipt " + suffix, account_type="greek",
                           campus_id=campus_id, campus_verified_at=now)
        users.append(user)
        session.add(user)
    await session.flush()
    for user in users:
        session.add(models.Membership(user_id=user.id, chapter_id=chapter_id,
                                      role="member", status="active"))
    session.add(models.Device(id=device_id, user_id=users[0].id,
                              device_label="c363 synthetic directory entry",
                              registration_id=1, identity_key=os.urandom(32)))
    session.add(models.Conversation(id=conversation_id, chapter_id=chapter_id,
                                    kind="group", title="Local receipt fixture"))
    await session.flush()
    for user in users:
        session.add(models.ConversationMember(conversation_id=conversation_id, user_id=user.id))
    await session.flush()
    return {
        "schema_version": 1,
        "campus_id": str(campus_id), "chapter_id": str(chapter_id),
        "users": [{"uid": user.firebase_uid, "user_id": str(user.id)} for user in users],
        "message_workload": {
            "sender_uid": users[0].firebase_uid, "sender_device_id": str(device_id),
            "conversation_id": str(conversation_id),
            "recipient_uids": [user.firebase_uid for user in users[1:]],
        },
    }


async def seed_database(url: str, recipient_count: int) -> dict:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    validated_url = validate_database_url(url)
    validate_recipient_count(recipient_count)
    # Construct this explicitly from the validated URL, never a cached app setting.
    engine = create_async_engine(validated_url, pool_size=1, max_overflow=0,
                                 connect_args={"timeout": 5, "command_timeout": 15})
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            async with session.begin():
                return await create_fixture(session, recipient_count)
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--recipients", type=int, default=2)
    parser.add_argument("--out", required=True, help="New private manifest file; never overwritten")
    args = parser.parse_args()
    try:
        validate_recipient_count(args.recipients)
        url = os.environ.get("DATABASE_URL", "")
        validate_database_url(url)
        if os.environ.get("ENV") != "local" or os.environ.get("AUTH_MODE") != "emulated":
            raise FixtureError("requires_explicit_local_emulated_environment")
        # Reserve the new output before the database transaction, so an existing
        # file or symlink cannot cause writes followed by an output refusal.
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            manifest = asyncio.run(seed_database(url, args.recipients))
            json.dump(manifest, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FixtureError as error:
        print(json.dumps({"status": "refused", "reason": str(error)}))
        return 2
    except Exception:
        # A commit or file-write response may have been lost. Keep the empty or
        # partial private file; inspect the disposable DB before a manual rerun.
        print(json.dumps({"status": "not_proven", "reason": "fixture_or_output_failed"}))
        return 2
    print(json.dumps({"status": "local_fixture_created", "recipients": args.recipients,
                      "cryptographic_session_proven": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
