"""Board c403: channel-scoped drain waits and a fail-safe kill-target identifier.

Proves three things about the helpers test_ws_resource_integration.py now uses in
place of the deleted server-wide no_subscribers(broker) and the deleted
client_list/len==1/CLIENT KILL block:

1. A foreign Redis pubsub client held open on an unrelated channel times out the
   old, server-wide wait but does not affect the new, channel-scoped one.
2. The new scoped wait refuses to pass vacuously on a channel that never had a
   subscriber -- it fails loudly instead of returning immediately.
3. The new kill-target identifier refuses to guess when more than one new pubsub
   client appears between its before/after snapshots, naming every candidate
   instead of picking one to kill.

Fixtures are imported from test_ws_resource_integration.py rather than redefined;
pytest resolves fixtures by name in a test module's own namespace, so an import
is sufficient for discovery. conftest.py is intentionally untouched.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from redis.asyncio import Redis

from loadtest.ws_resource_probe import redis_totals, require_loopback
from tests.test_ws_resource_integration import (
    broker, needs_redis, new_pubsub_client_id, no_own_subscribers,
    own_channel_subscribed, own_channel_subscribers,
)


def _redis_url() -> str:
    return require_loopback(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))


async def _deleted_server_wide_wait(broker):
    """Reconstruction of the no_subscribers(broker) helper board c403 removed from
    test_ws_resource_integration.py, kept ONLY here to demonstrate what was wrong
    with it: it loops on the pubsub client count for the WHOLE Redis instance, so
    it cannot distinguish this test's own sockets from anyone else's.
    """
    async with asyncio.timeout(3):
        while (await redis_totals(broker))["redis_pubsub_clients"]:
            await asyncio.sleep(.01)


@needs_redis
async def test_foreign_pubsub_client_times_out_server_wide_wait_not_scoped(broker):
    """A client subscribed to an unrelated channel, held past the 3s ceiling,
    times out the old server-wide wait but leaves the new scoped wait untouched.
    """
    # This test is the one place in the suite that genuinely needs an EXCLUSIVE
    # Redis, because its control (the last line) asserts that with the foreign
    # client released the server-wide wait succeeds -- which is only true if
    # nothing else on the server holds a pubsub client. Another session's suite
    # running right now would make that control time out and turn this red on
    # correct code, i.e. exactly the flake class c403 removes. So it checks the
    # precondition and skips saying why, rather than becoming the next flake.
    # Deliberately NOT loosened instead: a control that tolerates foreign clients
    # would no longer attribute the timeout above to the foreign client, which is
    # the only thing it is for.
    #
    # AND DELIBERATELY NOT PRE-DECLARED TO c402's SKIP GUARD. If this skip ever
    # fires in CI, the run goes RED as an undeclared skip (conftest's
    # CHIRP_MAX_SKIPS / CHIRP_ALLOWED_SKIP_PREFIXES check), because ci.yml names
    # only the c364 and c400 harnesses and budgets 12 skips, 6 each, so the WS
    # files contribute none there today. That red is the correct outcome and the
    # reason this guard is worth having: it would mean something else is holding a
    # pubsub client on CI's Redis, which CI is supposed to keep exclusive, and that
    # is a real environmental fact worth shouting about. The WRONG response is to
    # add this file to CHIRP_ALLOWED_SKIP_PREFIXES, which would convert one loud,
    # specific alarm into permanent silence - the exact trade c402 exists to
    # refuse. The right response is to find out what is subscribed in CI and why.
    # (Ruled by c402's author, arithmetic verified against ci.yml, board c403.)
    already = len([row for row in await broker.client_list() if "P" in row["flags"]])
    if already:
        pytest.skip(
            f"another pubsub client is already connected ({already}); this test's "
            "control needs an exclusive Redis and cannot be established here"
        )

    user_id = f"c403-{uuid.uuid4().hex}"
    foreign = Redis.from_url(_redis_url(), decode_responses=True, socket_connect_timeout=1)
    foreign_pubsub = foreign.pubsub()
    await foreign_pubsub.subscribe("c403-unrelated-channel")
    try:
        # Establish and drain a real subscriber on this test's own channel first,
        # so the scoped wait below is a genuine drain, not a vacuous no-op.
        own = broker.pubsub()
        await own.subscribe(f"user:{user_id}")
        async with asyncio.timeout(2):
            while not await own_channel_subscribers(broker, user_id):
                await asyncio.sleep(.01)
        await own.unsubscribe(f"user:{user_id}")
        await own.aclose()

        # Scoped wait: the foreign client is on a different channel entirely, so
        # this must return promptly, well inside the foreign client's window.
        await asyncio.wait_for(no_own_subscribers(broker, user_id), 1)

        # Server-wide wait: the foreign client is still open and still counts
        # toward redis_pubsub_clients, so this must time out while it is held.
        with pytest.raises(TimeoutError):
            await _deleted_server_wide_wait(broker)
    finally:
        await foreign_pubsub.unsubscribe("c403-unrelated-channel")
        await foreign_pubsub.aclose()
        await foreign.aclose()

    # Falsification / control: released, the SAME server-wide wait now succeeds --
    # attributing the timeout above to the foreign client, not to anything else.
    await asyncio.wait_for(_deleted_server_wide_wait(broker), 2)


@needs_redis
async def test_own_channel_subscribed_refuses_a_channel_that_was_never_subscribed(broker):
    """own_channel_subscribed() must fail loudly on a channel with no live
    subscriber, rather than passing silently as if a real one existed.
    """
    user_id = f"c403-never-{uuid.uuid4().hex}"
    assert await own_channel_subscribers(broker, user_id) == 0

    with pytest.raises(AssertionError, match="vacuously"):
        await own_channel_subscribed(broker, user_id)


@needs_redis
async def test_new_pubsub_client_id_refuses_two_candidates_and_kills_neither(broker):
    """A concurrent suite opening its own pubsub client in the same window as
    ours must make the helper refuse to choose, naming both candidate ids, and
    neither client may be killed as a side effect of that refusal.
    """
    before_ids = {row["id"] for row in await broker.client_list() if "P" in row["flags"]}

    ours = broker.pubsub()
    concurrent = Redis.from_url(_redis_url(), decode_responses=True, socket_connect_timeout=1)
    concurrent_pubsub = concurrent.pubsub()
    try:
        await ours.subscribe(f"user:c403-{uuid.uuid4().hex}")
        await concurrent_pubsub.subscribe("c403-someone-elses-channel")
        async with asyncio.timeout(2):
            while len({row["id"] for row in await broker.client_list() if "P" in row["flags"]} - before_ids) < 2:
                await asyncio.sleep(.01)
        after_ids = {row["id"] for row in await broker.client_list() if "P" in row["flags"]}
        new_ids = after_ids - before_ids
        assert len(new_ids) == 2, "test setup did not actually create two new candidates"

        with pytest.raises(AssertionError) as excinfo:
            await new_pubsub_client_id(broker, before_ids)
        message = str(excinfo.value)
        assert "cannot uniquely identify" in message
        for candidate_id in new_ids:
            assert str(candidate_id) in message

        # Nothing was killed: both clients are still present on the server.
        still_present = {row["id"] for row in await broker.client_list()}
        assert new_ids <= still_present
    finally:
        await ours.unsubscribe()
        await ours.aclose()
        await concurrent_pubsub.unsubscribe("c403-someone-elses-channel")
        await concurrent_pubsub.aclose()
        await concurrent.aclose()
