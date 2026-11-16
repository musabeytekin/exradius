"""Migrations (design §4): ``alembic upgrade head`` under a Postgres advisory lock, so two
pods starting at once do not both migrate."""

from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from exradius_desk.models import make_engine

MIGRATION_LOCK = 0x6465736B  # "desk"


def alembic_config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


async def migrate(url: str) -> None:
    engine = make_engine(url)
    postgres = engine.dialect.name == "postgresql"
    try:
        async with engine.connect() as connection:
            if postgres:
                await connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK})
                await connection.commit()
            try:
                # env.py runs its own event loop: in a thread of its own.
                await asyncio.to_thread(command.upgrade, alembic_config(url), "head")
            finally:
                if postgres:
                    await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK})
                    await connection.commit()
    finally:
        await engine.dispose()
