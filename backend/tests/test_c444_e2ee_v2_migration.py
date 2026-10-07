"""Migration 0042 (E2EE v2 backend) upgrades, constrains and downgrades correctly (board c444).

Same own-scratch-database, manual-alembic-stepping harness as test_c356_outbox_migration.py:
the shared session-scoped `migrated_db` fixture always migrates straight to head, so it
cannot observe the pre-0042 shape or a legacy row surviving the step.

WHAT THIS PINS
  * a legacy device, one-time prekey and v1 message written BEFORE 0042 come out of the
    upgrade byte-identical, with every v2 column NULL (legacy rows are "left alone");
  * the CHECK constraints reject malformed v2 rows and keep the two shapes exclusive;
  * the partial unique indexes carry the v2 invariants (one fallback per device, no key id
    reuse, no identity reuse) without touching legacy rows;
  * a downgrade with only legacy data restores the old shape and loses nothing;
  * a downgrade with ANY v2 data refuses before changing anything.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_c344_dm_key_migration import (
    _admin_execute,
    _alembic,
    _database_of,
    _probe,
    _seed_dm,
    _seed_user,
    _swap_database,
)
from tests.test_c356_outbox_migration import _drop_db_with_retry

IDENTITY = b"\x11" * 32
ED = b"\x22" * 32
SIG = b"\x33" * 64


@pytest.fixture
def scratch_db() -> Iterator[str]:
    """A throwaway database migrated to 0039 (the revision 0042 sits on)."""
    requested = os.environ.get(
        "TEST_DATABASE_URL", "postgresql+asyncpg://chirp:chirp@localhost:5432/chirp_test"
    )
    base = _database_of(requested)
    admin_url = _swap_database(requested, "postgres")
    db_name = f"{base}_c444mig_{uuid.uuid4().hex[:8]}"
    url = _swap_database(requested, db_name)
    try:
        asyncio.run(_probe(admin_url))
    except Exception:
        pytest.skip("postgres not available — docker compose up db")

    original = os.environ.get("DATABASE_URL")
    asyncio.run(
        _admin_execute(
            admin_url,
            [
                f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)',
                f"CREATE DATABASE \"{db_name}\" ENCODING 'UTF8' TEMPLATE template0",
            ],
        )
    )
    try:
        _alembic(url, "0039")
        yield url
    finally:
        if original is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original
        from app.config import get_settings

        get_settings.cache_clear()
        asyncio.run(_drop_db_with_retry(admin_url, db_name))


async def _execute(url: str, sql: str, params: dict[str, Any] | None = None) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(sql), params or {})
    finally:
        await engine.dispose()


async def _fetch(url: str, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return [dict(row) for row in (await conn.execute(text(sql), params or {})).mappings()]
    finally:
        await engine.dispose()


async def _columns(url: str, table: str) -> dict[str, dict[str, Any]]:
    rows = await _fetch(
        url,
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = :t",
        {"t": table},
    )
    return {row["column_name"]: row for row in rows}


async def _seed_legacy(url: str) -> dict[str, str]:
    """A legacy device with a one-time prekey, and a v1 message from it, all pre-0042."""
    ids = {name: str(uuid.uuid4()) for name in ("u1", "u2", "device", "otk", "dm", "message")}
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await _seed_user(conn, ids["u1"], "U1")
            await _seed_user(conn, ids["u2"], "U2")
            await conn.execute(
                text(
                    "INSERT INTO devices (id, user_id, registration_id, identity_key) "
                    "VALUES (:id, :u, 4242, :ik)"
                ),
                {"id": ids["device"], "u": ids["u1"], "ik": IDENTITY},
            )
            await conn.execute(
                text(
                    "INSERT INTO one_time_prekeys (id, device_id, key_id, public_key) "
                    "VALUES (:id, :d, 7, :pk)"
                ),
                {"id": ids["otk"], "d": ids["device"], "pk": b"\x44" * 32},
            )
            await _seed_dm(conn, ids["dm"], [ids["u1"], ids["u2"]], datetime.now(timezone.utc))
            await conn.execute(
                text(
                    "INSERT INTO messages (id, conversation_id, sender_device_id, ciphertext) "
                    "VALUES (:id, :c, :d, :ct)"
                ),
                {"id": ids["message"], "c": ids["dm"], "d": ids["device"], "ct": b"v1-bytes"},
            )
    finally:
        await engine.dispose()
    return ids


def _v2_device_sql(
    device_id: str, user_id: str, *, generation: int = 50, **override: Any
) -> tuple[str, dict[str, Any]]:
    """An INSERT for a v2 device. Identities are random per call unless overridden, so a
    test that expects ONE constraint to fire is not accidentally tripping another."""
    params: dict[str, Any] = {
        "id": device_id, "u": user_id, "ik": os.urandom(32), "ed": os.urandom(32),
        "sig": SIG, "gen": generation,
    }
    params.update(override)
    return (
        "INSERT INTO devices (id, user_id, identity_key, crypto_suite, identity_ed25519, "
        "generation, binding_signature, approved_at) "
        "VALUES (:id, :u, :ik, 'vodozemac-olm-v1', :ed, :gen, :sig, now())",
        params,
    )


def _violation(url: str, sql: str, params: dict[str, Any], constraint: str) -> None:
    with pytest.raises(DBAPIError, match=constraint):
        asyncio.run(_execute(url, sql, params))


def test_upgrade_leaves_legacy_rows_alone_and_enforces_v2_shape(scratch_db: str) -> None:
    url = scratch_db
    legacy = asyncio.run(_seed_legacy(url))
    before_columns = asyncio.run(_columns(url, "messages"))
    assert before_columns["ciphertext"]["is_nullable"] == "NO"

    _alembic(url, "0042")

    # ---- legacy rows untouched, every v2 column NULL, kind defaulted ----
    (device,) = asyncio.run(_fetch(url, "SELECT * FROM devices WHERE id = :id", {"id": legacy["device"]}))
    assert device["registration_id"] == 4242 and bytes(device["identity_key"]) == IDENTITY
    for column in (
        "crypto_suite", "identity_ed25519", "generation", "binding_signature",
        "approved_at", "approved_by_device_id", "approval_signature",
    ):
        assert device[column] is None, column
    (otk,) = asyncio.run(_fetch(url, "SELECT * FROM one_time_prekeys WHERE id = :id", {"id": legacy["otk"]}))
    assert otk["key_id"] == 7 and otk["kind"] == "one_time" and otk["signature"] is None
    (message,) = asyncio.run(_fetch(url, "SELECT * FROM messages WHERE id = :id", {"id": legacy["message"]}))
    assert bytes(message["ciphertext"]) == b"v1-bytes"
    assert message["client_message_id"] is None and message["envelope_version"] is None

    # ---- column types and nullability ----
    assert asyncio.run(_columns(url, "messages"))["ciphertext"]["is_nullable"] == "YES"
    assert asyncio.run(_columns(url, "devices"))["registration_id"]["is_nullable"] == "YES"
    assert asyncio.run(_columns(url, "devices"))["generation"]["data_type"] == "bigint"
    assert asyncio.run(_columns(url, "one_time_prekeys"))["key_id"]["data_type"] == "bigint"
    assert "message_legs" in {
        row["tablename"] for row in asyncio.run(_fetch(url, "SELECT tablename FROM pg_tables"))
    }

    # ---- devices: v2 shape and legacy exclusivity ----
    u1 = legacy["u1"]
    first_id = str(uuid.uuid4())
    sql, first = _v2_device_sql(first_id, u1, generation=1)
    asyncio.run(_execute(url, sql, first))  # a well-formed v2 row is accepted
    for override in (
        {"ed": b"\x66" * 31},  # short ed25519
        {"ik": b"\x55" * 31},  # short curve25519
        {"sig": b"\x33" * 63},  # short binding signature
        {"gen": 0},  # generation below 1
        {"gen": 2**32},  # generation above u32
        {"ed": None},  # missing ed25519
        {"sig": None},  # missing binding signature
    ):
        sql, params = _v2_device_sql(str(uuid.uuid4()), u1, **override)
        _violation(url, sql, params, "ck_devices_v2_shape")

    _violation(
        url,
        "INSERT INTO devices (id, user_id, registration_id, identity_key, identity_ed25519) "
        "VALUES (:id, :u, 1, :ik, :ed)",
        {"id": str(uuid.uuid4()), "u": u1, "ik": os.urandom(32), "ed": ED},
        "ck_devices_legacy_has_no_v2_fields",
    )
    _violation(
        url,
        "INSERT INTO devices (id, user_id, identity_key) VALUES (:id, :u, :ik)",
        {"id": str(uuid.uuid4()), "u": u1, "ik": os.urandom(32)},
        "ck_devices_registration_id",
    )
    # Approval shape: self-approval, and a signature with no approver.
    _violation(  # approver without a signature
        url, "UPDATE devices SET approved_by_device_id = :d WHERE id = :d", {"d": first_id},
        "ck_devices_approval_shape",
    )
    _violation(  # a device approving itself, even with a well-formed signature
        url,
        "UPDATE devices SET approved_by_device_id = :d, approval_signature = :s WHERE id = :d",
        {"d": first_id, "s": SIG}, "ck_devices_approval_shape",
    )
    _violation(
        url, "UPDATE devices SET approval_signature = :s WHERE id = :d",
        {"d": first_id, "s": SIG}, "ck_devices_approval_shape",
    )
    # v2 identity and generation uniqueness (partial: legacy rows never collide).
    sql, params = _v2_device_sql(str(uuid.uuid4()), legacy["u2"], ik=first["ik"])
    _violation(url, sql, params, "uq_devices_v2_identity_curve25519")
    sql, params = _v2_device_sql(str(uuid.uuid4()), legacy["u2"], ed=first["ed"])
    _violation(url, sql, params, "uq_devices_v2_identity_ed25519")
    sql, params = _v2_device_sql(str(uuid.uuid4()), u1, generation=1)
    _violation(url, sql, params, "uq_devices_v2_user_generation")

    # ---- one_time_prekeys: kind, signature and fallback rules ----
    v2_device = first_id
    otk_sql = (
        "INSERT INTO one_time_prekeys (device_id, key_id, public_key, kind, signature) "
        "VALUES (:d, :k, :pk, :kind, :sig)"
    )
    base = {"d": v2_device, "k": 10, "pk": b"\x01" * 32, "kind": "one_time", "sig": SIG}
    asyncio.run(_execute(url, otk_sql, base))
    _violation(url, otk_sql, {**base, "kind": "bogus"}, "ck_otk_kind")
    _violation(url, otk_sql, {**base, "k": 11, "sig": None, "kind": "fallback"}, "ck_otk_fallback_signed")
    _violation(url, otk_sql, {**base, "k": 12, "sig": b"\x33" * 63}, "ck_otk_v2_shape")
    _violation(url, otk_sql, {**base, "k": -1}, "ck_otk_v2_shape")
    _violation(url, otk_sql, {**base, "k": 10}, "uq_otk_v2_device_key_id")  # key id reuse
    asyncio.run(_execute(url, otk_sql, {**base, "k": 2**62, "kind": "fallback"}))  # BIGINT id, one fallback
    _violation(url, otk_sql, {**base, "k": 13, "kind": "fallback"}, "uq_otk_v2_one_fallback_per_device")
    # A legacy row (no signature) may still reuse a key id: the unique index ignores it.
    asyncio.run(
        _execute(
            url,
            "INSERT INTO one_time_prekeys (device_id, key_id, public_key) VALUES (:d, 7, :pk)",
            {"d": legacy["device"], "pk": b"\x02" * 32},
        )
    )

    # ---- messages: legacy needs ciphertext; v2 needs an id and forbids ciphertext ----
    msg_sql = (
        "INSERT INTO messages (conversation_id, sender_device_id, ciphertext, client_message_id, "
        "envelope_version) VALUES (:c, :d, :ct, :cmid, :ev)"
    )
    msg = {"c": legacy["dm"], "d": legacy["device"], "ct": None, "cmid": None, "ev": None}
    _violation(url, msg_sql, msg, "ck_messages_ciphertext_or_envelope")
    _violation(url, msg_sql, {**msg, "ct": b"x", "cmid": str(uuid.uuid4()), "ev": 1}, "ck_messages_v2_no_ciphertext")
    _violation(url, msg_sql, {**msg, "ev": 1}, "ck_messages_v2_client_message_id")
    cmid = str(uuid.uuid4())
    asyncio.run(_execute(url, msg_sql, {**msg, "cmid": cmid, "ev": 1}))
    _violation(url, msg_sql, {**msg, "cmid": cmid, "ev": 1}, "uq_messages_sender_device_client_message_id")

    # ---- message_legs: olm_type CHECK, composite key, cascade with the message ----
    (v2_message,) = asyncio.run(
        _fetch(url, "SELECT id FROM messages WHERE client_message_id = :c", {"c": cmid})
    )
    leg_sql = (
        "INSERT INTO message_legs (message_id, recipient_device_id, olm_type, ciphertext) "
        "VALUES (:m, :d, :t, :ct)"
    )
    leg = {"m": v2_message["id"], "d": v2_device, "t": 0, "ct": b"leg"}
    asyncio.run(_execute(url, leg_sql, leg))
    _violation(url, leg_sql, {**leg, "t": 2}, "ck_message_legs_olm_type")
    _violation(url, leg_sql, leg, "message_legs_pkey")
    asyncio.run(_execute(url, "DELETE FROM messages WHERE id = :m", {"m": v2_message["id"]}))
    assert asyncio.run(_fetch(url, "SELECT 1 FROM message_legs")) == [], "legs must cascade"


def test_downgrade_with_only_legacy_data_restores_the_old_shape(scratch_db: str) -> None:
    url = scratch_db
    legacy = asyncio.run(_seed_legacy(url))
    _alembic(url, "0042")
    # v2 columns exist, but nothing v2 was ever written.
    _alembic(url, "0039", down=True)

    devices = asyncio.run(_columns(url, "devices"))
    assert not {"crypto_suite", "identity_ed25519", "generation", "approved_at"} & devices.keys()
    assert devices["registration_id"]["is_nullable"] == "NO"
    assert asyncio.run(_columns(url, "messages"))["ciphertext"]["is_nullable"] == "NO"
    otk_columns = asyncio.run(_columns(url, "one_time_prekeys"))
    assert otk_columns["key_id"]["data_type"] == "integer"
    assert not {"kind", "signature"} & otk_columns.keys()
    tables = {row["tablename"] for row in asyncio.run(_fetch(url, "SELECT tablename FROM pg_tables"))}
    assert "message_legs" not in tables

    (device,) = asyncio.run(_fetch(url, "SELECT * FROM devices WHERE id = :id", {"id": legacy["device"]}))
    assert device["registration_id"] == 4242 and bytes(device["identity_key"]) == IDENTITY
    (message,) = asyncio.run(_fetch(url, "SELECT * FROM messages WHERE id = :id", {"id": legacy["message"]}))
    assert bytes(message["ciphertext"]) == b"v1-bytes"

    # And it comes back: upgrading again is clean.
    _alembic(url, "0042")
    assert "kind" in asyncio.run(_columns(url, "one_time_prekeys"))


@pytest.mark.parametrize("kind", ["device", "key", "message"])
def test_downgrade_refuses_when_any_v2_data_exists(scratch_db: str, kind: str) -> None:
    url = scratch_db
    legacy = asyncio.run(_seed_legacy(url))
    _alembic(url, "0042")
    if kind == "device":
        sql, params = _v2_device_sql(str(uuid.uuid4()), legacy["u1"], generation=1)
        asyncio.run(_execute(url, sql, params))
    elif kind == "key":
        asyncio.run(
            _execute(
                url,
                "INSERT INTO one_time_prekeys (device_id, key_id, public_key, kind, signature) "
                "VALUES (:d, 1, :pk, 'fallback', :sig)",
                {"d": legacy["device"], "pk": b"\x01" * 32, "sig": SIG},
            )
        )
    else:
        asyncio.run(
            _execute(
                url,
                "INSERT INTO messages (conversation_id, sender_device_id, client_message_id, "
                "envelope_version) VALUES (:c, :d, :cmid, 1)",
                {"c": legacy["dm"], "d": legacy["device"], "cmid": str(uuid.uuid4())},
            )
        )

    with pytest.raises(RuntimeError, match="0042 downgrade refused"):
        _alembic(url, "0039", down=True)

    # Nothing was changed: still at 0042, v2 columns and the legacy rows intact.
    assert asyncio.run(_fetch(url, "SELECT version_num FROM alembic_version")) == [{"version_num": "0042"}]
    assert "generation" in asyncio.run(_columns(url, "devices"))
    assert asyncio.run(_fetch(url, "SELECT 1 FROM messages WHERE id = :id", {"id": legacy["message"]}))
