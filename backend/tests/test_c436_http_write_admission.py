"""HTTP write admission refuses account mutations during a deletion hold."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import event, select, text

from app import models
from app.config import get_settings
from app.db import get_session_factory
from app.services.account_deletion_hold import activate_deletion_hold


def _set_admission_flag(
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    *,
    legal_enforcement: bool | None = None,
) -> None:
    monkeypatch.setenv("ACCOUNT_DELETION_HTTP_ADMISSION_ENABLED", str(enabled).lower())
    if legal_enforcement is not None:
        monkeypatch.setenv("LEGAL_ENFORCEMENT_ENABLED", str(legal_enforcement).lower())
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
        await session.commit()
        request_id = request.id
    # The hold primitive must be the first operation in its transaction. The
    # request itself is operator-created fixture state, so activate in a fresh
    # transaction rather than after the request INSERT flush.
    async with get_session_factory()() as session:
        await activate_deletion_hold(
            session, user_id=uuid.UUID(user_id), request_id=request_id
        )
        await session.commit()
    return request_id


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
            text("UPDATE users SET suspended_at = now() WHERE id = :id"),
            {"id": uuid.UUID(other.id)},
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
    from app.db import get_engine

    statements: list[str] = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    engine = get_engine()
    event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        response = await _profile(client, user, company="Legacy path")
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_sql)
    assert response.status_code == 200, response.text
    assert not any("account_deletion_holds" in statement for statement in statements)

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


@pytest.mark.asyncio
async def test_legal_enforcement_still_rejects_unaccepted_write(
    client, make_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission_flag(monkeypatch, True, legal_enforcement=True)
    user = await make_user("Legal gate user")
    response = await _profile(client, user, company="Must accept first")
    assert response.status_code == 428, response.text
    assert response.json()["detail"] == "legal_acceptance_required"


@pytest.mark.asyncio
async def test_invalid_guard_is_sanitized_by_actual_http_handler(
    client, make_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_admission_flag(monkeypatch, True)
    user = await make_user("Guard failure user")
    from app.services import account_write_guard
    from app.services.account_deletion_hold import DeletionHoldInvalid

    def fail_bind(*_args, **_kwargs):
        raise DeletionHoldInvalid("internal identity or SQL detail")

    monkeypatch.setattr(account_write_guard, "bind_account_write_guard", fail_bind)
    response = await _profile(client, user, company="Should not write")
    assert response.status_code == 503
    assert response.json() == {"detail": "account_write_admission_unavailable"}


def test_only_alumni_mutations_are_enrolled_in_write_admission() -> None:
    """Guard the intended route inventory without claiming global enrollment."""
    import inspect

    from app.routers import alumni

    expected = (
        alumni.upsert_own_profile,
        alumni.create_job_post,
        alumni.delete_job_post,
    )
    for endpoint in expected:
        dependency = inspect.signature(endpoint).parameters["user"].default
        assert dependency.dependency.__name__ == "get_current_user_for_write"

    for endpoint in (alumni.get_own_profile, alumni.list_job_posts):
        dependency = inspect.signature(endpoint).parameters["user"].default
        assert dependency.dependency.__name__ != "get_current_user_for_write"
