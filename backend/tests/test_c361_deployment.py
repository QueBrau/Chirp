"""Execute the actual deployment endpoint with Firebase verification and local PG."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import firebase_admin
from firebase_admin import auth as firebase_auth
from sqlalchemy import text

import app.middleware.auth as auth_module
from app.db import get_session, get_session_factory
from app.routers.deployment import packaged_schema_heads
from tests.conftest import verify_campus


async def test_deployment_auth_and_real_packaged_database_heads(client, make_user, monkeypatch):
    user = await make_user()
    monkeypatch.setattr(auth_module, "get_settings", lambda: SimpleNamespace(auth_mode="firebase"))
    monkeypatch.setattr(firebase_admin, "get_app", lambda: object())

    def verify(token):
        if token != "valid-fixture":
            raise ValueError("expired or invalid token")
        return {"uid": user.firebase_uid}

    monkeypatch.setattr(firebase_auth, "verify_id_token", verify)
    monkeypatch.setenv("K_SERVICE", "chirp-api")
    monkeypatch.setenv("K_REVISION", "chirp-api-c361")
    packaged_schema_heads.cache_clear()
    monkeypatch.chdir("/")  # The image's packaged heads cannot depend on launch cwd.
    for headers in ({}, {"Authorization": "Bearer invalid"}, {"Authorization": "Bearer expired"}):
        response = await client.get("/_deployment", headers=headers)
        assert response.status_code == 401
        assert "schema" not in response.text
    response = await client.get("/_deployment", headers={"Authorization": "Bearer valid-fixture"})
    assert response.status_code == 200
    async with get_session_factory()() as session:
        db_heads = (await session.execute(text("SELECT version_num FROM alembic_version ORDER BY version_num"))).scalars().all()
    assert db_heads and list(packaged_schema_heads()) == db_heads
    assert response.json() == {
        "service": "chirp-api", "revision": "chirp-api-c361",
        "code_schema_heads": db_heads, "database_schema_heads": db_heads,
    }


async def test_deployment_reports_actual_drift_and_rejects_suspended(client, make_user):
    user = await make_user()
    original = list(packaged_schema_heads())
    assert len(original) == 1
    async with get_session_factory()() as session:
        await session.execute(text("UPDATE alembic_version SET version_num = 'drifted-test-head'"))
        await session.commit()
    try:
        response = await client.get("/_deployment", headers=user.headers)
        assert response.status_code == 200
        assert response.json()["code_schema_heads"] == original
        assert response.json()["database_schema_heads"] == ["drifted-test-head"]
    finally:
        async with get_session_factory()() as session:
            await session.execute(text("UPDATE alembic_version SET version_num = :head"), {"head": original[0]})
            await session.execute(text("UPDATE users SET suspended_at = now() WHERE id = :id"), {"id": user.id})
            await session.commit()
    response = await client.get("/_deployment", headers=user.headers)
    assert response.status_code == 403


async def test_health_stays_independent_of_database_dependency(client):
    app = client._transport.app

    def unavailable_db():
        raise AssertionError("Liveness must not open a database session")

    app.dependency_overrides[get_session] = unavailable_db
    try:
        response = await client.get("/_health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
    finally:
        app.dependency_overrides.clear()


async def test_verifier_contracts_accept_real_auth_memberships_and_campus_feed(client, make_chapter_with):
    path = Path(__file__).resolve().parents[2] / "scripts/deploy_verify.py"
    spec = importlib.util.spec_from_file_location("c361_verifier", path)
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    setup = await make_chapter_with("historian")
    async with get_session_factory()() as session:
        campus_id = str((await session.execute(text("SELECT campus_id FROM chapters WHERE id = :id"), {"id": setup.chapter_id})).scalar_one())
    me = await client.get("/auth/me", headers=setup.member.headers)
    assert me.status_code == 200
    assert me.json()["memberships"][0]["role"] == "historian"
    assert verifier.valid_me(me.json(), setup.member.id, campus_id)
    unverified = await client.get(f"/campuses/{campus_id}/chirps?limit=1", headers=setup.member.headers)
    assert unverified.status_code == 403
    assert not verifier.valid_feed(unverified.json(), campus_id)
    await verify_campus(setup.member.id)
    created = await client.post(
        f"/campuses/{campus_id}/chirps", headers=setup.member.headers,
        json={"body": "Real authenticated campus fixture"},
    )
    assert created.status_code == 201, created.text
    feed = await client.get(f"/campuses/{campus_id}/chirps?limit=1", headers=setup.member.headers)
    assert feed.status_code == 200 and len(feed.json()) == 1
    assert verifier.valid_feed(feed.json(), campus_id)
