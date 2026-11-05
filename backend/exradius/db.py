"""Database engine and session factory."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

SessionFactory = async_sessionmaker[AsyncSession]


def make_engine(url: str) -> AsyncEngine:
    kwargs = {} if url.startswith("sqlite") else {"pool_pre_ping": True}
    return create_async_engine(url, **kwargs)


def make_session_factory(engine: AsyncEngine) -> SessionFactory:
    return async_sessionmaker(engine, expire_on_commit=False)
