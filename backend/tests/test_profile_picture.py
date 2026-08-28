"""PATCH /auth/me: change your own display name and profile picture (board card c221).

THE ONE THAT MATTERS IS test_the_avatar_is_finalized_to_avatars_not_posts. Everything
else here is ordinary route coverage; that test is the whole reason the card exists.

jobs/media_reconcile.py builds its reference set from `select(Post.media_urls)` and
NOTHING else, then deletes everything under posts/ that is not in it. users.avatar_url
is not in that set. So an avatar finalized to posts/ - which is what
finalize_media_object() does by default, and what the obvious implementation would
have done - is unreferenced BY DEFINITION, ages past the job's 24h floor, and is
collected on the next --delete run. Every functional test would still pass; the photos
would just disappear a day later.

Leaving the object in tmp/ is not a hiding place either: the age-based GCS lifecycle
rule scoped to tmp/ exists precisely to reap abandoned uploads (c132).

The fake GCS harness is the one from test_media_url_validation.py, so the move is
exercised against a fake rather than skipped - which is also what lets the destination
prefix be asserted at all.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from httpx import AsyncClient

from app.services import storage_service
from tests.conftest import ApiUser

TEST_BUCKET = "test-media-bucket"


def _configure_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(storage_service, "_bucket_name", lambda: TEST_BUCKET)


class _FakeBlob:
    def __init__(self, name: str, captured: dict) -> None:
        self.name = name
        self._captured = captured

    def delete(self) -> None:
        self._captured.setdefault("deleted", []).append(self.name)


class _FakeBucket:
    def __init__(self, captured: dict) -> None:
        self._captured = captured

    def blob(self, name: str) -> _FakeBlob:
        return _FakeBlob(name, self._captured)

    def copy_blob(self, blob, _bucket, new_name, **kwargs):
        self._captured.setdefault("copy_blob_calls", []).append(
            {"source": blob.name, "new_name": new_name, "kwargs": kwargs}
        )
        return _FakeBlob(new_name, self._captured)


def _install_fake_gcs(monkeypatch: pytest.MonkeyPatch, captured: dict) -> None:
    fake_bucket = _FakeBucket(captured)
    monkeypatch.setattr(
        storage_service, "_storage_client", lambda: SimpleNamespace(bucket=lambda name: fake_bucket)
    )


@pytest.fixture(autouse=True)
def _reset_storage_client_cache():
    storage_service._client = None
    yield
    storage_service._client = None


async def _make_user(client: AsyncClient, display_name: str = "Avatar Haver") -> ApiUser:
    uid = f"uid-{uuid.uuid4().hex}"
    headers = {"X-Debug-Firebase-Uid": uid}
    email = f"{uid}@example.edu"
    response = await client.post(
        "/auth/bootstrap",
        json={"email": email, "display_name": display_name, "account_type": "non_greek"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return ApiUser(id=response.json()["id"], firebase_uid=uid, email=email, headers=headers)


async def test_the_avatar_is_finalized_to_avatars_not_posts(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The card's reason for existing.

    A posts/ destination here would put every profile picture in media_reconcile's
    delete set, because that job's reference set is posts.media_urls alone. This asserts
    the destination prefix directly rather than trusting the route not to regress to the
    default.
    """
    _configure_bucket(monkeypatch)
    captured: dict = {}
    _install_fake_gcs(monkeypatch, captured)
    user = await _make_user(client)
    tmp_name = f"tmp/{user.id}/portrait.jpg"

    response = await client.patch(
        "/auth/me", json={"avatar_object_name": tmp_name}, headers=user.headers
    )
    assert response.status_code == 200, response.text

    expected = f"https://storage.googleapis.com/{TEST_BUCKET}/avatars/{user.id}/portrait.jpg"
    assert response.json()["avatar_url"] == expected, (
        "the avatar must land under avatars/. A posts/ url here means media_reconcile "
        "will collect every profile picture about a day after it is set (c221)."
    )

    move = captured["copy_blob_calls"][0]
    assert move["source"] == tmp_name
    assert move["new_name"].startswith("avatars/"), move["new_name"]
    assert not move["new_name"].startswith("posts/"), (
        "finalized into posts/, which is media_reconcile's delete territory"
    )
    # Same conditional-copy contract post media relies on: asserts the destination does
    # not exist, which needs only create. An unconditional copy would require delete on
    # the destination prefix, which the runtime account deliberately lacks.
    assert move["kwargs"].get("if_generation_match") == 0


async def test_setting_a_picture_survives_a_reread(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is persisted, not just echoed back by the write."""
    _configure_bucket(monkeypatch)
    _install_fake_gcs(monkeypatch, {})
    user = await _make_user(client)

    await client.patch(
        "/auth/me",
        json={"avatar_object_name": f"tmp/{user.id}/pic.png"},
        headers=user.headers,
    )
    me = await client.get("/auth/me", headers=user.headers)
    assert me.status_code == 200, me.text
    assert me.json()["user"]["avatar_url"].endswith(f"/avatars/{user.id}/pic.png")


async def test_explicit_null_removes_the_picture_and_omission_leaves_it(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitted vs explicit null, the contract model_fields_set buys.

    Without the distinction, "I only want to change my name" would silently wipe the
    caller's profile picture - the failure mode a plain `if body.avatar is not None`
    produces.
    """
    _configure_bucket(monkeypatch)
    _install_fake_gcs(monkeypatch, {})
    user = await _make_user(client)
    await client.patch(
        "/auth/me", json={"avatar_object_name": f"tmp/{user.id}/a.jpg"}, headers=user.headers
    )

    # Omitting the field must not disturb the stored picture.
    renamed = await client.patch(
        "/auth/me", json={"display_name": "New Name"}, headers=user.headers
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["display_name"] == "New Name"
    assert renamed.json()["avatar_url"] is not None, "a name-only edit wiped the picture"

    # Explicit null clears it, back to initials.
    cleared = await client.patch(
        "/auth/me", json={"avatar_object_name": None}, headers=user.headers
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["avatar_url"] is None


async def test_you_cannot_claim_someone_elses_upload(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate post create already has, applied here.

    Object names are opaque UUIDs but they are NOT secret, and the bucket is public
    read - so without the user-scoped prefix check a caller could set their avatar to
    another user's tmp/ upload.
    """
    _configure_bucket(monkeypatch)
    _install_fake_gcs(monkeypatch, {})
    victim = await _make_user(client, "Victim")
    attacker = await _make_user(client, "Attacker")

    response = await client.patch(
        "/auth/me",
        json={"avatar_object_name": f"tmp/{victim.id}/private.jpg"},
        headers=attacker.headers,
    )
    assert response.status_code == 400, response.text

    me = await client.get("/auth/me", headers=attacker.headers)
    assert me.json()["user"]["avatar_url"] is None


async def test_a_raw_url_is_not_accepted_as_an_object_name(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The narrowing this route makes over UserUpdate.

    UserUpdate carries an avatar_url behind validate_public_url, which accepts any
    http(s) address - wiring that up would let anyone point their avatar at a tracking
    pixel or someone else's host. This route takes an object name only, so a url is not
    a valid input at all.
    """
    _configure_bucket(monkeypatch)
    _install_fake_gcs(monkeypatch, {})
    user = await _make_user(client)

    response = await client.patch(
        "/auth/me",
        json={"avatar_object_name": "https://evil.example.com/tracker.gif"},
        headers=user.headers,
    )
    assert response.status_code == 400, response.text


async def test_display_name_cannot_be_cleared(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """display_name is NOT NULL and is the only name anything renders.

    Refused with a named 422 rather than an IntegrityError surfacing as a 500, and
    rather than being silently ignored - which would tell the caller the edit worked.
    """
    _configure_bucket(monkeypatch)
    _install_fake_gcs(monkeypatch, {})
    user = await _make_user(client)

    response = await client.patch("/auth/me", json={"display_name": None}, headers=user.headers)
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "display_name_cannot_be_cleared"


async def test_the_route_only_ever_edits_the_caller(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No user id in the path or the body, so there is no shape that targets someone else.

    Pinned rather than assumed: a later "admin can edit any profile" change would have
    to add an id somewhere, and this test is what makes that visible.
    """
    _configure_bucket(monkeypatch)
    _install_fake_gcs(monkeypatch, {})
    one = await _make_user(client, "One")
    two = await _make_user(client, "Two")

    await client.patch("/auth/me", json={"display_name": "Renamed"}, headers=one.headers)

    other = await client.get("/auth/me", headers=two.headers)
    assert other.json()["user"]["display_name"] == "Two"


async def test_unauthenticated_callers_are_refused(client: AsyncClient) -> None:
    response = await client.patch("/auth/me", json={"display_name": "Nobody"})
    assert response.status_code == 401, response.text
