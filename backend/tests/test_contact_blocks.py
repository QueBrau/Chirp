"""Blocks must stop CONTACT, not just hide content (board card c243).

Before this card user_blocks was consulted only by the feed and chirp read paths, so
blocking someone hid their posts and stopped nothing else — they could still open a DM,
add you to a group, invite you to their event and buzz your phone. These tests pin the
contact half, and they pin the DIRECTION, which is the subtle part: enforcement is
asymmetric on purpose (see app/core/blocks.py), because a symmetric rule would turn
POST /moderation/blocks/by-chirp into a deanonymisation oracle. Anyone tempted to "fix"
the asymmetry should read test_blocker_gets_no_signal_when_they_initiate first — it is
that oracle written down as a test.
"""
from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from tests.conftest import (
    MakeChapterWith,
    MakeUser,
    RegisterDevice,
    b64,
    share_verified_campus,
)


async def _block(client: AsyncClient, blocker: Any, blocked: Any) -> None:
    """blocker blocks blocked through the ordinary named-block endpoint."""
    response = await client.post(
        "/moderation/blocks",
        json={"blocked_id": blocked.id},
        headers=blocker.headers,
    )
    assert response.status_code == 201, response.text


async def _open_dm(client: AsyncClient, creator: Any, other: Any) -> Any:
    return await client.post(
        "/conversations",
        json={"kind": "dm", "member_user_ids": [other.id]},
        headers=creator.headers,
    )


async def _send(
    client: AsyncClient, conversation_id: str, sender: Any, device_id: str
) -> Any:
    return await client.post(
        f"/conversations/{conversation_id}/messages",
        json={
            "sender_device_id": device_id,
            "ciphertext_b64": b64(b"opaque-ciphertext"),
            "message_type": "signal",
        },
        headers=sender.headers,
    )


@pytest.mark.asyncio
async def test_blocked_user_cannot_open_a_conversation_with_their_blocker(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """The headline bug: blocking someone must stop them starting a new thread with you."""
    alice = await make_user("Alice Blocker")
    mallory = await make_user("Mallory Blocked")
    await share_verified_campus(alice.id, mallory.id)

    # Before the block these two are ordinary campus peers, so the refusal below is
    # attributable to the block and nothing else.
    before = await _open_dm(client, mallory, alice)
    assert before.status_code == 201, before.text

    await _block(client, alice, mallory)

    after = await _open_dm(client, mallory, alice)
    assert after.status_code == 403, after.text
    assert after.json() == {"detail": "recipient_not_reachable"}


@pytest.mark.asyncio
async def test_blocked_user_cannot_send_into_an_existing_conversation(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    """A thread that predates the block is not a standing licence to keep talking.

    The block is very often created BECAUSE of what was said in this exact thread, so
    enforcing only at conversation-creation would leave the original channel wide open.
    """
    alice = await make_user("Alice Blocker")
    mallory = await make_user("Mallory Blocked")
    await share_verified_campus(alice.id, mallory.id)
    device = await register_device(mallory, one_time_prekey_count=1)

    created = await _open_dm(client, mallory, alice)
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]

    first = await _send(client, conversation_id, mallory, device["id"])
    assert first.status_code == 201, first.text

    await _block(client, alice, mallory)

    after = await _send(client, conversation_id, mallory, device["id"])
    assert after.status_code == 403, after.text
    assert after.json() == {"detail": "recipient_not_reachable"}

    # The refused send must not have stored ciphertext — the block check runs before the
    # insert precisely so a blocked message leaves nothing behind for a later reader. We
    # can no longer observe that from alice's own view (c348): once history filters by
    # the READER's current blocks, alice is now blocking mallory, so mallory's earlier,
    # legitimately-sent message is ALSO hidden from alice — 0, not 1. Read it back as
    # mallory instead: mallory blocks no one, so her view is unfiltered and still proves
    # the refused second send stored nothing (exactly 1 message, not 2).
    as_alice = await client.get(
        f"/conversations/{conversation_id}/messages", headers=alice.headers
    )
    assert as_alice.status_code == 200, as_alice.text
    assert as_alice.json() == []

    as_mallory = await client.get(
        f"/conversations/{conversation_id}/messages", headers=mallory.headers
    )
    assert as_mallory.status_code == 200, as_mallory.text
    assert len(as_mallory.json()) == 1


@pytest.mark.asyncio
async def test_blocker_gets_no_signal_when_they_initiate(
    client: AsyncClient, make_user: MakeUser
) -> None:
    """Enforcement is asymmetric, and this test is the reason why (SPEC §8.3).

    POST /moderation/blocks/by-chirp lets someone block an anonymous chirp's author
    without learning who it is, and there is no endpoint that lists your own blocks. If a
    block also stopped the BLOCKER from reaching the person they blocked, they could
    recover that identity by trying to DM each candidate and watching for the one that is
    refused. So the blocker's own blocks are never consulted when they are the one making
    contact, and this asserts that they get back an ordinary 201 with nothing to observe.

    The residual gap is deliberate and self-healing: alice can still reach mallory, and
    mallory blocking back is exactly the case the tests above enforce.
    """
    alice = await make_user("Alice Blocker")
    mallory = await make_user("Mallory Blocked")
    await share_verified_campus(alice.id, mallory.id)

    await _block(client, alice, mallory)

    initiated = await _open_dm(client, alice, mallory)
    assert initiated.status_code == 201, initiated.text
    assert {m["user_id"] for m in initiated.json()["members"]} == {alice.id, mallory.id}


@pytest.mark.asyncio
async def test_block_is_enforced_on_the_chapter_path_too(
    client: AsyncClient, make_chapter_with: MakeChapterWith
) -> None:
    """A shared chapter does not make contact consensual.

    The chapter branch had its own membership check and no block check, so naming a
    chapter_id was a way around the block for anyone still on the roster.
    """
    setup = await make_chapter_with("historian")

    before = await client.post(
        "/conversations",
        json={
            "chapter_id": setup.chapter_id,
            "kind": "dm",
            "member_user_ids": [setup.member.id],
        },
        headers=setup.president.headers,
    )
    assert before.status_code == 201, before.text

    await _block(client, setup.member, setup.president)

    after = await client.post(
        "/conversations",
        json={
            "chapter_id": setup.chapter_id,
            "kind": "dm",
            "member_user_ids": [setup.member.id],
        },
        headers=setup.president.headers,
    )
    assert after.status_code == 403, after.text
    assert after.json() == {"detail": "recipient_not_reachable"}


@pytest.mark.asyncio
async def test_group_send_skips_blockers_but_still_reaches_everyone_else(
    client: AsyncClient,
    make_user: MakeUser,
    register_device: RegisterDevice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One member's block must silence the sender FOR THEM, not for the whole group.

    Refusing the entire send would hand any single member a veto over everyone else's
    group chat, which is its own abuse. So the send succeeds and the blocker is dropped
    from both the websocket fan-out and the push — the two surfaces that actually reach
    a phone.
    """
    creator = await make_user("Group Creator")
    blocker = await make_user("Group Blocker")
    bystander = await make_user("Group Bystander")
    await share_verified_campus(creator.id, blocker.id, bystander.id)
    device = await register_device(creator, one_time_prekey_count=1)

    created = await client.post(
        "/conversations",
        json={
            "kind": "group",
            "title": "Retreat Planning",
            "member_user_ids": [blocker.id, bystander.id],
        },
        headers=creator.headers,
    )
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]

    await _block(client, blocker, creator)

    published: list[str] = []
    pushed: list[str] = []

    async def _capture_publish(user_id: str, event: dict[str, Any]) -> None:
        published.append(user_id)

    async def _capture_push(user_id: str, title: str) -> None:
        pushed.append(user_id)

    import app.routers.messages as messages_router
    import app.ws.pubsub as pubsub

    monkeypatch.setattr(pubsub, "publish_to_user", _capture_publish)
    # messages.py binds both names at import time (from-import), so patch its module too.
    monkeypatch.setattr(messages_router, "publish_to_user", _capture_publish)
    monkeypatch.setattr(messages_router, "send_content_free_push", _capture_push)

    sent = await _send(client, conversation_id, creator, device["id"])
    assert sent.status_code == 201, sent.text

    assert blocker.id not in published, "a blocker must not receive the ws fan-out"
    assert blocker.id not in pushed, "a blocker must not be pushed to"
    assert bystander.id in published, "the group must still work for everyone else"
    assert bystander.id in pushed


@pytest.mark.asyncio
async def test_event_invite_skips_someone_who_blocked_the_host(
    client: AsyncClient, make_chapter_with: MakeChapterWith, make_user: MakeUser
) -> None:
    """An invite grants read access and lands in /me/event-invites, so it is contact.

    Skipped silently rather than refused: a 403 naming the skipped ids would tell the
    host exactly who blocked them, and inviting a roster must not fail wholesale over one
    blocked member.
    """
    setup = await make_chapter_with("member")
    guest = await make_user("Blocking Guest")
    created = await client.post(
        f"/chapters/{setup.chapter_id}/events",
        json={
            "title": "Rush Week Mixer",
            "starts_at": "2026-09-27T19:00:00Z",
            "location": "Chapter House",
            "cover_url": "https://picsum.photos/seed/rush/800/600",
        },
        headers=setup.president.headers,
    )
    assert created.status_code == 201, created.text
    event_id = created.json()["id"]

    await _block(client, guest, setup.president)

    invited = await client.post(
        f"/events/{event_id}/invites",
        json={"user_ids": [guest.id]},
        headers=setup.president.headers,
    )
    assert invited.status_code == 201, invited.text
    assert guest.id not in {i["invited_user_id"] for i in invited.json()}

    # The read access an invite would have granted must not have been granted either.
    reachable = await client.get(f"/events/{event_id}", headers=guest.headers)
    assert reachable.status_code == 403, reachable.text
    mine = await client.get("/me/event-invites", headers=guest.headers)
    assert mine.status_code == 200, mine.text
    assert mine.json() == []


@pytest.mark.asyncio
async def test_unblocked_campus_peers_are_completely_unaffected(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    """The regression guard: a guard that breaks ordinary messaging is worse than the bug.

    Two verified peers on one campus, no blocks anywhere — open a DM, send, and read it
    back, all of it untouched by c243.
    """
    alice = await make_user("Ordinary Alice")
    bob = await make_user("Ordinary Bob")
    await share_verified_campus(alice.id, bob.id)
    device = await register_device(alice, one_time_prekey_count=1)

    created = await _open_dm(client, alice, bob)
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]

    sent = await _send(client, conversation_id, alice, device["id"])
    assert sent.status_code == 201, sent.text

    history = await client.get(
        f"/conversations/{conversation_id}/messages", headers=bob.headers
    )
    assert history.status_code == 200, history.text
    assert len(history.json()) == 1


async def _open_group(
    client: AsyncClient, creator: Any, others: list[Any], title: str = "Group"
) -> Any:
    return await client.post(
        "/conversations",
        json={
            "kind": "group",
            "title": title,
            "member_user_ids": [other.id for other in others],
        },
        headers=creator.headers,
    )


@pytest.mark.asyncio
async def test_history_filters_blocked_sender_before_limit_keeps_page_full(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    """History filtering must happen in SQL before LIMIT, or a page comes back short.

    a blocks b. c sends first (2 messages), then b sends LAST (3 messages), so the 3
    newest-by-created_at rows in the whole conversation are ALL b's — the exact layout
    that would break a naive "LIMIT then filter in Python" implementation, which would
    see its top-2 raw rows both belong to the blocked sender and return an empty (or
    wrong) page instead of a full one.
    """
    a = await make_user("History A")
    b = await make_user("History B")
    c = await make_user("History C")
    await share_verified_campus(a.id, b.id, c.id)
    device_b = await register_device(b, one_time_prekey_count=1)
    device_c = await register_device(c, one_time_prekey_count=1)

    created = await _open_group(client, a, [b, c])
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]

    await _block(client, a, b)

    for _ in range(2):
        sent = await _send(client, conversation_id, c, device_c["id"])
        assert sent.status_code == 201, sent.text
    for _ in range(3):
        sent = await _send(client, conversation_id, b, device_b["id"])
        assert sent.status_code == 201, sent.text

    page = await client.get(
        f"/conversations/{conversation_id}/messages",
        params={"limit": 2},
        headers=a.headers,
    )
    assert page.status_code == 200, page.text
    rows = page.json()
    assert len(rows) == 2, "the page must stay full, not shortened by the hidden rows"
    assert {row["sender_device_id"] for row in rows} == {device_c["id"]}


@pytest.mark.asyncio
async def test_history_pagination_never_surfaces_blocked_sender_across_pages(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    """Walking every page with the cursor must never surface a blocked sender's rows,
    and must terminate cleanly once the visible rows are exhausted."""
    a = await make_user("Paging A")
    b = await make_user("Paging B")
    c = await make_user("Paging C")
    await share_verified_campus(a.id, b.id, c.id)
    device_b = await register_device(b, one_time_prekey_count=1)
    device_c = await register_device(c, one_time_prekey_count=1)

    created = await _open_group(client, a, [b, c])
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]

    await _block(client, a, b)

    for _ in range(2):
        assert (await _send(client, conversation_id, c, device_c["id"])).status_code == 201
    for _ in range(3):
        assert (await _send(client, conversation_id, b, device_b["id"])).status_code == 201

    collected: list[dict[str, Any]] = []
    params: dict[str, Any] = {"limit": 1}
    max_calls = 10  # generous upper bound; the real bound asserted below is tighter
    calls = 0
    while calls < max_calls:
        calls += 1
        page = await client.get(
            f"/conversations/{conversation_id}/messages",
            params=params,
            headers=a.headers,
        )
        assert page.status_code == 200, page.text
        rows = page.json()
        if not rows:
            break
        collected.extend(rows)
        last = rows[-1]
        params = {"limit": 1, "before": last["created_at"], "before_id": last["id"]}

    assert calls <= 3, "pagination must not stall trying to skip filtered rows"
    assert len(collected) == 2
    assert {row["sender_device_id"] for row in collected} == {device_c["id"]}


@pytest.mark.asyncio
async def test_history_uninvolved_member_sees_everyone(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    """Filtering is per-reader (the caller's own blocks), not a global hide on the row."""
    a = await make_user("Uninvolved A")
    b = await make_user("Uninvolved B")
    c = await make_user("Uninvolved C")
    await share_verified_campus(a.id, b.id, c.id)
    device_b = await register_device(b, one_time_prekey_count=1)
    device_c = await register_device(c, one_time_prekey_count=1)

    created = await _open_group(client, a, [b, c])
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]

    await _block(client, a, b)

    for _ in range(2):
        assert (await _send(client, conversation_id, c, device_c["id"])).status_code == 201
    for _ in range(3):
        assert (await _send(client, conversation_id, b, device_b["id"])).status_code == 201

    as_c = await client.get(
        f"/conversations/{conversation_id}/messages",
        params={"limit": 200},
        headers=c.headers,
    )
    assert as_c.status_code == 200, as_c.text
    assert len(as_c.json()) == 5
    assert device_b["id"] in {row["sender_device_id"] for row in as_c.json()}


@pytest.mark.asyncio
async def test_blocked_sender_own_history_view_matches_control_shape(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    """A blocker's outbound is never consulted (app/core/blocks.py) — that must hold for
    the blocked person's OWN read of their own messages too: no oracle field, no
    truncation, no reordering distinguishes 'someone blocked me' from 'no one did'."""
    a = await make_user("Shape A")
    b = await make_user("Shape B")
    c = await make_user("Shape C")
    await share_verified_campus(a.id, b.id, c.id)
    device_b = await register_device(b, one_time_prekey_count=1)
    device_c = await register_device(c, one_time_prekey_count=1)

    created = await _open_group(client, a, [b, c])
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]
    await _block(client, a, b)
    for _ in range(2):
        assert (await _send(client, conversation_id, c, device_c["id"])).status_code == 201
    for _ in range(3):
        assert (await _send(client, conversation_id, b, device_b["id"])).status_code == 201

    control_a = await make_user("Control A")
    control_b = await make_user("Control B")
    control_c = await make_user("Control C")
    await share_verified_campus(control_a.id, control_b.id, control_c.id)
    control_device_b = await register_device(control_b, one_time_prekey_count=1)
    control_device_c = await register_device(control_c, one_time_prekey_count=1)
    control_created = await _open_group(client, control_a, [control_b, control_c])
    assert control_created.status_code == 201, control_created.text
    control_conversation_id = control_created.json()["id"]
    for _ in range(2):
        assert (
            await _send(client, control_conversation_id, control_c, control_device_c["id"])
        ).status_code == 201
    for _ in range(3):
        assert (
            await _send(client, control_conversation_id, control_b, control_device_b["id"])
        ).status_code == 201

    as_b = await client.get(
        f"/conversations/{conversation_id}/messages",
        params={"limit": 200},
        headers=b.headers,
    )
    as_control_b = await client.get(
        f"/conversations/{control_conversation_id}/messages",
        params={"limit": 200},
        headers=control_b.headers,
    )
    assert as_b.status_code == 200, as_b.text
    assert as_control_b.status_code == 200, as_control_b.text
    assert len(as_b.json()) == 5
    assert len(as_control_b.json()) == 5
    expected_keys = {
        "id",
        "conversation_id",
        "sender_device_id",
        "ciphertext_b64",
        "message_type",
        "created_at",
    }
    assert set(as_b.json()[0].keys()) == expected_keys
    assert set(as_control_b.json()[0].keys()) == expected_keys


@pytest.mark.asyncio
async def test_history_hides_retroactively_after_block_created_post_send(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    """A message that predates the block is not grandfathered in — history is read live
    off current UserBlock state, not snapshotted at send time."""
    alice = await make_user("Retro Alice")
    mallory = await make_user("Retro Mallory")
    await share_verified_campus(alice.id, mallory.id)
    device = await register_device(mallory, one_time_prekey_count=1)

    created = await _open_dm(client, mallory, alice)
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]

    sent = await _send(client, conversation_id, mallory, device["id"])
    assert sent.status_code == 201, sent.text

    await _block(client, alice, mallory)

    as_alice = await client.get(
        f"/conversations/{conversation_id}/messages", headers=alice.headers
    )
    assert as_alice.status_code == 200, as_alice.text
    assert as_alice.json() == []

    as_mallory = await client.get(
        f"/conversations/{conversation_id}/messages", headers=mallory.headers
    )
    assert as_mallory.status_code == 200, as_mallory.text
    assert len(as_mallory.json()) == 1


@pytest.mark.asyncio
async def test_history_unblock_restores_hidden_messages(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    """Unblocking must restore the exact stored row, not a placeholder or a cached hide."""
    alice = await make_user("Restore Alice")
    mallory = await make_user("Restore Mallory")
    await share_verified_campus(alice.id, mallory.id)
    device = await register_device(mallory, one_time_prekey_count=1)

    created = await _open_dm(client, mallory, alice)
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]

    sent = await _send(client, conversation_id, mallory, device["id"])
    assert sent.status_code == 201, sent.text
    original_ciphertext = sent.json()["ciphertext_b64"]

    await _block(client, alice, mallory)
    hidden = await client.get(
        f"/conversations/{conversation_id}/messages", headers=alice.headers
    )
    assert hidden.status_code == 200, hidden.text
    assert hidden.json() == []

    unblocked = await client.delete(
        "/moderation/blocks",
        params={"blocked_id": mallory.id},
        headers=alice.headers,
    )
    assert unblocked.status_code == 204, unblocked.text

    restored = await client.get(
        f"/conversations/{conversation_id}/messages", headers=alice.headers
    )
    assert restored.status_code == 200, restored.text
    rows = restored.json()
    assert len(rows) == 1
    assert rows[0]["ciphertext_b64"] == original_ciphertext


@pytest.mark.asyncio
async def test_history_dm_case_full_scenario(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    """The card's explicit DM case, end to end: send, block, refused resend, empty
    history, unblock, restored history — in one sequence."""
    alice = await make_user("Lifecycle Alice")
    mallory = await make_user("Lifecycle Mallory")
    await share_verified_campus(alice.id, mallory.id)
    device = await register_device(mallory, one_time_prekey_count=1)

    created = await _open_dm(client, mallory, alice)
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]

    first = await _send(client, conversation_id, mallory, device["id"])
    assert first.status_code == 201, first.text

    await _block(client, alice, mallory)

    refused = await _send(client, conversation_id, mallory, device["id"])
    assert refused.status_code == 403, refused.text

    after_block = await client.get(
        f"/conversations/{conversation_id}/messages", headers=alice.headers
    )
    assert after_block.status_code == 200, after_block.text
    assert after_block.json() == []

    unblocked = await client.delete(
        "/moderation/blocks",
        params={"blocked_id": mallory.id},
        headers=alice.headers,
    )
    assert unblocked.status_code == 204, unblocked.text

    after_unblock = await client.get(
        f"/conversations/{conversation_id}/messages", headers=alice.headers
    )
    assert after_unblock.status_code == 200, after_unblock.text
    assert len(after_unblock.json()) == 1


@pytest.mark.asyncio
async def test_history_ignores_by_chirp_only_block(
    client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice
) -> None:
    """A by-chirp block must not move a named surface (c279/c342 precedent, now pinned
    for message history too).

    Message history is named: alice already knows mallory is the sender of the DM
    message below. But the FILTER must key off UserBlock.source == 'named' only, not off
    the (blocker, blocked) pair regardless of source — matching blockers_of instead would
    let an anonymous by-chirp block (whose target alice never learns) start silently
    hiding one specific named contact's messages, which is a before/after diff that names
    the anonymous chirp's author. This is the one place c348's plan flagged as a judgment
    call rather than an obvious bugfix, and it is exactly the case blockers_of's own
    module docstring warns against widening onto a named surface.
    """
    alice = await make_user("ByChirp Alice")
    mallory = await make_user("ByChirp Mallory")
    campus_id = await share_verified_campus(alice.id, mallory.id)
    device = await register_device(mallory, one_time_prekey_count=1)

    created = await _open_dm(client, mallory, alice)
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]

    sent = await _send(client, conversation_id, mallory, device["id"])
    assert sent.status_code == 201, sent.text

    chirp = await client.post(
        f"/campuses/{campus_id}/chirps", json={"body": "anon"}, headers=mallory.headers
    )
    assert chirp.status_code == 201, chirp.text
    chirp_id = chirp.json()["id"]

    by_chirp_block = await client.post(
        f"/moderation/blocks/by-chirp/{chirp_id}", headers=alice.headers
    )
    assert by_chirp_block.status_code == 204, by_chirp_block.text

    history = await client.get(
        f"/conversations/{conversation_id}/messages", headers=alice.headers
    )
    assert history.status_code == 200, history.text
    assert len(history.json()) == 1
