"""Block-by-chirp (SPEC §8.3): blocking a chirp's author must never reveal who that author is."""
from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy import text

from tests.conftest import ApiUser, MakeCampus, set_campus


async def _make_campus_user(
    client: AsyncClient, campus_id: str, display_name: str = "Block Test User"
) -> ApiUser:
    """Bootstrap a user pinned to `campus_id` (make_user doesn't expose campus_id)."""
    uid = f"uid-{uuid.uuid4().hex}"
    headers = {"X-Debug-Firebase-Uid": uid}
    email = f"{uid}@example.edu"
    response = await client.post(
        "/auth/bootstrap",
        json={
            "email": email,
            "display_name": display_name,
            "account_type": "non_greek",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    user = ApiUser(id=response.json()["id"], firebase_uid=uid, email=email, headers=headers)
    # c85: campus is server-owned, so it is set directly rather than claimed in the
    # bootstrap body. Same pattern as _grant_platform_admin — no API grants it until
    # the .edu redemption in c86 exists.
    await set_campus(user.id, campus_id)
    return user


async def test_block_by_chirp_returns_204_with_empty_body(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """POST /moderation/blocks/by-chirp/{chirp_id} succeeds with no response body — a
    UserBlockOut here would echo blocked_id and expose the chirp's author (§8.3)."""
    campus_id = await make_campus()
    author = await _make_campus_user(client, campus_id, "Author")
    blocker = await _make_campus_user(client, campus_id, "Blocker")

    chirp = await client.post(
        f"/campuses/{campus_id}/chirps", json={"body": "block me"}, headers=author.headers
    )
    assert chirp.status_code == 201, chirp.text
    chirp_id = chirp.json()["id"]

    response = await client.post(f"/moderation/blocks/by-chirp/{chirp_id}", headers=blocker.headers)

    assert response.status_code == 204, response.text
    assert response.content == b""


async def test_block_by_chirp_hides_authors_chirps_from_blocker_only(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """Once blocked, the author's chirps vanish from the blocker's own feed — but stay
    fully visible to everyone else (including the author), with no tombstone or count
    anywhere indicating a hide happened."""
    campus_id = await make_campus()
    author = await _make_campus_user(client, campus_id, "Author")
    blocker = await _make_campus_user(client, campus_id, "Blocker")
    bystander = await _make_campus_user(client, campus_id, "Bystander")

    chirp = await client.post(
        f"/campuses/{campus_id}/chirps", json={"body": "hide me"}, headers=author.headers
    )
    assert chirp.status_code == 201, chirp.text
    chirp_id = chirp.json()["id"]

    block = await client.post(f"/moderation/blocks/by-chirp/{chirp_id}", headers=blocker.headers)
    assert block.status_code == 204, block.text

    blocker_listing = await client.get(f"/campuses/{campus_id}/chirps", headers=blocker.headers)
    assert blocker_listing.status_code == 200, blocker_listing.text
    assert all(item["id"] != chirp_id for item in blocker_listing.json())

    bystander_listing = await client.get(
        f"/campuses/{campus_id}/chirps", headers=bystander.headers
    )
    assert bystander_listing.status_code == 200, bystander_listing.text
    assert any(item["id"] == chirp_id for item in bystander_listing.json())

    author_listing = await client.get(f"/campuses/{campus_id}/chirps", headers=author.headers)
    assert any(item["id"] == chirp_id for item in author_listing.json())


async def test_block_by_chirp_hides_all_of_that_authors_chirps(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """Blocking via ONE of an author's chirps must hide ALL of that author's chirps from the
    blocker — the filter is keyed on author_id, not the particular chirp_id used to block.
    A filter accidentally keyed to chirp_id would pass every other test but fail this one."""
    campus_id = await make_campus()
    author = await _make_campus_user(client, campus_id, "Author")
    blocker = await _make_campus_user(client, campus_id, "Blocker")
    bystander = await _make_campus_user(client, campus_id, "Bystander")

    chirp_ids = []
    for body in ("first chirp", "second chirp", "third chirp"):
        chirp = await client.post(
            f"/campuses/{campus_id}/chirps", json={"body": body}, headers=author.headers
        )
        assert chirp.status_code == 201, chirp.text
        chirp_ids.append(chirp.json()["id"])

    block = await client.post(
        f"/moderation/blocks/by-chirp/{chirp_ids[0]}", headers=blocker.headers
    )
    assert block.status_code == 204, block.text

    blocker_listing = await client.get(f"/campuses/{campus_id}/chirps", headers=blocker.headers)
    assert blocker_listing.status_code == 200, blocker_listing.text
    blocker_ids = {item["id"] for item in blocker_listing.json()}
    for chirp_id in chirp_ids:
        assert chirp_id not in blocker_ids

    bystander_listing = await client.get(
        f"/campuses/{campus_id}/chirps", headers=bystander.headers
    )
    assert bystander_listing.status_code == 200, bystander_listing.text
    bystander_ids = {item["id"] for item in bystander_listing.json()}
    for chirp_id in chirp_ids:
        assert chirp_id in bystander_ids


async def test_block_does_not_hide_other_authors_chirps(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """Blocking author A must not hide author B's chirps — an anti-join missing the
    blocker_id == caller condition would hide everyone's chirps from everyone, which no
    other test would notice."""
    campus_id = await make_campus()
    author_a = await _make_campus_user(client, campus_id, "Author A")
    author_b = await _make_campus_user(client, campus_id, "Author B")
    blocker = await _make_campus_user(client, campus_id, "Blocker")

    chirp_a = await client.post(
        f"/campuses/{campus_id}/chirps", json={"body": "from author a"}, headers=author_a.headers
    )
    assert chirp_a.status_code == 201, chirp_a.text
    chirp_a_id = chirp_a.json()["id"]

    chirp_b = await client.post(
        f"/campuses/{campus_id}/chirps", json={"body": "from author b"}, headers=author_b.headers
    )
    assert chirp_b.status_code == 201, chirp_b.text
    chirp_b_id = chirp_b.json()["id"]

    block = await client.post(f"/moderation/blocks/by-chirp/{chirp_a_id}", headers=blocker.headers)
    assert block.status_code == 204, block.text

    blocker_listing = await client.get(f"/campuses/{campus_id}/chirps", headers=blocker.headers)
    assert blocker_listing.status_code == 200, blocker_listing.text
    blocker_ids = {item["id"] for item in blocker_listing.json()}
    assert chirp_a_id not in blocker_ids
    assert chirp_b_id in blocker_ids


async def test_block_by_chirp_twice_is_204_both_times(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """Blocking the same chirp's author twice stays 204 -> 204, never 409: a 409-vs-204
    split would be a one-bit oracle for "have I already blocked this author" that a
    caller could use to test whether two different chirps share an author."""
    campus_id = await make_campus()
    author = await _make_campus_user(client, campus_id, "Author")
    blocker = await _make_campus_user(client, campus_id, "Blocker")

    chirp = await client.post(
        f"/campuses/{campus_id}/chirps", json={"body": "double block"}, headers=author.headers
    )
    assert chirp.status_code == 201, chirp.text
    chirp_id = chirp.json()["id"]

    first = await client.post(f"/moderation/blocks/by-chirp/{chirp_id}", headers=blocker.headers)
    second = await client.post(f"/moderation/blocks/by-chirp/{chirp_id}", headers=blocker.headers)

    assert first.status_code == 204, first.text
    assert second.status_code == 204, second.text
    assert first.content == b""
    assert second.content == b""


async def test_block_by_chirp_cross_campus_is_403(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """A caller cannot block-by-chirp a chirp posted on a campus other than their own."""
    campus_a = await make_campus()
    campus_b = await make_campus()
    author = await _make_campus_user(client, campus_a, "Author")
    outsider = await _make_campus_user(client, campus_b, "Outsider")

    chirp = await client.post(
        f"/campuses/{campus_a}/chirps", json={"body": "campus a only"}, headers=author.headers
    )
    assert chirp.status_code == 201, chirp.text
    chirp_id = chirp.json()["id"]

    response = await client.post(f"/moderation/blocks/by-chirp/{chirp_id}", headers=outsider.headers)

    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "not_your_campus"}


async def test_block_by_chirp_self_is_403(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """An author cannot block themselves via their own chirp."""
    campus_id = await make_campus()
    author = await _make_campus_user(client, campus_id, "Author")

    chirp = await client.post(
        f"/campuses/{campus_id}/chirps", json={"body": "myself"}, headers=author.headers
    )
    assert chirp.status_code == 201, chirp.text
    chirp_id = chirp.json()["id"]

    response = await client.post(f"/moderation/blocks/by-chirp/{chirp_id}", headers=author.headers)

    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "cannot_block_self"}


async def test_block_by_chirp_missing_or_removed_chirp_is_404(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """A nonexistent chirp id, and a soft-removed one, both 404 rather than leaking state."""
    campus_id = await make_campus()
    author = await _make_campus_user(client, campus_id, "Author")
    blocker = await _make_campus_user(client, campus_id, "Blocker")

    missing = await client.post(
        f"/moderation/blocks/by-chirp/{uuid.uuid4()}", headers=blocker.headers
    )
    assert missing.status_code == 404, missing.text
    assert missing.json() == {"detail": "chirp_not_found"}

    chirp = await client.post(
        f"/campuses/{campus_id}/chirps", json={"body": "will be removed"}, headers=author.headers
    )
    chirp_id = chirp.json()["id"]
    deleted = await client.delete(f"/chirps/{chirp_id}", headers=author.headers)
    assert deleted.status_code == 204, deleted.text

    removed = await client.post(f"/moderation/blocks/by-chirp/{chirp_id}", headers=blocker.headers)
    assert removed.status_code == 404, removed.text
    assert removed.json() == {"detail": "chirp_not_found"}


async def test_chirp_endpoints_never_expose_author_id(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """Every chirp-related response body — create, list, vote, and block-by-chirp — must
    never contain an author/author_id key anywhere (§8.3 / invariant 3)."""
    campus_id = await make_campus()
    author = await _make_campus_user(client, campus_id, "Author")
    voter = await _make_campus_user(client, campus_id, "Voter")

    created = await client.post(
        f"/campuses/{campus_id}/chirps", json={"body": "no author here"}, headers=author.headers
    )
    assert created.status_code == 201, created.text
    created_body = created.json()
    assert "author_id" not in created_body
    assert "author" not in created_body
    chirp_id = created_body["id"]

    listing = await client.get(f"/campuses/{campus_id}/chirps", headers=voter.headers)
    assert listing.status_code == 200, listing.text
    assert listing.json(), "expected at least the chirp just posted"
    for item in listing.json():
        assert "author_id" not in item
        assert "author" not in item

    voted = await client.put(f"/chirps/{chirp_id}/vote", json={"value": 1}, headers=voter.headers)
    assert voted.status_code == 200, voted.text
    assert "author_id" not in voted.json()
    assert "author" not in voted.json()

    blocked = await client.post(f"/moderation/blocks/by-chirp/{chirp_id}", headers=voter.headers)
    assert blocked.status_code == 204, blocked.text
    assert blocked.content == b""


async def test_block_by_chirp_is_idempotent_and_does_the_same_work_each_time(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """Blocking the same author twice must be indistinguishable from blocking once.

    Both calls return 204 and exactly one UserBlock row exists. The endpoint is an
    unconditional upsert rather than read-then-maybe-insert so the two outcomes are
    also latency-equivalent — an attacker timing the response must not be able to
    tell whether a block already existed, which would reveal that two chirps share an
    author (SPEC 8.3).
    """
    campus_id = await make_campus()
    author = await _make_campus_user(client, campus_id, "Chirp Author")
    blocker = await _make_campus_user(client, campus_id, "Chirp Blocker")

    created = await client.post(
        f"/campuses/{campus_id}/chirps", json={"body": "same author, twice"}, headers=author.headers
    )
    assert created.status_code == 201, created.text
    chirp_id = created.json()["id"]

    first = await client.post(
        f"/moderation/blocks/by-chirp/{chirp_id}", headers=blocker.headers
    )
    second = await client.post(
        f"/moderation/blocks/by-chirp/{chirp_id}", headers=blocker.headers
    )

    assert first.status_code == 204, first.text
    assert second.status_code == 204, second.text

    from app.db import get_session_factory

    async with get_session_factory()() as session:
        count = await session.execute(
            text("SELECT count(*) FROM user_blocks WHERE blocker_id = :b"),
            {"b": blocker.id},
        )
        assert int(count.scalar_one()) == 1
