"""HTTP write admission refuses account mutations during a deletion hold."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app import models
from app.config import get_settings
from app.db import get_session_factory
from app.services.account_deletion_hold import activate_deletion_hold


def _dependency_names(dependant) -> set[str]:
    names: set[str] = set()
    for dependency in dependant.dependencies:
        call = dependency.call
        if call is not None:
            names.add(getattr(call, "__name__", ""))
        names.update(_dependency_names(dependency))
    return names


def _set_admission_flag(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", str(enabled).lower())
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    yield
    get_settings.cache_clear()


async def _activate_hold(user_id: str) -> uuid.UUID:
    """Create the operator-owned deletion request and activate its hold directly."""
    async with get_session_factory()() as session:
        request = models.AccountDataRequest(
            user_id=uuid.UUID(user_id), kind="deletion", open_key="open"
        )
        session.add(request)
        await session.flush()
        await activate_deletion_hold(
            session, user_id=uuid.UUID(user_id), request_id=request.id
        )
        await session.commit()
        return request.id


async def _profile(client, user, *, company: str):
    return await client.put(
        "/alumni/profile",
        headers=user.headers,
        json={"company": company, "open_to_mentoring": True},
    )


async def _job(client, user, chapter_id: str):
    return await client.post(
        "/jobs",
        headers=user.headers,
        json={
            "chapter_id": chapter_id,
            "title": "Counsel",
            "company": "Chirp",
            "location": "Remote",
            "description": "A bounded test job",
        },
    )


@pytest.mark.asyncio
async def test_active_hold_blocks_profile_and_job_writes_without_mutation(
    client, make_chapter_with, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission_flag(monkeypatch, True)
    setup = await make_chapter_with("alumni")
    profile = await _profile(client, setup.member, company="Before hold")
    assert profile.status_code == 200, profile.text
    job = await _job(client, setup.member, setup.chapter_id)
    assert job.status_code == 201, job.text
    job_id = job.json()["id"]

    await _activate_hold(setup.member.id)

    blocked_profile = await _profile(client, setup.member, company="After hold")
    blocked_job = await _job(client, setup.member, setup.chapter_id)
    blocked_delete = await client.delete(
        f"/jobs/{job_id}", headers=setup.member.headers
    )

    assert blocked_profile.status_code == 423, blocked_profile.text
    assert blocked_job.status_code == 423, blocked_job.text
    assert blocked_delete.status_code == 423, blocked_delete.text

    async with get_session_factory()() as session:
        stored_profile = await session.get(
            models.AlumniProfile, uuid.UUID(setup.member.id)
        )
        stored_job = await session.get(models.JobPost, uuid.UUID(job_id))
        assert stored_profile is not None
        assert stored_profile.company == "Before hold"
        assert stored_job is not None


@pytest.mark.asyncio
async def test_hold_keeps_owned_reads_and_privacy_status_available(
    client, make_chapter_with, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission_flag(monkeypatch, True)
    setup = await make_chapter_with("alumni")
    created = await _profile(client, setup.member, company="Readable")
    assert created.status_code == 200, created.text
    await _activate_hold(setup.member.id)

    profile = await client.get("/alumni/profile", headers=setup.member.headers)
    jobs = await client.get("/jobs", headers=setup.member.headers)
    requests = await client.get("/me/data-requests", headers=setup.member.headers)

    assert profile.status_code == 200, profile.text
    assert jobs.status_code == 200, jobs.text
    assert requests.status_code == 200, requests.text


@pytest.mark.asyncio
async def test_active_hold_isolated_to_owner_and_suspended_auth_still_wins(
    client, make_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission_flag(monkeypatch, True)
    owner = await make_user("Held owner")
    other = await make_user("Other owner")
    await _profile(client, owner, company="Owner")
    await _activate_hold(owner.id)

    other_write = await _profile(client, other, company="Other can write")
    assert other_write.status_code == 200, other_write.text

    async with get_session_factory()() as session:
        await session.execute(
            models.User.__table__.update()
            .where(models.User.id == uuid.UUID(other.id))
            .values(is_suspended=True)
        )
        await session.commit()

    suspended_write = await _profile(client, other, company="Suspended")
    assert suspended_write.status_code in {401, 403}


@pytest.mark.asyncio
async def test_flag_off_preserves_legacy_writes_and_does_not_activate_intake_hold(
    client, make_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission_flag(monkeypatch, False)
    user = await make_user("Compatibility user")
    response = await _profile(client, user, company="Legacy path")
    assert response.status_code == 200, response.text

    intake = await client.post(
        "/me/data-requests", headers=user.headers, json={"kind": "deletion"}
    )
    assert intake.status_code == 201, intake.text
    request_id = uuid.UUID(intake.json()["id"])
    async with get_session_factory()() as session:
        hold = await session.scalar(
            select(models.AccountDeletionHold).where(
                models.AccountDeletionHold.request_id == request_id
            )
        )
        assert hold is None


def test_only_alumni_mutations_are_enrolled_in_write_admission() -> None:
    """Guard the intended route inventory without claiming global enrollment."""
    from app.main import create_app

    app = create_app()
    expected = {
        ("PUT", "/alumni/profile"),
        ("POST", "/jobs"),
        ("DELETE", "/jobs/{job_id}"),
    }
    routes = {
        (method, route.path): route
        for route in app.routes
        if hasattr(route, "methods")
        for method in route.methods
    }
    for method, path in expected:
        route = routes[(method, path)]
        assert "get_current_user_for_write" in _dependency_names(route.dependant)

    for method, path in (("GET", "/alumni/profile"), ("GET", "/jobs")):
        route = routes[(method, path)]
        assert "get_current_user_for_write" not in _dependency_names(route.dependant)
