"""Live polls for the secretary dashboard (board card c162).

The tests that matter most here are not the happy path. They are:

  * one member cannot produce two votes, even by changing their mind;
  * a closed poll refuses ballots;
  * an option from ANOTHER poll cannot be voted for, which the composite foreign
    key makes impossible at the schema level and which would otherwise corrupt a
    tally into plausible-but-wrong numbers;
  * nothing in any response says who voted for what.

That last one is a product guarantee, not a detail: a chapter voting on money or
on people needs the ballot to be secret, so it is asserted rather than assumed.
"""

from __future__ import annotations

from datetime import datetime, timezone

from httpx import AsyncClient

from tests.conftest import MakeChapterWith


async def _create_poll(
    client: AsyncClient, setup, question: str = "Approve the budget?", **kwargs
) -> dict:
    body = {"question": question, "options": ["Yes", "No"], **kwargs}
    created = await client.post(
        f"/chapters/{setup.chapter_id}/polls", json=body, headers=setup.member.headers
    )
    assert created.status_code == 201, created.text
    return created.json()


async def test_secretary_opens_a_poll_with_options_in_order(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("secretary")
    poll = await _create_poll(client, setup, options=["Yes", "No", "Abstain"])

    assert poll["status"] == "open"
    assert poll["total_votes"] == 0
    assert poll["my_option_id"] is None
    assert [opt["text"] for opt in poll["options"]] == ["Yes", "No", "Abstain"]
    assert [opt["position"] for opt in poll["options"]] == [0, 1, 2]
    assert all(opt["votes"] == 0 for opt in poll["options"])


async def test_a_plain_member_cannot_open_a_poll(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    refused = await client.post(
        f"/chapters/{setup.chapter_id}/polls",
        json={"question": "Should I be in charge?", "options": ["Yes", "No"]},
        headers=setup.member.headers,
    )
    assert refused.status_code == 403, refused.text


async def test_a_poll_needs_at_least_two_distinct_non_blank_options(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("secretary")
    for options in (["Yes"], ["Yes", "yes"], ["Yes", "   "]):
        refused = await client.post(
            f"/chapters/{setup.chapter_id}/polls",
            json={"question": "Well?", "options": options},
            headers=setup.member.headers,
        )
        assert refused.status_code == 422, f"{options} -> {refused.text}"


async def test_a_member_votes_and_the_tally_moves(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("secretary")
    poll = await _create_poll(client, setup)
    yes = poll["options"][0]["id"]

    voted = await client.post(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}/vote",
        json={"option_id": yes},
        headers=setup.president.headers,
    )
    assert voted.status_code == 200, voted.text
    body = voted.json()
    assert body["total_votes"] == 1
    assert body["my_option_id"] == yes
    assert {opt["id"]: opt["votes"] for opt in body["options"]}[yes] == 1


async def test_changing_a_vote_replaces_it_rather_than_adding_one(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """The one-vote-per-member guarantee, which is a PRIMARY KEY and not a rule the
    route remembers to apply."""
    setup = await make_chapter_with("secretary")
    poll = await _create_poll(client, setup)
    yes, no = poll["options"][0]["id"], poll["options"][1]["id"]

    await client.post(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}/vote",
        json={"option_id": yes},
        headers=setup.president.headers,
    )
    changed = await client.post(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}/vote",
        json={"option_id": no},
        headers=setup.president.headers,
    )
    assert changed.status_code == 200, changed.text
    body = changed.json()
    assert body["total_votes"] == 1, "a changed mind must not become a second ballot"
    counts = {opt["id"]: opt["votes"] for opt in body["options"]}
    assert counts[yes] == 0 and counts[no] == 1
    assert body["my_option_id"] == no


async def test_two_members_vote_independently(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("secretary")
    poll = await _create_poll(client, setup)
    yes, no = poll["options"][0]["id"], poll["options"][1]["id"]

    await client.post(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}/vote",
        json={"option_id": yes},
        headers=setup.member.headers,
    )
    second = await client.post(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}/vote",
        json={"option_id": no},
        headers=setup.president.headers,
    )
    body = second.json()
    assert body["total_votes"] == 2
    assert {opt["id"]: opt["votes"] for opt in body["options"]} == {yes: 1, no: 1}


async def test_the_response_never_says_who_voted(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Ballot secrecy. my_option_id describes the CALLER and nobody else."""
    setup = await make_chapter_with("secretary")
    poll = await _create_poll(client, setup)
    yes = poll["options"][0]["id"]

    # The president votes; the secretary who opened the poll reads it without voting.
    await client.post(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}/vote",
        json={"option_id": yes},
        headers=setup.president.headers,
    )
    seen = await client.get(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}",
        headers=setup.member.headers,
    )
    body = seen.json()
    assert body["total_votes"] == 1
    assert body["my_option_id"] is None, "another member's ballot must not leak"
    # The voter's id appears NOWHERE. Asserted against the raw body rather than the
    # parsed one so a future field carrying an identity fails this too. The poll's
    # own created_by is the secretary here, which is why the voter is someone else.
    assert setup.president.id not in seen.text


async def test_a_closed_poll_refuses_ballots(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("secretary")
    poll = await _create_poll(client, setup)

    closed = await client.post(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}/close",
        headers=setup.member.headers,
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "closed"
    assert closed.json()["closed_at"] is not None

    refused = await client.post(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}/vote",
        json={"option_id": poll["options"][0]["id"]},
        headers=setup.president.headers,
    )
    assert refused.status_code == 409, refused.text


async def test_closing_twice_is_a_no_op_not_an_error(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Two officers tapping close is ordinary; the second has done nothing wrong."""
    setup = await make_chapter_with("secretary")
    poll = await _create_poll(client, setup)
    first = await client.post(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}/close",
        headers=setup.member.headers,
    )
    second = await client.post(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}/close",
        headers=setup.member.headers,
    )
    assert second.status_code == 200, second.text
    assert second.json()["closed_at"] == first.json()["closed_at"]


async def test_a_plain_member_cannot_close_a_poll(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("member")
    poll = await _create_poll(
        client,
        type("S", (), {"chapter_id": setup.chapter_id, "member": setup.president})(),
    )
    refused = await client.post(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}/close",
        headers=setup.member.headers,
    )
    assert refused.status_code == 403, refused.text


async def test_voting_with_another_polls_option_is_refused(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """Otherwise the tally is wrong in a way that looks like a plausible number."""
    setup = await make_chapter_with("secretary")
    poll_a = await _create_poll(client, setup, question="A?")
    poll_b = await _create_poll(client, setup, question="B?")

    refused = await client.post(
        f"/chapters/{setup.chapter_id}/polls/{poll_a['id']}/vote",
        json={"option_id": poll_b["options"][0]["id"]},
        headers=setup.president.headers,
    )
    assert refused.status_code == 404, refused.text


async def test_polls_are_scoped_to_their_chapter(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    home = await make_chapter_with("secretary")
    other = await make_chapter_with("secretary")
    poll = await _create_poll(client, home)

    seen = await client.get(
        f"/chapters/{other.chapter_id}/polls/{poll['id']}", headers=other.member.headers
    )
    assert seen.status_code == 404, seen.text


async def test_a_poll_cannot_attach_to_another_chapters_meeting(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    home = await make_chapter_with("secretary")
    other = await make_chapter_with("secretary")
    meeting = await client.post(
        f"/chapters/{other.chapter_id}/meetings",
        json={"title": "Theirs", "meeting_date": datetime.now(timezone.utc).isoformat()},
        headers=other.member.headers,
    )
    assert meeting.status_code == 201, meeting.text

    refused = await client.post(
        f"/chapters/{home.chapter_id}/polls",
        json={
            "question": "Whose meeting?",
            "options": ["Yes", "No"],
            "meeting_id": meeting.json()["id"],
        },
        headers=home.member.headers,
    )
    assert refused.status_code == 404, refused.text


async def test_listing_filters_by_meeting_and_reports_my_own_ballot(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("secretary")
    meeting = await client.post(
        f"/chapters/{setup.chapter_id}/meetings",
        json={"title": "Chapter", "meeting_date": datetime.now(timezone.utc).isoformat()},
        headers=setup.member.headers,
    )
    meeting_id = meeting.json()["id"]

    attached = await _create_poll(client, setup, question="Attached?", meeting_id=meeting_id)
    await _create_poll(client, setup, question="Standalone?")

    await client.post(
        f"/chapters/{setup.chapter_id}/polls/{attached['id']}/vote",
        json={"option_id": attached["options"][0]["id"]},
        headers=setup.president.headers,
    )

    everything = await client.get(
        f"/chapters/{setup.chapter_id}/polls", headers=setup.president.headers
    )
    assert everything.status_code == 200, everything.text
    assert len(everything.json()) == 2

    filtered = await client.get(
        f"/chapters/{setup.chapter_id}/polls?meeting_id={meeting_id}",
        headers=setup.president.headers,
    )
    body = filtered.json()
    assert len(body) == 1
    assert body[0]["question"] == "Attached?"
    assert body[0]["my_option_id"] == attached["options"][0]["id"]
    assert body[0]["total_votes"] == 1


async def test_deleting_a_poll_takes_its_ballots_with_it(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    setup = await make_chapter_with("secretary")
    poll = await _create_poll(client, setup)
    await client.post(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}/vote",
        json={"option_id": poll["options"][0]["id"]},
        headers=setup.president.headers,
    )

    deleted = await client.delete(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}", headers=setup.member.headers
    )
    assert deleted.status_code == 204, deleted.text

    gone = await client.get(
        f"/chapters/{setup.chapter_id}/polls/{poll['id']}", headers=setup.member.headers
    )
    assert gone.status_code == 404
