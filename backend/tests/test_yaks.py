"""Yak anonymity + basic vote/delete coverage (SPEC §8.3; security review findings 7 & 14)."""
from __future__ import annotations

import uuid

from httpx import AsyncClient

from tests.conftest import ApiUser, MakeCampus


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
            "campus_id": campus_id,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return ApiUser(id=response.json()["id"], firebase_uid=uid, email=email, headers=headers)


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
