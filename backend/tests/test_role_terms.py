"""role_terms: dated role history per membership (board card c83).

Jose's Aug 24 ruling, binding: a chapter role is a DATED TERM, not a plain fact.
Covers the write side (PATCH /chapters/{chapter_id}/members closes the open term and
opens a new one via app.services.role_term_service.apply_role_change, no-ops when
the role is unchanged), the read side (GET .../members/{user_id}/role-terms, newest
first, gated like the roster), and the one-open-term-per-membership invariant at the
database level.

Migration 0021's BACKFILL correctness is covered separately in
test_role_terms_backfill.py — it needs a database with memberships already present
BEFORE 0021 runs, and the session-scoped `migrated_db` fixture used everywhere else
in this suite has already fast-forwarded to head before any test's data exists.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import models
from app.db import get_session_factory
from tests.conftest import ChapterSetup, MakeChapterWith, MakeUser


async def _role_terms(client: AsyncClient, setup: ChapterSetup, user_id: str) -> list[dict]:
    response = await client.get(
        f"/chapters/{setup.chapter_id}/members/{user_id}/role-terms",
        headers=setup.president.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_membership_creation_seeds_one_open_term(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """join_chapter (and create_chapter's founding president) seed an initial open
    term matching the role the membership started with — otherwise the FIRST role
    change on a post-0021 membership would have no open term to close and would
    silently lose the founding role from history."""
    setup = await make_chapter_with("member")

    terms = await _role_terms(client, setup, setup.member.id)

    assert len(terms) == 1
    assert terms[0]["role"] == "member"
    assert terms[0]["ended_at"] is None
    assert terms[0]["changed_by"] is None, "no admin acted — a membership just began existing"


async def test_role_change_closes_old_term_and_opens_new(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """(a) The core c83 write behavior: PATCHing a member to a NEW role closes their
    prior open term (stamping ended_at) and opens a fresh one carrying the new role
    and changed_by = the acting president, while memberships.role is kept in sync."""
    setup = await make_chapter_with("member")
    original_term_id = (await _role_terms(client, setup, setup.member.id))[0]["id"]

    response = await client.patch(
        f"/chapters/{setup.chapter_id}/members",
        json={"user_id": setup.member.id, "role": "treasurer"},
        headers=setup.president.headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "treasurer", "memberships.role stays in sync"

    terms = await _role_terms(client, setup, setup.member.id)
    assert len(terms) == 2, "the old term closes, a new one opens — never a bare overwrite"

    newest, closed = terms[0], terms[1]
    assert newest["id"] != original_term_id
    assert newest["role"] == "treasurer"
    assert newest["ended_at"] is None, "the new term is the open one"
    assert newest["changed_by"] == setup.president.id

    assert closed["id"] == original_term_id
    assert closed["role"] == "member", "history keeps the role that was actually held"
    assert closed["ended_at"] is not None, "the old term is now closed"


async def test_role_change_history_newest_first(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A member promoted twice accumulates three terms, always returned newest first."""
    setup = await make_chapter_with("member")

    for role in ("secretary", "vice_president"):
        response = await client.patch(
            f"/chapters/{setup.chapter_id}/members",
            json={"user_id": setup.member.id, "role": role},
            headers=setup.president.headers,
        )
        assert response.status_code == 200, response.text

    terms = await _role_terms(client, setup, setup.member.id)
    assert [t["role"] for t in terms] == ["vice_president", "secretary", "member"]
    assert terms[0]["ended_at"] is None
    assert all(t["ended_at"] is not None for t in terms[1:])


async def test_unchanged_role_is_a_no_op(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """(b) PATCHing a member with the role they ALREADY hold must not fabricate a new
    term or a changed_by 'change' for a role that never changed — board card c83,
    explicit: "No-op when the new role equals the current one." A status/pledge_class
    edit in the same request (a form that always sends every field) exercises the
    realistic trigger for this."""
    setup = await make_chapter_with("member")
    original = (await _role_terms(client, setup, setup.member.id))[0]

    response = await client.patch(
        f"/chapters/{setup.chapter_id}/members",
        json={
            "user_id": setup.member.id,
            "role": "member",
            "pledge_class": "Fall 2025",
        },
        headers=setup.president.headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["pledge_class"] == "Fall 2025", "non-role fields still apply"

    terms = await _role_terms(client, setup, setup.member.id)
    assert len(terms) == 1, "no new term was cut"
    assert terms[0]["id"] == original["id"]
    assert terms[0]["ended_at"] is None, "the original term is still open"
    assert terms[0]["changed_by"] is None, "still nobody's change — this member was never touched"


async def test_one_open_term_per_membership_is_enforced_in_postgres(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """(c) The partial unique index — not just apply_role_change's application-level
    discipline — is what makes two open terms for one membership impossible. Insert
    a second open row directly, bypassing the service entirely, and the database
    itself must refuse it."""
    setup = await make_chapter_with("member")

    session_factory = get_session_factory()
    async with session_factory() as session:
        result = await session.execute(
            select(models.Membership.id).where(
                models.Membership.user_id == uuid.UUID(setup.member.id),
                models.Membership.chapter_id == uuid.UUID(setup.chapter_id),
            )
        )
        membership_id = result.scalar_one()
        session.add(
            models.RoleTerm(
                membership_id=membership_id,
                role="treasurer",
                started_at=datetime.now(timezone.utc),
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_role_terms_requires_chapter_membership(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """(e) auth: a non-member of the chapter is refused before the query ever runs,
    same 403 shape as the roster (org scoping, §8.4)."""
    chapter_a = await make_chapter_with("member")
    chapter_b = await make_chapter_with("member")

    response = await client.get(
        f"/chapters/{chapter_b.chapter_id}/members/{chapter_b.member.id}/role-terms",
        headers=chapter_a.member.headers,
    )
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "not_a_member"}


async def test_role_terms_visible_to_any_active_member_not_admin_only(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """(e) permission gate: a plain member of the SAME chapter can read another
    member's role history — the endpoint is gated exactly like the roster
    (get_current_membership), not tightened to MEMBERS_ADMIN, per the explicit
    build instruction to reuse the existing member-data-read pattern."""
    setup = await make_chapter_with("member")

    response = await client.get(
        f"/chapters/{setup.chapter_id}/members/{setup.president.id}/role-terms",
        headers=setup.member.headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()[0]["role"] == "president"


async def test_role_terms_404s_for_a_user_not_a_member_of_this_chapter(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """The membership lookup is scoped by BOTH chapter_id and user_id, so a real
    user who simply never joined THIS chapter 404s rather than leaking an empty
    list or another chapter's history."""
    setup = await make_chapter_with("member")
    stranger = await make_user("Stranger Danger")

    response = await client.get(
        f"/chapters/{setup.chapter_id}/members/{stranger.id}/role-terms",
        headers=setup.president.headers,
    )
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "membership_not_found"}
