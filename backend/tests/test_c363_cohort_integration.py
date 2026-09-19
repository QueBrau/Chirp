"""Actual HTTP/Redis/SQL/WS boundaries for the multi-cohort local instrument."""
import asyncio
from collections import Counter
from dataclasses import replace
import json
import socket
from urllib.parse import parse_qsl, urlencode

import pytest
from sqlalchemy import select

from loadtest.cohort_contract import parse_manifest
from loadtest.cohort_fixture import create_fixture
from loadtest.cohort_workload import run_cohorts
from loadtest.message_receipts import Observation
from loadtest.receipt_fixture import FixtureError
from loadtest.ws_resource_probe import local_server
from tests.test_c363_cohort_contract import config
from tests.test_c363_message_receipt_integration import needs_redis, receipt_broker


class ActualBoundary:
    def __init__(self, app, raw, mode="normal"):
        self.app, self.raw, self.mode = app, raw, mode
        self.active = self.peak = 0
        self.calls = []
        self.dropped = 0

    async def __call__(self, scope, receive, send):
        first, second = (cohort["receipt"] for cohort in self.raw["cohorts"])
        if scope["type"] == "websocket":
            uid = dict(scope["headers"])[b"x-debug-firebase-uid"].decode()
            async def ws_send(event):
                if (self.mode == "missing_recipient" and uid == first["message_workload"]["recipient_uids"][1]
                        and event["type"] == "websocket.send" and event.get("text")
                        and json.loads(event["text"]).get("type") == "message"):
                    self.dropped += 1
                    return
                await send(event)
            await self.app(scope, receive, ws_send)
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        self.calls.append((scope["method"], scope["path"], scope["query_string"]))
        self.active += 1
        self.peak = max(self.peak, self.active)
        if self.mode == "cursor_dropped" and scope["path"] == "/conversations":
            params = dict(parse_qsl(scope["query_string"].decode()))
            params.pop("before_id", None)
            scope = scope | {"query_string": urlencode(params).encode()}
        start = None
        ended = False
        foreign_scope = self.mode == "foreign_scope" and scope["path"] == f"/chapters/{first['chapter_id']}/members"
        wrong_audience = self.mode == "wrong_audience" and scope["path"] == f"/campuses/{first['campus_id']}/feed"
        tamper = foreign_scope or wrong_audience

        async def http_send(event):
            nonlocal start, ended
            if tamper and event["type"] == "http.response.start":
                start = event
                return
            if tamper and event["type"] == "http.response.body":
                rows = json.loads(event["body"])
                if foreign_scope:
                    rows[0]["chapter_id"] = second["chapter_id"]
                else:
                    rows[0]["audience"] = "org"
                body = json.dumps(rows).encode()
                headers = [(key, value) for key, value in start["headers"] if key != b"content-length"]
                await send(start | {"headers": headers + [(b"content-length", str(len(body)).encode())]})
                event = event | {"body": body}
            await send(event)
            if event["type"] == "http.response.body" and not event.get("more_body", False):
                self.active -= 1
                ended = True

        try:
            # Make overlapping requests visible if separate cohort limiters ever
            # replace the shared permit; this is not a latency benchmark.
            await asyncio.sleep(.015)
            if self.mode == "redirect":
                await http_send({"type": "http.response.start", "status": 307,
                                 "headers": [(b"location", b"http://remote.invalid:9/private"),
                                             (b"content-length", b"0")]})
                await http_send({"type": "http.response.body", "body": b""})
            else:
                await self.app(scope, receive, http_send)
        finally:
            if not ended:
                self.active -= 1


@pytest.fixture
async def cohort_data(receipt_broker):
    from app.db import get_session_factory
    async with get_session_factory()() as session:
        async with session.begin():
            return await create_fixture(session)


async def observe(raw, mode="normal"):
    from app.main import create_app
    boundary = ActualBoundary(create_app(), raw, mode)
    async with local_server(boundary) as (base, _):
        example = config()
        actual = replace(example, base_url=base.replace("ws://", "http://", 1), ws_url=base + "/ws",
                         duration_seconds=16, caps=replace(example.caps, max_concurrent_requests=1),
                         ws=replace(example.ws, connects_per_second=10),
                         abort=replace(example.abort, read_p95_ceiling_ms=10000, write_p95_ceiling_ms=10000))
        report = await asyncio.wait_for(run_cohorts(actual, parse_manifest(json.dumps(raw)), settle_seconds=1), 40)
    return report, boundary


@needs_redis
async def test_actual_cohorts_cover_cardinality_cursor_ties_shared_pacing_and_receipts(cohort_data, monkeypatch):
    from app import models
    from app.db import get_session_factory

    starts = []
    original = Observation.send_one
    async def capture_dispatch(self, client, previous_start):
        started = await original(self, client, previous_start)
        starts.append(started)
        return started
    monkeypatch.setattr(Observation, "send_one", capture_dispatch)
    report, boundary = await observe(cohort_data)
    assert report["status"] == "LOCAL_COHORT_MIX_OBSERVED", [
        {"errors": cohort["delivery"]["errors"],
         "walks": [member["completed_walks"] for member in cohort["members"]]}
        for cohort in report["cohorts"]
    ]
    assert report["errors"] == []
    assert boundary.peak == 1
    assert len(starts) == 2 and starts[1] - starts[0] >= 4
    assert report["fairness"]["requests"] == len(boundary.calls)
    assert report["budgets"]["aggregate_reference_extra_rps"] == 0
    assert report["driver"]["event_loop_delay_ms"]["samples"] > 0
    assert report["fairness"]["acceptance_proven"] is False
    assert [c["fixture_counts"]["chapter_members"] for c in report["cohorts"]] == [100, 300]
    for cohort in report["cohorts"]:
        assert cohort["read_coverage_complete"] is True
        assert cohort["delivery"]["messages"]["accepted"] == 1
        assert cohort["delivery"]["receipts"]["unique"] == 2
        assert cohort["delivery"]["receipts"]["missing"] == 0
        assert cohort["delivery"]["budgets"]["reference_probe_additional_rps"] == 0
        for member in cohort["members"]:
            assert set(member["completed_walks"]) == {"campus_feed", "chapter_posts", "inbox", "history", "roster", "hot_inbox"}
            assert member["routes"]["hot_inbox"] >= 3
            assert member["route_stats"]["inbox"]["client_response_ms"]["samples"] >= 2
    assert any(b"before_id=" in query for _, path, query in boundary.calls if path == "/conversations")
    assert all(path != "/auth/me" for _, path, _ in boundary.calls)
    all_seeded = {identifier for c in cohort_data["cohorts"] for identifier in c["dataset"]["message_ids"]}
    async with get_session_factory()() as session:
        stored = (await session.execute(select(models.Message))).scalars().all()
        new = [message for message in stored if str(message.id) not in all_seeded]
        assert len(new) == 2
        assert Counter(str(message.conversation_id) for message in new) == Counter(
            c["receipt"]["message_workload"]["conversation_id"] for c in cohort_data["cohorts"])
        with pytest.raises(FixtureError, match="database_contains_application_rows"):
            await create_fixture(session)
    aggregate = json.dumps(report)
    for c in cohort_data["cohorts"]:
        receipt = c["receipt"]
        private = [receipt["campus_id"], receipt["chapter_id"],
                   receipt["message_workload"]["sender_device_id"],
                   *(value for user in receipt["users"] for value in user.values()),
                   *(identifier for key, values in c["dataset"].items() if key != "history_before" for identifier in values)]
        assert all(value not in aggregate for value in private)
    assert all(str(message.id) not in aggregate for message in new)
    assert {"photos", "polls", "dues", "fairness_acceptance", "production_capacity"} <= set(report["not_proven"])


@needs_redis
@pytest.mark.parametrize("mode,code", [
    ("foreign_scope", "cohort_roster_mismatch"),
    ("wrong_audience", "cohort_response_identity_mismatch"),
    ("cursor_dropped", "cohort_pagination_incomplete"),
    ("missing_recipient", "receipts_missing"),
    ("redirect", "cohort_read_not_200"),
])
async def test_actual_boundary_failures_refuse_success(cohort_data, monkeypatch, mode, code):
    original = socket.getaddrinfo
    dns = []
    def guarded_dns(host, *args, **kwargs):
        dns.append(host)
        assert host in {"localhost", "127.0.0.1", "::1", b"localhost", b"127.0.0.1", b"::1"}
        return original(host, *args, **kwargs)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_dns)
    report, boundary = await observe(cohort_data, mode)
    assert report["status"] == "NOT_PROVEN"
    assert any(code in cohort["delivery"]["errors"] for cohort in report["cohorts"]), [
        cohort["delivery"]["errors"] for cohort in report["cohorts"]
    ]
    if mode == "missing_recipient":
        assert boundary.dropped == 1
        assert sum(c["delivery"]["receipts"]["expected"] for c in report["cohorts"]) == 4
        assert sum(c["delivery"]["receipts"]["unique"] for c in report["cohorts"]) == 3
        assert sum(c["delivery"]["receipts"]["missing"] for c in report["cohorts"]) == 1
    if mode == "redirect":
        assert "remote.invalid" not in dns
        assert not any(path == "/private" for _, path, _ in boundary.calls)


async def test_fixture_refuses_rootless_application_rows_without_writing_any_cohort(client):
    from app import models
    from app.db import get_session_factory
    async with get_session_factory()() as session:
        session.add(models.ProcessedStripeEvent(event_id="local-fixture-occupied", event_type="synthetic"))
        await session.commit()
        with pytest.raises(FixtureError, match="database_contains_application_rows"):
            await create_fixture(session)
        assert (await session.execute(select(models.User))).first() is None
        assert (await session.execute(select(models.Campus))).first() is None
        assert (await session.execute(select(models.ProcessedStripeEvent.event_id))).scalar_one() == "local-fixture-occupied"
