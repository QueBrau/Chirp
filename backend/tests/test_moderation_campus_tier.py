"""c142: moderation's c108 tier follows the target's actual audience, not its type.

THE BUG, and why "target_type == 'yak'" was wrong the moment c71 shipped: a Post can
carry audience="campus" and publish to the campus feed, exactly like a Yak does. But
resolve_report and remove_content both keyed the .edu-verification tier on
target_type alone, so a campus-audience POST read as chapter content for moderation
purposes even though feed.py requires a verified .edu to CREATE one. Net effect:
publishing a campus post needed more proof than removing it did — backwards, and
exploitable by an unverified officer in a DIFFERENT chapter on the SAME campus, since
campus_id for a post/comment report/removal was never chapter-scoped to begin with
(SECURITY-REVIEW finding 1 already established campus, not chapter, as the scoping
unit for moderation).

A SECOND, independent bug rode along: remove_content resolved campus_id by hopping
through the post's Chapter, which is nullable since c71 let a chapter-less student
post straight to their campus. That hop returned None for such a post, and
_require_eboard_for_campus treats None as "no campus matches" — 403ing every officer,
verified or not, for content that has a perfectly good campus_id sitting on the row.

THIS FILE IS THE TRIPWIRE for both, matching test_campus_gate.py's own convention: if
any test here starts passing where it must refuse, or refusing where it must pass, the
tier or the campus resolution has regressed.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.conftest import (
    ApiUser,
    ChapterSetup,
    MakeCampus,
    MakeUser,
    _grant_platform_admin,
    set_campus,
    verify_campus,
)

UNVERIFIED = "campus_unverified"


async def _make_chapter_on_campus(
    client: AsyncClient, make_user: MakeUser, campus_id: str, president_name: str
) -> ChapterSetup:
    """Same shape as conftest's make_chapter_with, parametrized on a caller-supplied
    campus rather than minting a fresh one — needed here because the exploit is
    specifically TWO CHAPTERS ON ONE CAMPUS, which make_chapter_with (one campus per
    call) cannot produce."""
    president = await make_user(president_name)
    await _grant_platform_admin(president.id)
    created = await client.post(
        "/chapters",
        json={
            "campus_id": campus_id,
            "org_name": f"Test Org {uuid.uuid4().hex[:6]}",
            "chapter_name": president_name,
        },
        headers=president.headers,
    )
    assert created.status_code == 201, created.text
    return ChapterSetup(chapter_id=created.json()["id"], member=president, president=president)


@pytest.fixture
async def two_chapters_same_campus(
    client: AsyncClient, make_user: MakeUser, make_campus: MakeCampus
) -> tuple[str, ChapterSetup, ChapterSetup]:
    """campus_id, chapter A, chapter B — the exact shape the exploit needs: a moderator
    in A must not get an easier bar for content that lives on B, just because both
    officers happen to share a campus."""
    campus_id = await make_campus()
    chapter_a = await _make_chapter_on_campus(client, make_user, campus_id, "President A")
    chapter_b = await _make_chapter_on_campus(client, make_user, campus_id, "President B")
    return campus_id, chapter_a, chapter_b


async def _campus_post(client: AsyncClient, chapter: ChapterSetup, audience: str) -> dict:
    response = await client.post(
        f"/chapters/{chapter.chapter_id}/posts",
        json={"body": f"{audience} post", "audience": audience},
        headers=chapter.president.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _report(client: AsyncClient, reporter: ApiUser, target_type: str, target_id: str) -> str:
    response = await client.post(
        "/moderation/reports",
        json={"target_type": target_type, "target_id": target_id, "reason": "spam"},
        headers=reporter.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


# ---------------------------------------------------------------------------
# The exploit, both routes: cross-chapter, same campus, campus-audience post
# ---------------------------------------------------------------------------


async def test_unverified_officer_cannot_remove_another_chapters_campus_post(
    client: AsyncClient, two_chapters_same_campus: tuple[str, ChapterSetup, ChapterSetup]
) -> None:
    """THE EXPLOIT ITSELF, unpatched behaviour: chapter A's UNVERIFIED president removes
    a campus-audience post authored from chapter B on the same campus. Before the fix
    this returned 204 - target_type == "post" always evaluated campus_content=False,
    so the .edu check never ran, regardless of the post's own audience."""
    campus_id, chapter_a, chapter_b = two_chapters_same_campus
    await verify_campus(chapter_b.president.id)  # author is verified; irrelevant to the bug
    post = await _campus_post(client, chapter_b, "campus")

    response = await client.post(
        "/moderation/content/remove",
        json={"target_type": "post", "target_id": post["id"], "reason": "spam"},
        headers=chapter_a.president.headers,  # unverified, different chapter
    )

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == UNVERIFIED


async def test_verified_officer_can_remove_another_chapters_campus_post(
    client: AsyncClient, two_chapters_same_campus: tuple[str, ChapterSetup, ChapterSetup]
) -> None:
    """The gate must refuse the right people and only them - without this, a rule that
    403s every officer would trivially pass the test above too."""
    campus_id, chapter_a, chapter_b = two_chapters_same_campus
    await verify_campus(chapter_a.president.id)
    await verify_campus(chapter_b.president.id)
    post = await _campus_post(client, chapter_b, "campus")

    response = await client.post(
        "/moderation/content/remove",
        json={"target_type": "post", "target_id": post["id"], "reason": "spam"},
        headers=chapter_a.president.headers,
    )

    assert response.status_code == 204, response.text


async def test_unverified_officer_cannot_dismiss_a_report_on_a_campus_post(
    client: AsyncClient, two_chapters_same_campus: tuple[str, ChapterSetup, ChapterSetup]
) -> None:
    """resolve_report had the identical hole - dismissing is how a bad actor makes a
    complaint about their own campus-wide post disappear, and target_type=='post' let
    an unverified officer do exactly that."""
    campus_id, chapter_a, chapter_b = two_chapters_same_campus
    await verify_campus(chapter_b.president.id)
    post = await _campus_post(client, chapter_b, "campus")
    reporter = chapter_b.president
    report_id = await _report(client, reporter, "post", post["id"])

    response = await client.patch(
        f"/moderation/reports/{report_id}",
        json={"status": "dismissed", "reason": "not spam"},
        headers=chapter_a.president.headers,
    )

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == UNVERIFIED


async def test_verified_officer_can_dismiss_a_report_on_a_campus_post(
    client: AsyncClient, two_chapters_same_campus: tuple[str, ChapterSetup, ChapterSetup]
) -> None:
    campus_id, chapter_a, chapter_b = two_chapters_same_campus
    await verify_campus(chapter_a.president.id)
    await verify_campus(chapter_b.president.id)
    post = await _campus_post(client, chapter_b, "campus")
    report_id = await _report(client, chapter_b.president, "post", post["id"])

    response = await client.patch(
        f"/moderation/reports/{report_id}",
        json={"status": "dismissed", "reason": "not spam"},
        headers=chapter_a.president.headers,
    )

    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# Comments inherit their parent post's tier
# ---------------------------------------------------------------------------


async def test_unverified_officer_cannot_remove_a_comment_on_a_campus_post(
    client: AsyncClient, two_chapters_same_campus: tuple[str, ChapterSetup, ChapterSetup]
) -> None:
    """A comment carries no audience of its own - it must inherit its parent post's,
    or a campus-post's comments become the soft spot the post itself no longer is."""
    campus_id, chapter_a, chapter_b = two_chapters_same_campus
    await verify_campus(chapter_b.president.id)
    post = await _campus_post(client, chapter_b, "campus")
    comment = await client.post(
        f"/posts/{post['id']}/comments",
        json={"body": "a comment"},
        headers=chapter_b.president.headers,
    )
    assert comment.status_code == 201, comment.text

    response = await client.post(
        "/moderation/content/remove",
        json={"target_type": "comment", "target_id": comment.json()["id"], "reason": "spam"},
        headers=chapter_a.president.headers,
    )

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == UNVERIFIED


async def test_verified_officer_can_remove_a_comment_on_a_campus_post(
    client: AsyncClient, two_chapters_same_campus: tuple[str, ChapterSetup, ChapterSetup]
) -> None:
    campus_id, chapter_a, chapter_b = two_chapters_same_campus
    await verify_campus(chapter_a.president.id)
    await verify_campus(chapter_b.president.id)
    post = await _campus_post(client, chapter_b, "campus")
    comment = await client.post(
        f"/posts/{post['id']}/comments",
        json={"body": "a comment"},
        headers=chapter_b.president.headers,
    )
    assert comment.status_code == 201, comment.text

    response = await client.post(
        "/moderation/content/remove",
        json={"target_type": "comment", "target_id": comment.json()["id"], "reason": "spam"},
        headers=chapter_a.president.headers,
    )

    assert response.status_code == 204, response.text


# ---------------------------------------------------------------------------
# The second bug: a chapter-less campus post must still be moderatable
# ---------------------------------------------------------------------------


async def test_a_chapter_less_campus_post_is_moderatable_by_a_verified_officer(
    client: AsyncClient,
    make_user: MakeUser,
    two_chapters_same_campus: tuple[str, ChapterSetup, ChapterSetup],
) -> None:
    """Before the fix this 403'd EVERYONE, including a verified officer of a chapter
    on the very campus the post belongs to - remove_content hopped through the post's
    Chapter for campus_id, and a chapter-less post (c71: a student with no chapter
    posts straight to their campus) has none. Reading post.campus_id directly, which
    is always set, fixes this as a side effect of fixing the tier bug."""
    campus_id, chapter_a, _chapter_b = two_chapters_same_campus
    await verify_campus(chapter_a.president.id)
    student = await make_user("Chapterless Student")
    # set_campus, not verify_campus: this student has NO chapter, so c96's
    # chapter-derives-campus_id path never ran for them - campus_id has to be set
    # explicitly, which is exactly what set_campus(..., verified=True) does in one step.
    await set_campus(student.id, campus_id, verified=True)

    post = await client.post(
        f"/campuses/{campus_id}/posts",
        json={"body": "chapterless campus post"},
        headers=student.headers,
    )
    assert post.status_code == 201, post.text
    assert post.json()["chapter_id"] is None, "fixture must produce a genuinely chapter-less post"

    response = await client.post(
        "/moderation/content/remove",
        json={"target_type": "post", "target_id": post.json()["id"], "reason": "spam"},
        headers=chapter_a.president.headers,
    )

    assert response.status_code == 204, response.text


async def test_an_unverified_officer_is_still_refused_on_a_chapter_less_campus_post(
    client: AsyncClient,
    make_user: MakeUser,
    two_chapters_same_campus: tuple[str, ChapterSetup, ChapterSetup],
) -> None:
    """The fix for the over-restrictive bug must not become a new bypass: a
    chapter-less post is still campus-wide content, so an UNVERIFIED officer must
    still be refused, same as any other campus post."""
    campus_id, chapter_a, _chapter_b = two_chapters_same_campus
    student = await make_user("Chapterless Student 2")
    await set_campus(student.id, campus_id, verified=True)

    post = await client.post(
        f"/campuses/{campus_id}/posts",
        json={"body": "chapterless campus post"},
        headers=student.headers,
    )
    assert post.status_code == 201, post.text

    response = await client.post(
        "/moderation/content/remove",
        json={"target_type": "post", "target_id": post.json()["id"], "reason": "spam"},
        headers=chapter_a.president.headers,  # unverified
    )

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == UNVERIFIED


# ---------------------------------------------------------------------------
# Regression guard: org-audience content must still need only the officer role
# ---------------------------------------------------------------------------


async def test_unverified_officer_can_still_remove_their_own_chapters_org_post(
    client: AsyncClient, two_chapters_same_campus: tuple[str, ChapterSetup, ChapterSetup]
) -> None:
    """c108's original, still-correct half: chapter membership grants chapter content
    with no email at all. An unverified president must still be able to moderate an
    ORG-audience post in their OWN chapter - if this starts 403ing, campus_content is
    leaking True onto genuinely chapter-scoped content."""
    _campus_id, chapter_a, _chapter_b = two_chapters_same_campus
    post = await _campus_post(client, chapter_a, "org")

    response = await client.post(
        "/moderation/content/remove",
        json={"target_type": "post", "target_id": post["id"], "reason": "spam"},
        headers=chapter_a.president.headers,  # unverified, same chapter
    )

    assert response.status_code == 204, response.text


async def test_unverified_officer_can_still_dismiss_a_report_on_their_own_org_post(
    client: AsyncClient, two_chapters_same_campus: tuple[str, ChapterSetup, ChapterSetup]
) -> None:
    _campus_id, chapter_a, _chapter_b = two_chapters_same_campus
    post = await _campus_post(client, chapter_a, "org")
    report_id = await _report(client, chapter_a.president, "post", post["id"])

    response = await client.patch(
        f"/moderation/reports/{report_id}",
        json={"status": "dismissed", "reason": "not spam"},
        headers=chapter_a.president.headers,
    )

    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# c147 (security's #66): the campus_content default when a comment's parent post
# cannot be resolved must agree with _report_campus_content's fail-closed default.
# ---------------------------------------------------------------------------


def test_content_campus_tier_defaults_to_campus_when_the_source_post_is_unresolvable() -> None:
    """Pins the REAL shared function both remove_content and _report_campus_content
    call - not a copy of its formula, and not a mock of the database.

    Why this is a direct call rather than a request through the API: post_comments.post_id
    is `NOT NULL REFERENCES posts(id)` with no ON DELETE and not DEFERRABLE (see
    alembic/versions/0001_initial.py), so Postgres itself refuses any INSERT or UPDATE
    that would leave a comment's post_id pointing at a row that does not exist - there is
    no way to construct a genuinely dangling comment through the ORM, the API, or even raw
    SQL without dropping the constraint for every other test sharing this database. That is
    exactly why this was reachable only in principle before c147: "safe today only because
    the campus_id-is-None check fires first."

    c147 extracted `_content_campus_tier(source_post: Post | None) -> bool` specifically so
    this scenario is testable WITHOUT needing an impossible row and WITHOUT mocking
    session.get (this project does not mock the database in its tests) - it is a pure
    function of an optional Post, so the "unresolvable" case is just calling it with None,
    exactly as remove_content does the moment session.get(Post, ...) finds nothing.
    """
    from app.routers.moderation import _content_campus_tier

    assert _content_campus_tier(None) is True, (
        "an unresolvable source post must default to the STRICTER campus tier, "
        "matching _report_campus_content - not silently to chapter content"
    )


def test_content_campus_tier_follows_the_resolved_posts_own_audience() -> None:
    """The non-default cases, so c147's refactor is pinned on both sides, not just
    the one it fixed."""
    from app import models
    from app.routers.moderation import _content_campus_tier

    assert _content_campus_tier(models.Post(audience="campus")) is True
    assert _content_campus_tier(models.Post(audience="org")) is False
