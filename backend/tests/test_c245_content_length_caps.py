"""c245: every user-supplied content body now has an upper bound.

Before this, each of these fields was `Field(min_length=1)` with nothing on the
other end, so the real ceiling was Cloud Run's 32MB request limit — one chirp
could push megabytes into a Text column that every reader of that feed then
downloads. The package already caps display_name at 80 and a poll question at
500, so bounding a body is the house convention; these fields had just been
missed.

EVERY CAP IS PROVEN TWICE HERE, and the second half is the one that matters. A
cap that fires on normal use is worse than no cap at all: it turns into a student
who cannot post and a bug report nobody can reproduce. So each test rejects one
character past the limit AND accepts a REALISTICALLY long real input — actual
prose of the kind the field exists to carry, not `"x" * n`. If a future tightening
of these numbers breaks the accept half, that is the test doing its job.

Deliberately NOT covered here: MessageCreate.ciphertext_b64. It is unbounded for
the same reason and belongs in the same sweep, but ciphertext expands over its
plaintext and needs a ceiling reasoned about in those terms rather than as text —
carded separately, and the messaging lane was live when this landed.
"""
from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy import text

from app.core.validation import (
    MAX_CHIRP_BODY_LENGTH,
    MAX_COMMENT_BODY_LENGTH,
    MAX_POST_BODY_LENGTH,
    MAX_REASON_LENGTH,
)
from tests.conftest import ApiUser, MakeCampus, MakeChapterWith, MakeUser, set_campus, verify_campus

# ---------------------------------------------------------------------------
# Realistic inputs: what the longest plausible REAL user actually writes.
#
# These are the accept half. Each is deliberately verbose for its field — a long
# one of its kind, not an average one — so the headroom being asserted is the
# headroom a real person has, and each carries a floor assertion so nobody can
# quietly shorten it until it stops proving anything.
# ---------------------------------------------------------------------------

# A student venting on the anonymous board at real length.
REALISTIC_CHIRP = (
    "does anyone else think the new parking policy is completely absurd. i pay "
    "the same permit fee i paid last year, the lot behind the rec center is now "
    "staff only after 4pm, and the overflow lot they told us to use is a fifteen "
    "minute walk in the dark. i have a night lab twice a week and campus safety "
    "told me to 'call the escort service' which has a forty minute wait most "
    "nights. i emailed parking services three weeks ago and got an automated "
    "reply telling me to consult the website that the policy is not even posted "
    "on yet. genuinely asking, who do you have to talk to around here to get an "
    "actual answer, because i am running out of people to ask and i am not "
    "paying another citation for parking in a lot i already have a permit for."
)

# A chapter announcement carrying full event detail — the long form the feed exists for.
REALISTIC_POST = (
    "RECRUITMENT WEEK SCHEDULE — please read all of this before Monday.\n\n"
    "Monday: open house at the chapter house, 6-9pm. Doors at 5:45 for anyone "
    "on setup. Business casual, no letters. We are expecting somewhere between "
    "sixty and ninety guys through the door across the three hours, so the front "
    "room needs to stay clear and everyone on rotation should be moving, not "
    "clustering with people they already know.\n\n"
    "Tuesday: philanthropy night, 7-9pm, benefiting the campus food pantry. "
    "Bring canned goods if you have them. We are matching donations up to five "
    "hundred dollars out of the philanthropy line, which finance approved at "
    "last week's meeting.\n\n"
    "Wednesday: interviews, by signup only. The sheet goes up Monday night and "
    "closes Tuesday at midnight. Twenty minute slots, two brothers per slot, and "
    "you are writing notes in the shared doc immediately after — not at the end "
    "of the night when you have done six of them and they have blurred together.\n\n"
    "Thursday: deliberations, chapter room, 8pm sharp. This runs long every year. "
    "Eat first. Bids go out Friday morning and we need the full list finalized "
    "before anyone leaves, so clear your night.\n\n"
    "Anyone who cannot make a night needs to tell the recruitment chair before "
    "Sunday, not the day of. We had eleven no-shows last fall and it was visible "
    "to the guys we were trying to recruit, which is the whole problem."
)

# A substantive reply, not a one-liner.
REALISTIC_COMMENT = (
    "worth adding that the food pantry takes fresh produce too, not just cans — "
    "i volunteered there last semester and they turn away a surprising amount of "
    "stuff for being expired, so check dates before you box things up. also they "
    "specifically need shelf-stable protein and toiletries, which nobody ever "
    "donates because everyone brings soup. if you are dropping off more than a "
    "couple of bags, message them first so someone is there to receive it."
)

# A report with the context a moderator actually needs to act on.
REALISTIC_REPORT_REASON = (
    "This account has posted the same message to the campus board six times in "
    "the last two hours, each time tagging a specific student by name and "
    "describing where she lives. She has asked them to stop in the comments and "
    "they reposted immediately after. I am reporting the most recent one but the "
    "earlier five are still up under the same author. This reads as targeted "
    "harassment rather than an argument that got heated, and the address detail "
    "is the part that worries me most."
)

# A moderator writing a real justification into the audit row.
REALISTIC_MODERATION_REASON = (
    "Removed after review. Targeted harassment of a named student including "
    "residence details, six reposts after being asked to stop, which puts it "
    "past the heated-argument line and into the conduct policy. Reporter was "
    "notified. Author has one prior warning from August for a similar pattern, "
    "so the next one should be a suspension rather than a removal."
)


def _one_past(limit: int) -> str:
    """Exactly one character over the cap — the smallest input that must be refused."""
    return "a" * (limit + 1)


async def _make_verified_campus_user(
    client: AsyncClient, campus_id: str, display_name: str = "Capped User"
) -> ApiUser:
    """A verified student pinned to `campus_id`, belonging to no chapter."""
    uid = f"uid-{uuid.uuid4().hex}"
    headers = {"X-Debug-Firebase-Uid": uid}
    email = f"{uid}@example.edu"
    response = await client.post(
        "/auth/bootstrap",
        json={"email": email, "display_name": display_name, "account_type": "non_greek"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    user = ApiUser(id=response.json()["id"], firebase_uid=uid, email=email, headers=headers)
    await set_campus(user.id, campus_id)
    return user


async def _campus_id_of_chapter(client: AsyncClient, setup) -> str:
    chapter = await client.get(
        f"/chapters/{setup.chapter_id}", headers=setup.president.headers
    )
    assert chapter.status_code == 200, chapter.text
    return chapter.json()["campus_id"]


# ---------------------------------------------------------------------------
# content bodies
# ---------------------------------------------------------------------------


async def test_chirp_body_cap_rejects_over_and_accepts_a_real_rant(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    campus_id = await make_campus()
    user = await _make_verified_campus_user(client, campus_id)

    too_long = await client.post(
        f"/campuses/{campus_id}/chirps",
        json={"body": _one_past(MAX_CHIRP_BODY_LENGTH)},
        headers=user.headers,
    )
    assert too_long.status_code == 422, too_long.text

    # The accept half: a genuinely long anonymous post still goes through, with
    # room to spare. If this ever fails, the cap is too tight for real use.
    assert len(REALISTIC_CHIRP) > 500, "sample got shortened until it proved nothing"
    assert len(REALISTIC_CHIRP) < MAX_CHIRP_BODY_LENGTH
    ok = await client.post(
        f"/campuses/{campus_id}/chirps",
        json={"body": REALISTIC_CHIRP},
        headers=user.headers,
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["body"] == REALISTIC_CHIRP


async def test_chirp_body_accepts_exactly_the_limit(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    """The boundary is inclusive: the cap is a ceiling, not a fence one short of it."""
    campus_id = await make_campus()
    user = await _make_verified_campus_user(client, campus_id)

    response = await client.post(
        f"/campuses/{campus_id}/chirps",
        json={"body": "a" * MAX_CHIRP_BODY_LENGTH},
        headers=user.headers,
    )
    assert response.status_code == 201, response.text


async def test_chapter_post_body_cap_rejects_over_and_accepts_a_real_announcement(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("president")

    too_long = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": _one_past(MAX_POST_BODY_LENGTH)},
        headers=setup.president.headers,
    )
    assert too_long.status_code == 422, too_long.text

    assert len(REALISTIC_POST) > 1000, "sample got shortened until it proved nothing"
    assert len(REALISTIC_POST) < MAX_POST_BODY_LENGTH
    ok = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": REALISTIC_POST},
        headers=setup.president.headers,
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["body"] == REALISTIC_POST


async def test_campus_post_body_cap_rejects_over_and_accepts_a_real_announcement(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    campus_id = await make_campus()
    user = await _make_verified_campus_user(client, campus_id)

    too_long = await client.post(
        f"/campuses/{campus_id}/posts",
        json={"body": _one_past(MAX_POST_BODY_LENGTH)},
        headers=user.headers,
    )
    assert too_long.status_code == 422, too_long.text

    ok = await client.post(
        f"/campuses/{campus_id}/posts",
        json={"body": REALISTIC_POST},
        headers=user.headers,
    )
    assert ok.status_code == 201, ok.text


async def test_post_update_body_is_capped_too(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The edit path writes the SAME column, so a ceiling only on create would leave
    the entire gap open behind a PATCH. This is the site the card did not list."""
    setup = await make_chapter_with("president")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "short enough to start with"},
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text
    post_id = created.json()["id"]

    too_long = await client.patch(
        f"/chapters/{setup.chapter_id}/posts/{post_id}",
        json={"body": _one_past(MAX_POST_BODY_LENGTH)},
        headers=setup.president.headers,
    )
    assert too_long.status_code == 422, too_long.text

    ok = await client.patch(
        f"/chapters/{setup.chapter_id}/posts/{post_id}",
        json={"body": REALISTIC_POST},
        headers=setup.president.headers,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["body"] == REALISTIC_POST


async def test_comment_body_cap_rejects_over_and_accepts_a_real_reply(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("president")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "a post worth replying to"},
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text
    post_id = created.json()["id"]

    too_long = await client.post(
        f"/posts/{post_id}/comments",
        json={"body": _one_past(MAX_COMMENT_BODY_LENGTH)},
        headers=setup.president.headers,
    )
    assert too_long.status_code == 422, too_long.text

    assert len(REALISTIC_COMMENT) > 300, "sample got shortened until it proved nothing"
    assert len(REALISTIC_COMMENT) < MAX_COMMENT_BODY_LENGTH
    ok = await client.post(
        f"/posts/{post_id}/comments",
        json={"body": REALISTIC_COMMENT},
        headers=setup.president.headers,
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["body"] == REALISTIC_COMMENT


# ---------------------------------------------------------------------------
# moderation reasons
#
# All five write a 'why' into a moderation_actions row that someone reads back
# later, so the cap has to leave room for a real explanation while keeping an
# essay out of the audit trail.
# ---------------------------------------------------------------------------


async def test_report_reason_cap_rejects_over_and_accepts_a_real_report(
    client: AsyncClient, make_campus: MakeCampus
) -> None:
    campus_id = await make_campus()
    reporter = await _make_verified_campus_user(client, campus_id, "Reporter")
    author = await _make_verified_campus_user(client, campus_id, "Author")
    chirp = await client.post(
        f"/campuses/{campus_id}/chirps",
        json={"body": "report me"},
        headers=author.headers,
    )
    assert chirp.status_code == 201, chirp.text
    chirp_id = chirp.json()["id"]

    too_long = await client.post(
        "/moderation/reports",
        json={
            "target_type": "chirp",
            "target_id": chirp_id,
            "reason": _one_past(MAX_REASON_LENGTH),
        },
        headers=reporter.headers,
    )
    assert too_long.status_code == 422, too_long.text

    assert len(REALISTIC_REPORT_REASON) > 300, "sample got shortened until it proved nothing"
    assert len(REALISTIC_REPORT_REASON) < MAX_REASON_LENGTH
    ok = await client.post(
        "/moderation/reports",
        json={
            "target_type": "chirp",
            "target_id": chirp_id,
            "reason": REALISTIC_REPORT_REASON,
        },
        headers=reporter.headers,
    )
    assert ok.status_code == 201, ok.text


async def test_chirp_remove_reason_cap_rejects_over_and_accepts_a_real_reason(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    await verify_campus(setup.member.id)
    await verify_campus(setup.president.id)
    campus_id = await _campus_id_of_chapter(client, setup)

    chirp = await client.post(
        f"/campuses/{campus_id}/chirps",
        json={"body": "moderate me"},
        headers=setup.member.headers,
    )
    assert chirp.status_code == 201, chirp.text
    chirp_id = chirp.json()["id"]

    too_long = await client.post(
        f"/moderation/chirps/{chirp_id}/remove",
        json={"reason": _one_past(MAX_REASON_LENGTH)},
        headers=setup.president.headers,
    )
    assert too_long.status_code == 422, too_long.text

    assert len(REALISTIC_MODERATION_REASON) < MAX_REASON_LENGTH
    ok = await client.post(
        f"/moderation/chirps/{chirp_id}/remove",
        json={"reason": REALISTIC_MODERATION_REASON},
        headers=setup.president.headers,
    )
    assert ok.status_code == 204, ok.text


async def test_suspend_reason_cap_rejects_over_and_accepts_a_real_reason(
    client: AsyncClient, make_user: MakeUser
) -> None:
    admin = await make_user("Platform Admin")
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(
            text("UPDATE users SET is_platform_admin = true WHERE id = :id"),
            {"id": admin.id},
        )
        await session.commit()
    target = await make_user("Rule Breaker")

    too_long = await client.post(
        f"/moderation/users/{target.id}/suspend",
        json={"reason": _one_past(MAX_REASON_LENGTH)},
        headers=admin.headers,
    )
    assert too_long.status_code == 422, too_long.text

    ok = await client.post(
        f"/moderation/users/{target.id}/suspend",
        json={"reason": REALISTIC_MODERATION_REASON},
        headers=admin.headers,
    )
    assert ok.status_code == 200, ok.text

    # The reason reached the audit row intact — a cap that silently truncated
    # would be worse than one that rejects.
    async with get_session_factory()() as session:
        stored = await session.execute(
            text(
                "SELECT reason FROM moderation_actions "
                "WHERE target_type = 'user' AND target_id = :id "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"id": target.id},
        )
        assert stored.scalar_one() == REALISTIC_MODERATION_REASON


async def test_content_remove_reason_cap_rejects_over_and_accepts_a_real_reason(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("president")
    await verify_campus(setup.president.id)
    created = await client.post(
        f"/chapters/{setup.chapter_id}/posts",
        json={"body": "remove me"},
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text
    post_id = created.json()["id"]

    too_long = await client.post(
        "/moderation/content/remove",
        json={
            "target_type": "post",
            "target_id": post_id,
            "reason": _one_past(MAX_REASON_LENGTH),
        },
        headers=setup.president.headers,
    )
    assert too_long.status_code == 422, too_long.text

    ok = await client.post(
        "/moderation/content/remove",
        json={
            "target_type": "post",
            "target_id": post_id,
            "reason": REALISTIC_MODERATION_REASON,
        },
        headers=setup.president.headers,
    )
    assert ok.status_code == 204, ok.text


async def test_report_resolve_reason_cap_rejects_over_and_accepts_a_real_reason(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    await verify_campus(setup.member.id)
    await verify_campus(setup.president.id)
    campus_id = await _campus_id_of_chapter(client, setup)

    chirp = await client.post(
        f"/campuses/{campus_id}/chirps",
        json={"body": "resolve a report about me"},
        headers=setup.member.headers,
    )
    assert chirp.status_code == 201, chirp.text
    report = await client.post(
        "/moderation/reports",
        json={
            "target_type": "chirp",
            "target_id": chirp.json()["id"],
            "reason": "needs review",
        },
        headers=setup.member.headers,
    )
    assert report.status_code == 201, report.text
    report_id = report.json()["id"]

    too_long = await client.patch(
        f"/moderation/reports/{report_id}",
        json={"status": "dismissed", "reason": _one_past(MAX_REASON_LENGTH)},
        headers=setup.president.headers,
    )
    assert too_long.status_code == 422, too_long.text

    ok = await client.patch(
        f"/moderation/reports/{report_id}",
        json={"status": "dismissed", "reason": REALISTIC_MODERATION_REASON},
        headers=setup.president.headers,
    )
    assert ok.status_code == 200, ok.text
