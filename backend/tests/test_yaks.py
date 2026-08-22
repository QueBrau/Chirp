"""Yak anonymity + basic vote/delete coverage (SPEC §8.3; security review findings 7 & 14)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from httpx import AsyncClient
from sqlalchemy import text

from tests.conftest import ApiUser, MakeCampus, set_campus


async def _make_campus_user(
    client: AsyncClient, campus_id: str, display_name: str = "Yak User"
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


async def test_create_yak_response_has_no_author_field(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """POST /campuses/{id}/yaks response JSON must never expose author identity (§8.3)."""
    campus_id = await make_campus()
    user = await _make_campus_user(client, campus_id)

    response = await client.post(
        f"/campuses/{campus_id}/yaks", json={"body": "first post"}, headers=user.headers
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert "author" not in body
    assert "author_id" not in body
    assert set(body.keys()) == {"id", "campus_id", "body", "score", "created_at"}


async def test_list_yaks_response_has_no_author_field(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """GET listing (with my_vote) also carries no author/author_id key anywhere."""
    campus_id = await make_campus()
    user = await _make_campus_user(client, campus_id)
    posted = await client.post(
        f"/campuses/{campus_id}/yaks", json={"body": "anon yak"}, headers=user.headers
    )
    assert posted.status_code == 201, posted.text

    response = await client.get(f"/campuses/{campus_id}/yaks", headers=user.headers)
    assert response.status_code == 200, response.text
    items = response.json()
    assert items, "expected at least the yak just posted"
    for item in items:
        assert "author" not in item
        assert "author_id" not in item


async def test_vote_yak_upserts_and_recomputes_score(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """PUT vote is an upsert keyed on (yak, caller); score reflects the sum of votes."""
    campus_id = await make_campus()
    author = await _make_campus_user(client, campus_id, "Author")
    voter = await _make_campus_user(client, campus_id, "Voter")

    created = await client.post(
        f"/campuses/{campus_id}/yaks", json={"body": "vote me"}, headers=author.headers
    )
    assert created.status_code == 201, created.text
    yak_id = created.json()["id"]

    up = await client.put(f"/yaks/{yak_id}/vote", json={"value": 1}, headers=voter.headers)
    assert up.status_code == 200, up.text
    assert up.json() == {"yak_id": yak_id, "value": 1}

    listing = await client.get(f"/campuses/{campus_id}/yaks", headers=author.headers)
    scores = {item["id"]: item["score"] for item in listing.json()}
    assert scores[yak_id] == 1

    # Flip the same voter's vote — must update the existing row, not add a second one.
    down = await client.put(f"/yaks/{yak_id}/vote", json={"value": -1}, headers=voter.headers)
    assert down.status_code == 200, down.text

    listing2 = await client.get(f"/campuses/{campus_id}/yaks", headers=author.headers)
    scores2 = {item["id"]: item["score"] for item in listing2.json()}
    assert scores2[yak_id] == -1


async def test_vote_yak_repeat_same_value_is_idempotent(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """Voting the same value twice in a row (double-tap) stays 200, never a 500 (finding 7)."""
    campus_id = await make_campus()
    author = await _make_campus_user(client, campus_id, "Author")
    voter = await _make_campus_user(client, campus_id, "Voter")

    created = await client.post(
        f"/campuses/{campus_id}/yaks", json={"body": "double tap"}, headers=author.headers
    )
    yak_id = created.json()["id"]

    first = await client.put(f"/yaks/{yak_id}/vote", json={"value": 1}, headers=voter.headers)
    second = await client.put(f"/yaks/{yak_id}/vote", json={"value": 1}, headers=voter.headers)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    listing = await client.get(f"/campuses/{campus_id}/yaks", headers=author.headers)
    scores = {item["id"]: item["score"] for item in listing.json()}
    assert scores[yak_id] == 1, "repeated identical vote must not double-count the score"


async def test_delete_yak_author_only(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """Only the author can delete their own yak; others get 403 not_author."""
    campus_id = await make_campus()
    author = await _make_campus_user(client, campus_id, "Author")
    other = await _make_campus_user(client, campus_id, "Other")

    created = await client.post(
        f"/campuses/{campus_id}/yaks", json={"body": "mine"}, headers=author.headers
    )
    yak_id = created.json()["id"]

    denied = await client.delete(f"/yaks/{yak_id}", headers=other.headers)
    assert denied.status_code == 403, denied.text
    assert denied.json() == {"detail": "not_author"}

    ok = await client.delete(f"/yaks/{yak_id}", headers=author.headers)
    assert ok.status_code == 204, ok.text

    listing = await client.get(f"/campuses/{campus_id}/yaks", headers=author.headers)
    assert all(item["id"] != yak_id for item in listing.json())


async def test_tied_timestamp_page_boundary_is_lossless_with_before_id(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """5 same-timestamp yaks: paging with before+before_id returns all 5, no dupes/gaps.

    Board card c127 - same shape as test_pagination.py's message version
    (security review finding 10): a created_at-only cursor silently drops rows
    that share a timestamp at a page boundary.
    """
    campus_id = await make_campus()
    author = await _make_campus_user(client, campus_id)

    tied_at = datetime.now(timezone.utc)
    yak_ids: list[str] = []
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        for i in range(5):
            result = await session.execute(
                text(
                    "INSERT INTO yaks (campus_id, author_id, body, created_at)"
                    " VALUES (:campus_id, :author_id, :body, :created_at)"
                    " RETURNING id"
                ),
                {
                    "campus_id": uuid.UUID(campus_id),
                    "author_id": uuid.UUID(author.id),
                    "body": f"tied-{i}",
                    "created_at": tied_at,
                },
            )
            yak_ids.append(str(result.scalar_one()))
        await session.commit()

    seen: list[str] = []
    before: str | None = None
    before_id: str | None = None
    for _ in range(10):
        params: dict[str, str | int] = {"limit": 2}
        if before is not None:
            params["before"] = before
            params["before_id"] = before_id
        response = await client.get(
            f"/campuses/{campus_id}/yaks", params=params, headers=author.headers
        )
        assert response.status_code == 200, response.text
        page = response.json()
        if not page:
            break
        seen.extend(item["id"] for item in page)
        before = page[-1]["created_at"]
        before_id = page[-1]["id"]

    assert sorted(seen) == sorted(yak_ids), "lost or duplicated a row at a tied-timestamp page boundary"


async def test_before_alone_still_works_backward_compatible(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """`before` without `before_id` (legacy clients) still paginates without erroring."""
    campus_id = await make_campus()
    author = await _make_campus_user(client, campus_id)

    for i in range(3):
        posted = await client.post(
            f"/campuses/{campus_id}/yaks", json={"body": f"yak-{i}"}, headers=author.headers
        )
        assert posted.status_code == 201, posted.text

    first_page = await client.get(
        f"/campuses/{campus_id}/yaks", params={"limit": 2}, headers=author.headers
    )
    assert first_page.status_code == 200, first_page.text
    items = first_page.json()
    assert len(items) == 2

    second_page = await client.get(
        f"/campuses/{campus_id}/yaks",
        params={"before": items[-1]["created_at"]},
        headers=author.headers,
    )
    assert second_page.status_code == 200, second_page.text
    assert len(second_page.json()) == 1


async def test_list_yaks_limit_defaults_and_caps(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """No limit param -> default page size applies; a limit above the cap is rejected.

    Board card c127: this endpoint had no bound at all before - the request that
    used to return the whole table now returns at most `limit`, and the cap
    itself cannot be raised by a client that just asks for more.
    """
    campus_id = await make_campus()
    author = await _make_campus_user(client, campus_id)

    for i in range(3):
        posted = await client.post(
            f"/campuses/{campus_id}/yaks", json={"body": f"yak-{i}"}, headers=author.headers
        )
        assert posted.status_code == 201, posted.text

    default_page = await client.get(f"/campuses/{campus_id}/yaks", headers=author.headers)
    assert default_page.status_code == 200, default_page.text
    assert len(default_page.json()) == 3  # well under the default cap, so nothing is trimmed

    too_large = await client.get(
        f"/campuses/{campus_id}/yaks", params={"limit": 201}, headers=author.headers
    )
    assert too_large.status_code == 422, too_large.text

    zero = await client.get(
        f"/campuses/{campus_id}/yaks", params={"limit": 0}, headers=author.headers
    )
    assert zero.status_code == 422, zero.text
