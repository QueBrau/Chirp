"""Alembic environment: async engine from app settings, metadata from app.db.Base."""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app import models  # noqa: F401  # imported so Base.metadata is fully populated
from app.config import get_settings
from app.db import Base

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers defaults to True, which silently switches OFF every
    # logger that already exists — including all of app.* whenever alembic is invoked
    # in-process rather than as its own command. The test suite does exactly that in
    # conftest's schema fixture, so any application logging after the first migration
    # run vanished, and a test asserting on log output failed with an empty capture
    # and no hint as to why (found writing c87's mailer tests).
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a DB connection)."""
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Configure the context against a live connection and run migrations."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine from settings and run migrations via run_sync."""
    connectable = create_async_engine(get_settings().database_url)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against the configured database."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
