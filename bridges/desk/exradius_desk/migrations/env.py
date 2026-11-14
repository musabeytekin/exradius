"""Alembic environment: Desk's own database, from the URL ``db.migrate`` sets."""

import asyncio

from alembic import context
from sqlalchemy.engine import Connection

from exradius_desk.models import Base, make_engine

config = context.config
target_metadata = Base.metadata


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True,
                      render_as_batch=connection.dialect.name == "sqlite")
    with context.begin_transaction():
        context.run_migrations()


async def _online() -> None:
    engine = make_engine(config.get_main_option("sqlalchemy.url"))
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


asyncio.run(_online())
