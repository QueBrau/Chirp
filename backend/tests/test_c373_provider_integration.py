"""Combined media/profile changes retain provider revalidation and commit-only telemetry."""
import asyncio
import json
import logging
import threading

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db import get_engine, get_session_factory
from app.routers import auth as auth_router
from app.services import storage_service


async def stored(user_id):
    async with get_session_factory()() as session:
        result = await session.execute(text(
            "SELECT display_name, avatar_url, account_type FROM users WHERE id=:id"
        ), {"id": user_id})
        return dict(result.mappings().one())


async def execute(sql, params=None):
    async with get_session_factory()() as session:
        await session.execute(text(sql), params or {})
        await session.commit()


def changes(caplog):
    return [json.loads(r.getMessage()) for r in caplog.records
            if r.name == "app.analytics"
            and json.loads(r.getMessage()).get("event") == "account_type_changed"]


@pytest.mark.parametrize("outcome", ["success", "suspended", "commit_failure"])
async def test_combined_avatar_account_change_stages_revalidates_and_emits_after_commit(
    client, make_user, monkeypatch, caplog, outcome,
):
    user = await make_user("Original name", account_type="non_greek")
    initial = await stored(user.id)
    entered, release = threading.Event(), threading.Event()
    copies = []
    avatar = f"https://storage.googleapis.com/local-fixture-media/avatars/{user.id}/profile.jpg"
    monkeypatch.setattr(storage_service.get_settings(), "media_bucket_name", "local-fixture-media")

    def provider(user_id, name, *, destination_prefix):
        copies.append((user_id, name, destination_prefix))
        entered.set()
        assert release.wait(5)
        return avatar

    monkeypatch.setattr(auth_router, "finalize_media_object", provider)
    if outcome == "commit_failure":
        await execute("""
            CREATE FUNCTION c355_c373_reject_account_change() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'profile integration commit failure' USING ERRCODE='23514';
            END $$
        """)
        await execute("""
            CREATE CONSTRAINT TRIGGER c355_c373_account_change
            AFTER UPDATE ON users DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
            WHEN (OLD.account_type IS DISTINCT FROM NEW.account_type)
            EXECUTE FUNCTION c355_c373_reject_account_change()
        """)
    caplog.clear()
    request = None
    try:
        with caplog.at_level(logging.INFO, logger="app.analytics"):
            request = asyncio.create_task(client.patch("/auth/me", headers=user.headers, json={
                "display_name": "Updated name", "account_type": "alumni",
                "avatar_object_name": f"tmp/{user.id}/profile.jpg",
            }))
            try:
                async with asyncio.timeout(3):
                    while not entered.is_set():
                        await asyncio.sleep(0.005)
                assert get_engine().pool.checkedout() == 0
                assert await stored(user.id) == initial
                assert changes(caplog) == []
                if outcome == "suspended":
                    await execute("UPDATE users SET suspended_at=now() WHERE id=:id", {"id": user.id})
            finally:
                release.set()

            if outcome == "commit_failure":
                with pytest.raises(DBAPIError):
                    await asyncio.wait_for(request, 5)
            else:
                response = await asyncio.wait_for(request, 5)
                assert response.status_code == (200 if outcome == "success" else 403), response.text
                if outcome == "suspended":
                    assert response.json()["detail"] == "account_suspended"

        assert len(copies) == 1
        assert copies[0][2] == "avatars"
        if outcome == "success":
            assert await stored(user.id) == {
                "display_name": "Updated name", "avatar_url": avatar, "account_type": "alumni",
            }
            [event] = changes(caplog)
            assert event["previous_account_type"] == "non_greek"
            assert event["account_type"] == "alumni"
            assert event["user_id"] == user.id
        else:
            assert await stored(user.id) == initial
            assert changes(caplog) == []
    finally:
        release.set()
        if request is not None and not request.done():
            await asyncio.gather(request, return_exceptions=True)
        if outcome == "commit_failure":
            await execute("DROP TRIGGER c355_c373_account_change ON users")
            await execute("DROP FUNCTION c355_c373_reject_account_change()")
