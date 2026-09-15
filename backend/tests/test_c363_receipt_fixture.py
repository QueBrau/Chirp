"""Disposable fixture guards and real route compatibility; no remote requests."""
import base64
import json
import stat
import sys

import pytest
from sqlalchemy import select

from loadtest import receipt_fixture as fixture


@pytest.mark.parametrize("url", [
    "postgresql+asyncpg://chirp:secret@127.0.0.1:5432/chirp_receipts_0123abcd",
    "postgresql+asyncpg://chirp@[::1]:5432/chirp_receipts_0123abcd",
])
def test_accepts_explicit_disposable_loopback_url(url):
    actual = fixture.validate_database_url(url)
    assert actual.host in {"127.0.0.1", "::1"}
    assert actual.database == "chirp_receipts_0123abcd"
    assert actual.port == 5432 and not actual.query


@pytest.mark.parametrize("url", [
    "", "postgresql+asyncpg://127.0.0.1:5432/chirp_test",
    "postgresql+asyncpg://localhost:5432/chirp_receipts_0123abcd",
    "postgresql+asyncpg://127.0.0.1.evil:5432/chirp_receipts_0123abcd",
    "postgresql+asyncpg://localhost@remote:5432/chirp_receipts_0123abcd",
    "postgresql+asyncpg://u:p@remote@127.0.0.1:5432/chirp_receipts_0123abcd",
    "postgresql+asyncpg://remote:5432/chirp_receipts_0123abcd?local=127.0.0.1",
    "postgresql+asyncpg://127.0.0.1:5432/chirp_receipts_0123abcd?host=remote",
    "postgresql+asyncpg://127.0.0.1:5432/chirp_receipts_0123abcd#secret",
    "postgresql+asyncpg://127.0.0.1/chirp_receipts_0123abcd",
    "postgresql+asyncpg://127.0.0.1:99999/chirp_receipts_0123abcd",
    "postgresql+asyncpg://127.0.0.1:5432/chirp_receipts_0123abcd\n",
    "postgresql://127.0.0.1:5432/chirp_receipts_0123abcd",
])
def test_refuses_ambiguous_or_non_disposable_database_without_echoing_url(url):
    with pytest.raises(fixture.FixtureError) as error:
        fixture.validate_database_url(url)
    assert str(error.value) == "requires_explicit_disposable_loopback_database"


@pytest.mark.parametrize("count", [0, 11, True, 1.5])
def test_recipient_bound(count):
    with pytest.raises(fixture.FixtureError):
        fixture.validate_recipient_count(count)


@pytest.mark.parametrize("symlink", [False, True])
def test_existing_output_refused_before_database_write(tmp_path, monkeypatch, symlink):
    target = tmp_path / "existing"
    target.write_text("preserve")
    output = tmp_path / "manifest" if symlink else target
    if symlink:
        output.symlink_to(target)
    async def forbidden(*args):
        pytest.fail("output refusal must precede database work")
    monkeypatch.setattr(fixture, "seed_database", forbidden)
    monkeypatch.setenv("ENV", "local")
    monkeypatch.setenv("AUTH_MODE", "emulated")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://127.0.0.1:5432/chirp_receipts_0123abcd")
    monkeypatch.setattr(sys, "argv", ["receipt_fixture", "--out", str(output)])
    assert fixture.main() == 2
    assert target.read_text() == "preserve"


def test_cli_success_preserves_validated_url_boundary_and_private_output(tmp_path, monkeypatch, capsys):
    url = "postgresql+asyncpg://chirp:private-password@127.0.0.1:5432/chirp_receipts_0123abcd"
    calls = []
    async def local_seed(raw_url, count):
        assert isinstance(raw_url, str)
        assert fixture.validate_database_url(raw_url).host == "127.0.0.1"
        calls.append(count)
        return {"fixture_identity": "private-fixture-id"}
    monkeypatch.setattr(fixture, "seed_database", local_seed)
    monkeypatch.setenv("ENV", "local")
    monkeypatch.setenv("AUTH_MODE", "emulated")
    monkeypatch.setenv("DATABASE_URL", url)
    output = tmp_path / "manifest.json"
    monkeypatch.setattr(sys, "argv", ["receipt_fixture", "--recipients", "3", "--out", str(output)])
    assert fixture.main() == 0 and calls == [3]
    assert json.loads(output.read_text()) == {"fixture_identity": "private-fixture-id"}
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    stdout = capsys.readouterr().out
    assert "private-password" not in stdout and "private-fixture-id" not in stdout


@pytest.mark.asyncio
async def test_fixture_supports_real_message_route_and_refuses_existing_rows(client):
    from app import models
    from app.db import get_session_factory
    async with get_session_factory()() as session:
        async with session.begin():
            manifest = await fixture.create_fixture(session, 2)
    workload = manifest["message_workload"]
    headers = {"X-Debug-Firebase-Uid": workload["sender_uid"]}
    payload = base64.b64encode(b"c363 opaque transport fixture").decode()
    response = await client.post(
        "/conversations/" + workload["conversation_id"] + "/messages",
        headers=headers, json={"sender_device_id": workload["sender_device_id"],
                               "ciphertext_b64": payload},
    )
    assert response.status_code == 201
    assert response.json()["ciphertext_b64"] == payload
    assert response.json()["conversation_id"] == workload["conversation_id"]
    async with get_session_factory()() as session:
        device = (await session.execute(select(models.Device))).scalar_one()
        assert str(device.user_id) == manifest["users"][0]["user_id"]
        members = (await session.execute(select(models.ConversationMember))).scalars().all()
        assert {str(member.user_id) for member in members} == {u["user_id"] for u in manifest["users"]}
        with pytest.raises(fixture.FixtureError, match="database_contains_application_rows"):
            await fixture.create_fixture(session, 1)
        assert len((await session.execute(select(models.User))).scalars().all()) == 3
