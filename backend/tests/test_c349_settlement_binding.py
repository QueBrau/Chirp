"""Real signed Stripe payloads against PostgreSQL-owned settlement authority."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import logging
import time
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.config import get_settings
from app.db import get_session_factory
from tests.test_payments import (
    _create_dues_cycle, _onboard, _reserved_intent_id, _succeeded_event,
    stripe_calls, stripe_env,
)


async def signed_post(client, event, *, secret="whsec_fake"):
    payload = json.dumps(event, separators=(",", ":")).encode()
    stamp = str(int(time.time()))
    digest = hmac.new(secret.encode(), stamp.encode() + b"." + payload, hashlib.sha256).hexdigest()
    return await client.post("/webhooks/stripe", content=payload,
                             headers={"Stripe-Signature": f"t={stamp},v1={digest}"})


async def rows(sql, params=None):
    async with get_session_factory()() as session:
        result = await session.execute(text(sql), params or {})
        return result.mappings().all()


@pytest.fixture
async def payment(client, make_chapter_with, stripe_env, stripe_calls, request):
    setup = await make_chapter_with(role="member")
    await _onboard(client, setup)
    cycle_id = await _create_dues_cycle(client, setup, amount_cents=12_345)
    response = await client.post(f"/payments/dues/{cycle_id}/intent",
                                 json={"rail": getattr(request, "param", "card")}, headers=setup.member.headers)
    assert response.status_code == 200, response.text
    intent_id = await _reserved_intent_id(cycle_id, setup.member.id)
    assert intent_id is not None
    event = await _succeeded_event(cycle_id, setup.member.id, intent_id, "evt_binding")
    assert (await rows("SELECT status FROM dues_payment_intents")) == [{"status": "open"}]
    return event


@pytest.mark.parametrize("field,value,reason", [
    ("account", "acct_other", "account_mismatch"),
    ("account", None, "account_mismatch"),
    ("livemode", True, "livemode_mismatch"),
    ("livemode", 0, "livemode_mismatch"),
    ("intent.livemode", True, "livemode_mismatch"),
    ("intent.amount", 100, "amount_mismatch"),
    ("intent.amount_received", 100, "amount_mismatch"),
    ("intent.amount_received", "12345", "amount_mismatch"),
    ("intent.amount_received", None, "amount_mismatch"),
    ("intent.currency", "eur", "currency_mismatch"),
    ("metadata.chirp_user_id", str(uuid.uuid4()), "metadata_mismatch"),
    ("metadata.chirp_user_id", "private@example.edu", "metadata_mismatch"),
    ("metadata.chirp_dues_cycle_id", str(uuid.uuid4()), "metadata_mismatch"),
    ("metadata.chirp_chapter_id", str(uuid.uuid4()), "metadata_mismatch"),
    ("metadata.chirp_rail", "ach", "metadata_mismatch"),
    ("intent.id", "pi_unknown", "no_reservation"),
])
async def test_signed_mismatch_quarantines_once_without_money_or_telemetry(
    client, payment, caplog, field, value, reason,
):
    event = copy.deepcopy(payment)
    intent = event["data"]["object"]
    target = intent["metadata"] if field.startswith("metadata.") else intent if field.startswith("intent.") else event
    target[field.rsplit(".", 1)[-1]] = value
    intent["description"] = "PRIVATE DESCRIPTION private@example.edu"
    intent["billing_details"] = {"email": "private@example.edu"}
    intent["metadata"]["free_text"] = "PRIVATE DESCRIPTION"
    caplog.clear()
    with caplog.at_level(logging.INFO):
        first = await signed_post(client, event)
        second = await signed_post(client, event)
    assert first.status_code == second.status_code == 200
    assert await rows("SELECT id FROM ledger_entries") == []
    assert await rows("SELECT status FROM dues_payment_intents") == [{"status": "open"}]
    quarantine = await rows("SELECT reason, expected, observed FROM stripe_settlement_quarantine")
    assert len(quarantine) == 1 and quarantine[0]["reason"] == reason
    assert len(await rows("SELECT event_id FROM processed_stripe_events")) == 1
    evidence = json.dumps([dict(row) for row in quarantine])
    assert "private@example.edu" not in evidence and "PRIVATE DESCRIPTION" not in evidence
    assert not any(record.name == "app.analytics" for record in caplog.records)
    assert "private@example.edu" not in caplog.text


async def test_signed_success_uses_snapshot_even_if_cycle_changes(client, payment, caplog):
    async with get_session_factory()() as session:
        await session.execute(text("UPDATE dues_cycles SET amount_cents=54321"))
        await session.commit()
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.analytics"):
        responses = await asyncio.gather(signed_post(client, payment), signed_post(client, payment),
                                         signed_post(client, dict(payment, id="evt_concurrent_id")))
        other_event = dict(payment, id="evt_second_id")
        responses.append(await signed_post(client, other_event))
    assert all(response.status_code == 200 for response in responses)
    assert await rows("SELECT amount_cents FROM ledger_entries") == [{"amount_cents": 12_345}]
    assert await rows("SELECT status FROM dues_payment_intents") == [{"status": "succeeded"}]
    assert await rows("SELECT id FROM stripe_settlement_quarantine") == []
    assert len([record for record in caplog.records if record.name == "app.analytics"]) == 1


@pytest.mark.parametrize("event_type", ["payment_intent.payment_failed", "payment_intent.canceled"])
async def test_signed_failure_requires_account_and_mode_and_never_demotes_success(client, payment, event_type):
    invalid = dict(payment, id="evt_wrong_account", type=event_type, account="acct_other")
    assert (await signed_post(client, invalid)).status_code == 200
    assert await rows("SELECT status FROM dues_payment_intents") == [{"status": "open"}]
    assert (await signed_post(client, payment)).status_code == 200
    late = dict(payment, id="evt_late", type=event_type)
    assert (await signed_post(client, late)).status_code == 200
    assert await rows("SELECT status FROM dues_payment_intents") == [{"status": "succeeded"}]


async def test_unknown_key_mode_fails_closed(client, payment, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "opaque_proxy_key")
    get_settings.cache_clear()
    try:
        assert (await signed_post(client, payment)).status_code == 200
        assert await rows("SELECT reason FROM stripe_settlement_quarantine") == [{"reason": "livemode_unverifiable"}]
        assert await rows("SELECT status FROM dues_payment_intents") == [{"status": "open"}]
    finally:
        get_settings.cache_clear()


async def test_bad_signature_cannot_write_receipt_or_quarantine(client, payment):
    assert (await signed_post(client, payment, secret="wrong_secret")).status_code == 400
    assert await rows("SELECT event_id FROM processed_stripe_events") == []
    assert await rows("SELECT id FROM stripe_settlement_quarantine") == []


async def test_failed_ledger_insert_rolls_back_receipt_status_and_analytics(client, payment, caplog):
    async with get_session_factory()() as session:
        await session.execute(text("CREATE FUNCTION c349_reject_ledger() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'test ledger write failure' USING ERRCODE='23514'; END $$"))
        await session.execute(text("CREATE TRIGGER c349_reject_ledger BEFORE INSERT ON ledger_entries FOR EACH ROW EXECUTE FUNCTION c349_reject_ledger()"))
        await session.commit()
    caplog.clear()
    try:
        with caplog.at_level(logging.INFO, logger="app.analytics"), pytest.raises(DBAPIError):
            await signed_post(client, payment)
        assert await rows("SELECT event_id FROM processed_stripe_events") == []
        assert await rows("SELECT status FROM dues_payment_intents") == [{"status": "open"}]
        assert await rows("SELECT id FROM ledger_entries") == []
        assert not any(record.name == "app.analytics" for record in caplog.records)
    finally:
        async with get_session_factory()() as session:
            await session.execute(text("DROP TRIGGER c349_reject_ledger ON ledger_entries"))
            await session.execute(text("DROP FUNCTION c349_reject_ledger()"))
            await session.commit()
    assert (await signed_post(client, payment)).status_code == 200
    assert await rows("SELECT status FROM dues_payment_intents") == [{"status": "succeeded"}]
    assert len(await rows("SELECT event_id FROM processed_stripe_events")) == 1
    assert len(await rows("SELECT id FROM ledger_entries")) == 1


async def test_failed_quarantine_insert_rolls_back_receipt_and_allows_retry(client, payment):
    async with get_session_factory()() as session:
        await session.execute(text("CREATE FUNCTION c349_reject_insert() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'test diagnostic write failure' USING ERRCODE='23514'; END $$"))
        await session.execute(text("CREATE TRIGGER c349_reject_insert BEFORE INSERT ON stripe_settlement_quarantine FOR EACH ROW EXECUTE FUNCTION c349_reject_insert()"))
        await session.commit()
    invalid = dict(payment, account="acct_other")
    try:
        # ASGITransport raises server failures. A swallowed IntegrityError would
        # return 200 here and permanently lose the receipt's diagnostic evidence.
        with pytest.raises(DBAPIError):
            await signed_post(client, invalid)
        assert await rows("SELECT event_id FROM processed_stripe_events") == []
        assert await rows("SELECT status FROM dues_payment_intents") == [{"status": "open"}]
    finally:
        async with get_session_factory()() as session:
            await session.execute(text("DROP TRIGGER c349_reject_insert ON stripe_settlement_quarantine"))
            await session.execute(text("DROP FUNCTION c349_reject_insert()"))
            await session.commit()
    assert (await signed_post(client, invalid)).status_code == 200
    assert len(await rows("SELECT event_id FROM processed_stripe_events")) == 1
    assert await rows("SELECT reason FROM stripe_settlement_quarantine") == [{"reason": "account_mismatch"}]


@pytest.mark.parametrize("key,livemode", [("sk_live_fake", True), ("rk_live_fake", True), ("rk_test_fake", False)])
@pytest.mark.parametrize("payment", ["ach"], indirect=True)
async def test_signed_ach_settlement_in_configured_live_or_test_mode(client, payment, monkeypatch, key, livemode):
    monkeypatch.setenv("STRIPE_SECRET_KEY", key)
    get_settings.cache_clear()
    payment["livemode"] = payment["data"]["object"]["livemode"] = livemode
    try:
        assert (await signed_post(client, payment)).status_code == 200
        assert await rows("SELECT status, rail FROM dues_payment_intents") == [{"status": "succeeded", "rail": "ach"}]
        assert await rows("SELECT amount_cents FROM ledger_entries") == [{"amount_cents": 12345}]
        assert await rows("SELECT id FROM stripe_settlement_quarantine") == []
    finally:
        get_settings.cache_clear()
