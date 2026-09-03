"""c279: blocking an anonymous chirp author must not name them (Jose's ruling (a)).

THE EXPLOIT, WHICH IS THE FIRST TEST IN THIS FILE BECAUSE IT IS THE CARD.
POST /moderation/blocks/by-chirp/{chirp_id} is airtight on its own terms: no response
body, no 409 split, an unconditional upsert so even the latency is constant. It never
tells the caller who the author is. But it used to write an ORDINARY user_blocks row,
and that row filtered the blocker's NAMED surfaces too - so:

    snapshot the chapter feed -> block by chirp -> re-fetch

and the named person who vanished IS the anonymous author, by display_name and
author_id both, learned from the pre-block snapshot. DELETE the block afterwards and the
feed comes back, with nothing durable marking that it happened. The endpoint was
airtight; the row it wrote was the leak.

The fix is provenance: named blocks hide everything exactly as before, by-chirp blocks
hide chirp surfaces only. So the named feed STOPS MOVING when a by-chirp block lands and
the diff has nothing to show.

WHAT MUST SURVIVE THE FIX, and each has a test here because weakening any of them would
be a worse bug than the one being closed:
  - the harasser still cannot contact the person who blocked them (that is what by-chirp
    blocking is FOR);
  - that author's chirps still disappear (the safety half);
  - a named block still hides everything (the c243/c35 suites should barely notice);
  - a named block is never silently downgraded by a later by-chirp block.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import ApiUser, MakeChapterWith, MakeUser, verify_campus

BACKEND_DIR = Path(__file__).resolve().parents[1]


async def _campus_id_of(client: AsyncClient, chapter_id: str, headers: dict) -> str:
    response = await client.get(f"/chapters/{chapter_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["campus_id"]


async def _block_source(blocker_id: str, blocked_id: str) -> str | None:
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        result = await session.execute(
            text(
                "SELECT source FROM user_blocks "
                "WHERE blocker_id = :b AND blocked_id = :t"
            ),
            {"b": blocker_id, "t": blocked_id},
        )
        row = result.first()
        return row[0] if row else None


class _Scene:
    """A blocker and an author who share a chapter AND a campus, with the author holding
    named content (a post, a comment on it) and one anonymous chirp."""

    def __init__(self, blocker: ApiUser, author: ApiUser, chapter_id: str, campus_id: str,
                 post_id: str, chirp_id: str) -> None:
        self.blocker = blocker
        self.author = author
        self.chapter_id = chapter_id
        self.campus_id = campus_id
        self.post_id = post_id
        self.chirp_id = chirp_id


async def _scene(client: AsyncClient, make_chapter_with: MakeChapterWith) -> _Scene:
    setup = await make_chapter_with("member")
    blocker, author = setup.president, setup.member
    await verify_campus(blocker.id)
    await verify_campus(author.id)
    campus_id = await _campus_id_of(client, setup.chapter_id, blocker.headers)

    post = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "a named post by the author"},
        headers=author.headers,
    )
    assert post.status_code == 201, post.text
    post_id = post.json()["id"]

    comment = await client.post(
        f"/posts/{post_id}/comments",
        json={"body": "a named comment by the author"},
        headers=author.headers,
    )
    assert comment.status_code == 201, comment.text

    chirp = await client.post(
        f"/campuses/{campus_id}/chirps",
        json={"body": "something anonymous and unflattering"},
        headers=author.headers,
    )
    assert chirp.status_code == 201, chirp.text

    return _Scene(blocker, author, setup.chapter_id, campus_id, post_id, chirp.json()["id"])


async def _named_surfaces(client: AsyncClient, scene: _Scene) -> dict[str, str]:
    """The three named surfaces the exploit read, captured as RAW TEXT.

    Raw text, not parsed json, on purpose: the claim is that a by-chirp block changes
    these responses not at all, and comparing bytes is the only version of that claim
    with no wiggle room.
    """
    posts = await client.get(
        f"/chapters/{scene.chapter_id}/posts", headers=scene.blocker.headers
    )
    comments = await client.get(
        f"/posts/{scene.post_id}/comments", headers=scene.blocker.headers
    )
    assert posts.status_code == 200, posts.text
    assert comments.status_code == 200, comments.text
    return {"posts": posts.text, "comments": comments.text}


async def _chirp_ids(client: AsyncClient, scene: _Scene) -> list[str]:
    response = await client.get(
        f"/campuses/{scene.campus_id}/chirps", headers=scene.blocker.headers
    )
    assert response.status_code == 200, response.text
    return [c["id"] for c in response.json()]


# ---------------------------------------------------------------------------
# 1. THE EXPLOIT
# ---------------------------------------------------------------------------


async def test_a_by_chirp_block_leaves_named_surfaces_byte_identical(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The deanonymisation diff, run as a test. This is the card.

    On the pre-c279 code this FAILS: the author's post and comment vanish from the
    blocker's named surfaces, which is precisely how the diff named them.
    """
    scene = await _scene(client, make_chapter_with)

    before = await _named_surfaces(client, scene)
    assert scene.author.id in before["posts"], "scene is wrong: author's post not visible"
    assert scene.chirp_id in await _chirp_ids(client, scene)

    blocked = await client.post(
        f"/moderation/blocks/by-chirp/{scene.chirp_id}", headers=scene.blocker.headers
    )
    assert blocked.status_code == 204, blocked.text

    after = await _named_surfaces(client, scene)
    assert after["posts"] == before["posts"], (
        "the author's NAMED posts moved when an anonymous chirp was blocked - "
        "diffing this response names the chirp author, which is the c279 leak"
    )
    assert after["comments"] == before["comments"], (
        "the author's NAMED comments moved - same leak, via the comment list"
    )

    # The safety half still works: that author's chirps are gone.
    assert scene.chirp_id not in await _chirp_ids(client, scene)

    # And the erase-the-evidence step restores the chirp, as unblock should.
    unblocked = await client.delete(
        f"/moderation/blocks?blocked_id={scene.author.id}", headers=scene.blocker.headers
    )
    assert unblocked.status_code == 204, unblocked.text
    assert scene.chirp_id in await _chirp_ids(client, scene)
    assert (await _named_surfaces(client, scene))["posts"] == before["posts"]


# ---------------------------------------------------------------------------
# 2. WHAT MUST SURVIVE
# ---------------------------------------------------------------------------


async def test_the_harasser_still_cannot_contact_the_person_who_blocked_them(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The reason by-chirp blocking exists at all, and the property c243 shipped.

    core/blocks.py ignores provenance deliberately: if a by-chirp block stopped
    refusing contact, someone being harassed anonymously could block the author and
    still get DMs from them. That would be a far worse bug than the one c279 closes.
    """
    scene = await _scene(client, make_chapter_with)
    blocked = await client.post(
        f"/moderation/blocks/by-chirp/{scene.chirp_id}", headers=scene.blocker.headers
    )
    assert blocked.status_code == 204, blocked.text

    # The AUTHOR (the harasser) tries to open a DM with the person who blocked them.
    attempt = await client.post(
        "/conversations",
        json={"kind": "dm", "member_user_ids": [scene.blocker.id]},
        headers=scene.author.headers,
    )
    assert attempt.status_code == 403, attempt.text
    assert attempt.json() == {"detail": "recipient_not_reachable"}


async def test_a_named_block_still_hides_everything(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The c35/c243 behaviour, unchanged. A named block is the caller saying "I never
    want to see this person again", and it must still mean that."""
    scene = await _scene(client, make_chapter_with)
    before = await _named_surfaces(client, scene)
    assert scene.author.id in before["posts"]

    blocked = await client.post(
        "/moderation/blocks",
        json={"blocked_id": scene.author.id},
        headers=scene.blocker.headers,
    )
    assert blocked.status_code == 201, blocked.text

    after = await _named_surfaces(client, scene)
    assert scene.author.id not in after["posts"], "a named block must still hide posts"
    assert "a named comment by the author" not in after["comments"]
    assert scene.chirp_id not in await _chirp_ids(client, scene)
    assert await _block_source(scene.blocker.id, scene.author.id) == "named"


# ---------------------------------------------------------------------------
# 3. PROVENANCE TRANSITIONS
# ---------------------------------------------------------------------------


async def test_a_named_block_upgrades_a_by_chirp_one_and_answers_201(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Upgrade, and the 201 is a SECURITY property rather than a convenience.

    Before provenance, this endpoint 409'd whenever any row existed - which handed back
    the identity the by-chirp endpoint refuses to give: block an anonymous author, then
    named-block roster members one at a time, and the one that 409s is the author. Same
    oracle shape c243 closed on the DM path, through a different door.

    With the upgrade, "no row" and "a by-chirp row" both answer 201 and are
    indistinguishable from outside. test_the_409_no_longer_reveals... pins that directly.
    """
    scene = await _scene(client, make_chapter_with)
    await client.post(
        f"/moderation/blocks/by-chirp/{scene.chirp_id}", headers=scene.blocker.headers
    )
    assert await _block_source(scene.blocker.id, scene.author.id) == "by_chirp"

    upgraded = await client.post(
        "/moderation/blocks",
        json={"blocked_id": scene.author.id},
        headers=scene.blocker.headers,
    )
    assert upgraded.status_code == 201, upgraded.text
    assert await _block_source(scene.blocker.id, scene.author.id) == "named"

    # And the upgrade actually takes effect on the named surfaces.
    after = await _named_surfaces(client, scene)
    assert scene.author.id not in after["posts"]


async def test_the_409_no_longer_reveals_that_a_by_chirp_block_exists(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A named block on a by-chirp-blocked author must be indistinguishable from a named
    block on someone never blocked at all. Two callers, identical observable outcome."""
    probed = await _scene(client, make_chapter_with)
    await client.post(
        f"/moderation/blocks/by-chirp/{probed.chirp_id}", headers=probed.blocker.headers
    )
    with_by_chirp_row = await client.post(
        "/moderation/blocks",
        json={"blocked_id": probed.author.id},
        headers=probed.blocker.headers,
    )

    clean = await _scene(client, make_chapter_with)
    with_no_row = await client.post(
        "/moderation/blocks",
        json={"blocked_id": clean.author.id},
        headers=clean.blocker.headers,
    )

    assert with_by_chirp_row.status_code == with_no_row.status_code == 201, (
        "a by-chirp row must not be observable through this endpoint's status - "
        f"got {with_by_chirp_row.status_code} vs {with_no_row.status_code}"
    )


async def test_a_by_chirp_block_never_downgrades_a_named_one(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Upgrade only, never the reverse. Someone who blocked a person BY NAME asked for
    everything hidden; a chirp they happen to block afterwards must not give some back."""
    scene = await _scene(client, make_chapter_with)
    await client.post(
        "/moderation/blocks",
        json={"blocked_id": scene.author.id},
        headers=scene.blocker.headers,
    )
    hidden = await _named_surfaces(client, scene)

    by_chirp = await client.post(
        f"/moderation/blocks/by-chirp/{scene.chirp_id}", headers=scene.blocker.headers
    )
    assert by_chirp.status_code == 204, by_chirp.text

    assert await _block_source(scene.blocker.id, scene.author.id) == "named"
    assert (await _named_surfaces(client, scene))["posts"] == hidden["posts"], (
        "a by-chirp block downgraded a named block and gave hidden content back"
    )


async def test_a_second_named_block_still_conflicts(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The one case that legitimately still 409s: the caller already blocked this person
    by name, which they know, so it reveals nothing they did not do themselves."""
    scene = await _scene(client, make_chapter_with)
    body = {"blocked_id": scene.author.id}
    first = await client.post("/moderation/blocks", json=body, headers=scene.blocker.headers)
    assert first.status_code == 201, first.text
    second = await client.post("/moderation/blocks", json=body, headers=scene.blocker.headers)
    assert second.status_code == 409, second.text
    assert second.json() == {"detail": "already_blocked"}


# ---------------------------------------------------------------------------
# 4. THE MIGRATION, against a table that already holds a row (c237's standard)
# ---------------------------------------------------------------------------


def _database_of(url: str) -> str:
    return url.rpartition("/")[2].partition("?")[0]


def _swap_database(url: str, database: str) -> str:
    head, _, tail = url.rpartition("/")
    _, sep, query = tail.partition("?")
    return f"{head}/{database}{sep}{query}"


async def _admin_execute(admin_url: str, statements: list[str]) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            for statement in statements:
                await conn.execute(text(statement))
    finally:
        await engine.dispose()


async def _probe(admin_url: str) -> None:
    engine = create_async_engine(admin_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


def _alembic(url: str, revision: str, *, down: bool = False) -> None:
    os.environ["DATABASE_URL"] = url
    from app.config import get_settings

    get_settings.cache_clear()
    from alembic import command
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    (command.downgrade if down else command.upgrade)(cfg, revision)


async def _seed_block_row(url: str) -> tuple[str, str]:
    """A pre-0030 block row, so the migration is proven against real data rather than an
    empty table - prod's table is empty today, but a migration that only works on empty
    tables is a migration nobody can trust the next time."""
    engine = create_async_engine(url)
    blocker, blocked = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        async with engine.begin() as conn:
            for uid, name in ((blocker, "Blocker"), (blocked, "Blocked")):
                await conn.execute(
                    text(
                        "INSERT INTO users (id, firebase_uid, email, display_name, account_type) "
                        "VALUES (:id, :uid, :email, :name, 'greek')"
                    ),
                    {"id": uid, "uid": f"uid-{uid}", "email": f"{uid}@example.edu", "name": name},
                )
            await conn.execute(
                text(
                    "INSERT INTO user_blocks (blocker_id, blocked_id) VALUES (:b, :t)"
                ),
                {"b": blocker, "t": blocked},
            )
    finally:
        await engine.dispose()
    return blocker, blocked


async def _read_source(url: str, blocker: str) -> str:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT source FROM user_blocks WHERE blocker_id = :b"), {"b": blocker}
            )
            return result.scalar_one()
    finally:
        await engine.dispose()


async def _column_exists(url: str) -> bool:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'user_blocks' AND column_name = 'source'"
                )
            )
            return result.first() is not None
    finally:
        await engine.dispose()


def test_0030_migration_up_down_up_with_a_row_present() -> None:
    """up (column added, existing row defaulted to 'named', bad values refused), down
    (column cleanly gone, row survives), up again (idempotent) — against a table that
    already holds a block, never an empty one."""
    requested = os.environ.get(
        "TEST_DATABASE_URL", "postgresql+asyncpg://chirp:chirp@localhost:5432/chirp_test"
    )
    base = _database_of(requested)
    admin_url = _swap_database(requested, "postgres")
    db_name = f"{base}_c279updownup_{uuid.uuid4().hex[:8]}"
    url = _swap_database(requested, db_name)

    try:
        asyncio.run(_probe(admin_url))
    except Exception:
        pytest.skip("postgres not available — docker compose up db")

    original = os.environ.get("DATABASE_URL")
    asyncio.run(
        _admin_execute(
            admin_url,
            [f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)', f'CREATE DATABASE "{db_name}"'],
        )
    )
    try:
        _alembic(url, "0029")
        blocker, _blocked = asyncio.run(_seed_block_row(url))
        assert not asyncio.run(_column_exists(url))

        # UP: the pre-existing row must come out 'named' — the safe direction, since it
        # preserves today's hide-everything behaviour rather than un-hiding content.
        _alembic(url, "0030")
        assert asyncio.run(_column_exists(url))
        assert asyncio.run(_read_source(url, blocker)) == "named"

        # The CHECK must actually refuse a typo'd source, which would otherwise never
        # match 'named' in the read filters and silently stop hiding named content.
        with pytest.raises(Exception):
            asyncio.run(
                _admin_execute(
                    url,
                    [
                        "UPDATE user_blocks SET source = 'by-chirp' "
                        f"WHERE blocker_id = '{blocker}'"
                    ],
                )
            )

        # DOWN: column gone, row still there.
        _alembic(url, "0029", down=True)
        assert not asyncio.run(_column_exists(url))

        # UP AGAIN: idempotent.
        _alembic(url, "0030")
        assert asyncio.run(_read_source(url, blocker)) == "named"
    finally:
        if original is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original
        from app.config import get_settings

        get_settings.cache_clear()
        asyncio.run(_admin_execute(admin_url, [f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)']))
