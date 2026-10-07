"""Read-only c437 removal inventory evidence against the disposable PostgreSQL DB."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.config import get_settings
from app.db import get_session_factory
from app.services.removal_inventory import inventory_media_reference
from tests.conftest import MakeChapterWith

BUCKET = "c437-inventory-media"


@pytest.fixture(autouse=True)
def _inventory_bucket(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(get_settings(), "media_bucket_name", BUCKET)


async def _insert_post(chapter_id: str, author_id: str, media_urls: list[str]) -> str:
    async with get_session_factory()() as session:
        result = await session.execute(
            text(
                "INSERT INTO posts (id, chapter_id, campus_id, author_id, body, media_urls, "
                "audience, post_type, created_at) "
                "SELECT gen_random_uuid(), :chapter, c.campus_id, :author, 'inventory fixture', "
                ":media, 'org', 'photo', now() FROM chapters c WHERE c.id = :chapter "
                "RETURNING id"
            ),
            {"chapter": chapter_id, "author": author_id, "media": media_urls},
        )
        post_id = str(result.scalar_one())
        await session.commit()
    return post_id


async def test_inventory_covers_shared_post_references_removed_rows_and_avatar(
    make_chapter_with: MakeChapterWith,
) -> None:
    setup = await make_chapter_with("president")
    object_name = f"posts/{setup.president.id}/shared.jpg"
    canonical = f"https://storage.googleapis.com/{BUCKET}/{object_name}"
    live_id = await _insert_post(setup.chapter_id, setup.president.id, [canonical])
    removed_id = await _insert_post(setup.chapter_id, setup.president.id, [canonical])

    async with get_session_factory()() as session:
        await session.execute(
            text("UPDATE posts SET deleted_at = now(), removed_reason = 'fixture' WHERE id = :id"),
            {"id": removed_id},
        )
        await session.execute(
            text("UPDATE users SET avatar_url = :url WHERE id = :id"),
            {
                "id": setup.president.id,
                "url": f"https://storage.googleapis.com/{BUCKET}/avatars/{setup.president.id}/avatar.jpg",
            },
        )
        await session.commit()

    async with get_session_factory()() as session:
        first = await inventory_media_reference(session, object_name)
    async with get_session_factory()() as session:
        second = await inventory_media_reference(session, object_name)

    assert first.complete is True
    assert first.ready_to_apply is True
    assert first.manifest_digest == second.manifest_digest
    assert {(ref.surface, ref.row_id, ref.state) for ref in first.references} == {
        ("posts.media_urls", live_id, "live"),
        ("posts.media_urls", removed_id, "removed"),
    }
    # The avatar is a different object name and must not be treated as a byte-identical
    # copy. This inventory proves shared references only; it does not inspect bytes.
    assert all(ref.surface != "users.avatar_url" for ref in first.references)
    assert first.unsupported_media_surfaces == ("content_reports.forwarded_plaintext",)

    avatar_name = f"avatars/{setup.president.id}/avatar.jpg"
    async with get_session_factory()() as session:
        avatar_inventory = await inventory_media_reference(session, avatar_name)
    assert avatar_inventory.complete is True
    assert avatar_inventory.ready_to_apply is True
    assert [(ref.surface, ref.row_id, ref.state) for ref in avatar_inventory.references] == [
        ("users.avatar_url", setup.president.id, "active")
    ]


async def test_inventory_refuses_ready_for_unknown_owned_reference_and_ignores_foreign_object(
    make_chapter_with: MakeChapterWith,
) -> None:
    setup = await make_chapter_with("president")
    object_name = f"posts/{setup.president.id}/target.jpg"
    target = f"https://storage.googleapis.com/{BUCKET}/{object_name}"
    foreign = "https://other-provider.invalid/avatars/not-ours.jpg"
    unknown_owned = f"https://storage.googleapis.com/storage/v1/b/{BUCKET}/o/not-supported"
    await _insert_post(setup.chapter_id, setup.president.id, [target, foreign, unknown_owned])

    async with get_session_factory()() as session:
        result = await inventory_media_reference(session, object_name)

    assert result.references[0].surface == "posts.media_urls"
    assert result.references[0].state == "live"
    assert result.complete is False
    assert result.ready_to_apply is False
    assert len(result.unknown_references) == 1
    assert result.unknown_references[0].surface == "posts.media_urls"
    assert result.unknown_references[0].reason == "unsupported_owned_url"
    # Foreign references are known to be outside this bucket and are not a match.
    assert result.unknown_references[0].value_digest != __import__("hashlib").sha256(foreign.encode()).hexdigest()


@pytest.mark.parametrize("bad_name", ["", "posts/not-a-uuid/a.jpg", "posts/../x.jpg", "posts/%s/a.jpg" % uuid.uuid4() + "?x=1"])
async def test_inventory_rejects_uncontrolled_object_reference(make_chapter_with: MakeChapterWith, bad_name: str) -> None:
    # The DB fixture is intentionally present: validation remains independent of any
    # provider and cannot be bypassed by an empty or path-traversal input.
    await make_chapter_with("president")
    async with get_session_factory()() as session:
        with pytest.raises(ValueError, match="object_name_invalid"):
            await inventory_media_reference(session, bad_name)
