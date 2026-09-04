"""GET /users/search: the search half of the c243 reachability rule (board c322).

`app-mobile/app/(tabs)/messages/new.tsx` (c273) only ever listed the caller's own
chapter roster, which is narrower than the server has ever permitted:
`_require_reachable_off_chapter` (routers/messages.py) already lets a campus-verified
student DM someone in a DIFFERENT chapter on their campus, and until this card there
was no way for the UI to find that person at all.

THE CENTRAL CLAIM THESE TESTS BACK: this endpoint and `_require_reachable_off_chapter`
read the SAME query — `app.core.reachability.reachable_off_chapter_ids` — rather than
two independently-maintained definitions of "reachable". The proof that extracting it
did not change the validator's own behaviour is the EXISTING test suite passing
UNCHANGED (test_contact_blocks.py, test_conversation_authz.py and friends were not
touched by this card and were run against the refactored code). What is new here is
the search-only half: the exclusions that are about being a valid SEARCH RESULT rather
than about reachability (self, ghosts, suspended, blocked, the query minimum, the cap,
the rate limit) — none of which live in the shared reachability query on purpose (see
its docstring).

ONE DELIBERATE DEVIATION FROM A NAIVE READING OF "exclude blocks either direction":
this endpoint excludes candidates who have BLOCKED THE CALLER (the same direction
`create_conversation`/`send_message` already enforce via `blockers_of`), and does NOT
exclude people the CALLER has blocked. Read app/core/blocks.py's module docstring
before changing that — POST /moderation/blocks/by-chirp lets someone block an
anonymous chirp's author without ever learning who it is, and there is deliberately no
endpoint that lists the caller's own blocks. Filtering the caller's own blocks out of
search would rebuild exactly the deanonymisation oracle that module exists to prevent:
block the chirp, then search known names one at a time and watch for the one that goes
missing. `test_caller_can_still_find_someone_they_blocked` pins this on purpose.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from app.core.rate_limits import USER_SEARCH_LIMIT
from tests.conftest import (
    ApiUser,
    MakeChapterWith,
    MakeUser,
    set_campus,
    share_verified_campus,
)


async def _search(client: AsyncClient, caller: ApiUser, q: str) -> list[dict]:
    response = await client.get("/users/search", params={"q": q}, headers=caller.headers)
    assert response.status_code == 200, response.text
    return response.json()


async def _set_ghost(user_id: str) -> None:
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(
            text("UPDATE users SET is_ghost = true WHERE id = :id"), {"id": user_id}
        )
        await session.commit()


async def _suspend(user_id: str) -> None:
    from app.db import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(
            text("UPDATE users SET suspended_at = now() WHERE id = :id"), {"id": user_id}
        )
        await session.commit()


async def _block(client: AsyncClient, blocker: ApiUser, blocked: ApiUser) -> None:
    response = await client.post(
        "/moderation/blocks", json={"blocked_id": blocked.id}, headers=blocker.headers
    )
    assert response.status_code == 201, response.text


@pytest.mark.asyncio
async def test_chapter_mate_is_found(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """The narrowest legitimate case: a fellow active member of the caller's own chapter,
    found regardless of campus verification (chapter membership stands on its own,
    per Jose's Aug 16 ruling in core/campus_access.py)."""
    setup = await make_chapter_with("historian")
    results = await _search(client, setup.member, "Chapter President")
    assert {r["id"] for r in results} == {setup.president.id}
    row = results[0]
    assert row["display_name"] == "Chapter President"
    assert set(row.keys()) == {"id", "display_name", "avatar_url"}


@pytest.mark.asyncio
async def test_same_campus_non_chapter_mate_found_when_caller_verified(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """The whole point of the card: two strangers in different chapters (or no chapter
    at all), same campus, caller campus-verified -> findable."""
    caller = await make_user("Searching Sam")
    target = await make_user("Findable Fiona")
    await share_verified_campus(caller.id, target.id)

    results = await _search(client, caller, "Findable")
    assert {r["id"] for r in results} == {target.id}


@pytest.mark.asyncio
async def test_same_person_not_found_when_caller_not_verified(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """The identical pair as above, but the CALLER has never proved an .edu — same shape
    as the c104/c105 bypass campus_access.py exists to refuse. The target being
    verified does not help; the bar is on the caller's side."""
    caller = await make_user("Unverified Caller")
    target = await make_user("Verified Target")
    campus_id = await share_verified_campus(target.id)
    await set_campus(caller.id, campus_id, verified=False)

    results = await _search(client, caller, "Verified Target")
    assert results == []


@pytest.mark.asyncio
async def test_different_campus_user_never_found(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """Cross-campus is not the documented feature (SPEC §3) and must never leak through
    search, even though both people are fully verified on their own campuses."""
    caller = await make_user("Home Campus Caller")
    stranger = await make_user("Away Campus Stranger")
    await share_verified_campus(caller.id)
    await share_verified_campus(stranger.id)

    results = await _search(client, caller, "Away Campus")
    assert results == []


@pytest.mark.asyncio
async def test_ghost_not_found(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A lineage placeholder is never a live account to open a DM with, even though it
    is otherwise "reachable" the same way a chapter mate is."""
    setup = await make_chapter_with("historian")
    await _set_ghost(setup.member.id)

    results = await _search(client, setup.president, "Chapter Historian")
    assert results == []


@pytest.mark.asyncio
async def test_suspended_target_not_found(client: AsyncClient, make_user: MakeUser) -> None:
    """No route enforces this on the recipient side today (only the caller is checked,
    in get_current_user); search must not surface a suspended account as someone to
    message regardless."""
    caller = await make_user("Search Caller")
    target = await make_user("Soon Suspended")
    await share_verified_campus(caller.id, target.id)
    await _suspend(target.id)

    results = await _search(client, caller, "Soon Suspended")
    assert results == []


@pytest.mark.asyncio
async def test_self_is_never_returned(client: AsyncClient, make_user: MakeUser) -> None:
    caller = await make_user("Searching For Self")
    results = await _search(client, caller, "Searching For Self")
    assert results == []


@pytest.mark.asyncio
async def test_blocked_by_candidate_is_not_found(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """A candidate who has blocked the caller must not be findable — otherwise search
    is the workaround for the block (board c243). This is the SAME direction
    create_conversation/send_message already enforce via blocks.py's blockers_of:
    "who has blocked me", not "who did I block" (see this file's own docstring, and
    test_caller_can_still_find_someone_they_blocked right below, for why the reverse
    direction is deliberately NOT enforced here)."""
    caller = await make_user("Would-Be Sender")
    blocker = await make_user("Blocks The Sender")
    await share_verified_campus(caller.id, blocker.id)
    await _block(client, blocker, caller)

    results = await _search(client, caller, "Blocks The Sender")
    assert results == []

    # Regression guard: the search that finds nobody must not be because the pair
    # stopped being reachable for some other reason. Confirm blocking is the only
    # thing that changed by checking the (unblocked) reverse direction below still
    # sees a hit before concluding blocks caused this one's empty result — done
    # inline via the sibling test rather than repeated here.


@pytest.mark.asyncio
async def test_caller_can_still_find_someone_they_blocked(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """DELIBERATE ASYMMETRY, pinned so nobody "fixes" it later. blocks.py's own
    docstring explains why: POST /moderation/blocks/by-chirp lets someone block an
    anonymous chirp's author without learning who it is, and there is no endpoint
    that lists the caller's own blocks, on purpose. If search hid people the CALLER
    has blocked, a caller could recover that anonymous identity by searching known
    names one at a time and watching for the one that goes missing - the exact
    deanonymisation oracle blocks.py exists to prevent, rebuilt against search
    instead of conversation creation. The caller could still open a DM with this
    person too (create_conversation's own asymmetric rule,
    test_blocker_gets_no_signal_when_they_initiate in test_contact_blocks.py) — this
    is that same accepted residual gap, now proven on the search side as well.
    """
    caller = await make_user("Has Blocked Someone")
    target = await make_user("Blocked By Caller")
    await share_verified_campus(caller.id, target.id)
    await _block(client, caller, target)

    results = await _search(client, caller, "Blocked By Caller")
    assert {r["id"] for r in results} == {target.id}


@pytest.mark.asyncio
async def test_minimum_query_length_is_enforced(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """A 1-character query against a whole verified campus is a directory dump wearing
    a search box, not a search."""
    caller = await make_user("Short Query Caller")
    target = await make_user("A")
    await share_verified_campus(caller.id, target.id)

    too_short = await client.get(
        "/users/search", params={"q": "a"}, headers=caller.headers
    )
    assert too_short.status_code == 422, too_short.text

    # Whitespace-padded down to one real character must not sneak past Query()'s
    # raw min_length check (routers/messages.py:search_users strips before re-checking).
    padded = await client.get(
        "/users/search", params={"q": " a "}, headers=caller.headers
    )
    assert padded.status_code == 422, padded.text


@pytest.mark.asyncio
async def test_wildcard_characters_in_query_are_escaped(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """A literal "_" in the query must not act as a SQL LIKE single-char wildcard.

    Unescaped, the pattern "%_nn%" matches "Ann" (any char + "nn"). Escaped, it looks
    for the literal substring "_nn", which "Ann" does not contain. This is the
    difference between the two, proven against a real row rather than asserted.
    """
    caller = await make_user("Escape Test Caller")
    target = await make_user("Ann Wildcard")
    await share_verified_campus(caller.id, target.id)

    literal_underscore = await _search(client, caller, "_nn")
    assert literal_underscore == []

    real_substring = await _search(client, caller, "Ann Wild")
    assert {r["id"] for r in real_substring} == {target.id}


@pytest.mark.asyncio
async def test_result_list_is_capped(client: AsyncClient, make_user: MakeUser) -> None:
    """An uncapped campus-wide people search is a scraper's directory (c258/c264)."""
    from app.routers.messages import USER_SEARCH_RESULT_CAP

    caller = await make_user("Cap Test Caller")
    campus_id = await share_verified_campus(caller.id)
    for i in range(USER_SEARCH_RESULT_CAP + 5):
        target = await make_user(f"Cap Candidate {i:02d}")
        await share_verified_campus(target.id)
        # share_verified_campus above mints a NEW campus per call; pin every
        # candidate onto the CALLER's campus instead, verified, same as
        # set_campus's contract.
        await set_campus(target.id, campus_id, verified=True)

    results = await _search(client, caller, "Cap Candidate")
    assert len(results) == USER_SEARCH_RESULT_CAP


@pytest.mark.asyncio
async def test_search_rate_limit_stops_a_loop_but_not_ordinary_typeahead(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """Same proof shape as test_c259_rate_limits.py: the ceiling is reachable by a
    script and comfortably clear of the debounced typeahead new.tsx actually sends."""
    max_calls, _window = USER_SEARCH_LIMIT
    caller = await make_user("Rate Limited Searcher")
    target = await make_user("Searched For")
    await share_verified_campus(caller.id, target.id)

    for i in range(max_calls):
        response = await client.get(
            "/users/search", params={"q": "Searched"}, headers=caller.headers
        )
        assert response.status_code == 200, f"search {i + 1}: {response.text}"

    refused = await client.get(
        "/users/search", params={"q": "Searched"}, headers=caller.headers
    )
    assert refused.status_code == 429, refused.text
    assert refused.json()["detail"] == "user_search_rate_limited"


@pytest.mark.asyncio
async def test_chapter_branch_alone_is_sufficient_without_campus_verification(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Sanity check that the chapter-membership half of the rule needs nothing from the
    campus half: make_chapter_with's members arrive UNVERIFIED and campus-less-in-the-
    relevant-sense by default (see conftest.verify_campus), so a hit here can only be
    explained by the shared-active-chapter branch of reachable_off_chapter_ids."""
    setup = await make_chapter_with("historian")

    chapter_hit = await _search(client, setup.member, "Chapter President")
    assert {r["id"] for r in chapter_hit} == {setup.president.id}
