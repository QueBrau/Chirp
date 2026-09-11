"""Real PostgreSQL index shape and populated 0035 downgrade/upgrade preservation."""
from __future__ import annotations

import runpy
import uuid
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from httpx import AsyncClient
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import MakeUser, RegisterDevice, _padded, b64


_INDEX_COLUMNS = {
    "idx_devices_user_revoked_created": "(user_id, revoked_at, created_at, id)",
    "idx_signed_prekeys_device_created": "(device_id, created_at DESC, id DESC)",
    "idx_otk_device_retained": "(device_id)",
    "idx_kyber_device_kind_created": "(device_id, is_last_resort, created_at DESC, id DESC)",
}


def _index_definitions(connection: Connection) -> dict[str, str]:
    return dict(connection.execute(text(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' "
        "AND tablename IN ('devices', 'signed_prekeys', 'one_time_prekeys', 'kyber_prekeys')"
    )).tuples().all())


def _directory_rows(connection: Connection, device_id: uuid.UUID) -> dict[str, list[dict]]:
    rows = {}
    for table in ("devices", "signed_prekeys", "one_time_prekeys", "kyber_prekeys"):
        key = "id" if table == "devices" else "device_id"
        rows[table] = connection.execute(text(
            f"SELECT row_to_json(t) FROM {table} t WHERE {key} = :id ORDER BY id"
        ), {"id": device_id}).scalars().all()
    return rows


async def test_index_migration_round_trip_preserves_keys_and_available_pool_indexes(
    migrated_db: str, client: AsyncClient, make_user: MakeUser, register_device: RegisterDevice,
) -> None:
    owner = await make_user("Migration Key Owner")
    device = await register_device(owner)
    device_id = uuid.UUID(device["id"])
    response = await client.post(f"/devices/{device_id}/prekeys", headers=owner.headers, json={
        "kyber_last_resort": {
            "key_id": 2,
            "public_key_b64": b64(_padded(b"kyber", 1568)),
            "signature_b64": b64(_padded(b"signature", 64)),
        },
    })
    assert response.status_code == 200, response.text
    migration = runpy.run_path(str(
        Path(__file__).parents[1] / "alembic/versions/0035_key_directory_resource_indexes.py"
    ))

    def verify_round_trip(connection: Connection) -> None:
        before_indexes = _index_definitions(connection)
        for name, columns in _INDEX_COLUMNS.items():
            assert columns in before_indexes[name]
            assert "WHERE" not in before_indexes[name], "retained counts include consumed rows"
        before_rows = _directory_rows(connection, device_id)
        assert all(before_rows.values()), "every indexed table must hold a real key-directory row"
        with Operations.context(MigrationContext.configure(connection)):
            migration["downgrade"]()
            down_indexes = _index_definitions(connection)
            assert not set(_INDEX_COLUMNS).intersection(down_indexes)
            assert {"idx_otk_available", "idx_kyber_otk_available"} <= down_indexes.keys()
            assert _directory_rows(connection, device_id) == before_rows
            migration["upgrade"]()
        assert _index_definitions(connection) == before_indexes
        assert _directory_rows(connection, device_id) == before_rows

    engine = create_async_engine(migrated_db)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(verify_round_trip)
    finally:
        await engine.dispose()
