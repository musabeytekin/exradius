"""Shared FastAPI dependencies."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from exradius.config import Settings
from exradius.crypto import SecretBox
from exradius.db import SessionFactory
from exradius.models import AdminUser
from exradius.secret_backends import SecretBackends
from exradius.storage import ObjectStore


@dataclass
class AppState:
    settings: Settings
    sessions: SessionFactory
    store: ObjectStore
    box: SecretBox
    broker: Any  # BrokerPublisher
    rereads: Any = None  # hooks.Rereads, set at startup
    router: Any = None  # routing.Router; None: one for settings.openai_base_url
    egress_auth: Any = None  # egress.EgressAuth; None: one with the in-cluster TokenReview
    backends: SecretBackends | None = None
    """Where stores' values live; made from ``box`` when not given."""

    def __post_init__(self) -> None:
        if self.backends is None:
            self.backends = SecretBackends.from_settings(self.box, self.settings)

    @property
    def secrets(self) -> SecretBackends:
        assert self.backends is not None
        return self.backends


def app_state(request: Request) -> AppState:
    return request.app.state.exradius


async def db(request: Request) -> AsyncIterator[AsyncSession]:
    async with app_state(request).sessions() as session:
        yield session


DB = Annotated[AsyncSession, Depends(db)]
State = Annotated[AppState, Depends(app_state)]


async def require_admin(request: Request, session: DB) -> AdminUser:
    admin_id = request.session.get("admin_id")
    if admin_id:
        admin = await session.get(AdminUser, uuid.UUID(admin_id))
        if admin is not None:
            return admin
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "login required")


Admin = Annotated[AdminUser, Depends(require_admin)]


async def get_or_404[T](session: AsyncSession, model: type[T], key: Any) -> T:
    obj = await session.get(model, key)
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{model.__name__} not found")
    return obj


def apply_update(obj: Any, update: Any) -> None:
    for key, value in update.model_dump(exclude_unset=True).items():
        setattr(obj, key, value)
