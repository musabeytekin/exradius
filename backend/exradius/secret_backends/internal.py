"""The ``internal`` backend: values encrypted with the master key in Exradius's own database
(``secret_store_entries``), set and deleted through the admin API."""

from __future__ import annotations

import uuid
from collections.abc import Collection

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from exradius.crypto import SecretBox
from exradius.models import SecretStoreEntry


class InternalBackend:
    def __init__(self, session: AsyncSession, box: SecretBox | None, store_id: uuid.UUID):
        self.session = session
        self.box = box
        self.store_id = store_id

    async def keys(self) -> set[str]:
        return set(await self.session.scalars(
            select(SecretStoreEntry.key).where(SecretStoreEntry.store_id == self.store_id)
        ))

    async def values(self, names: Collection[str]) -> dict[str, str]:
        if self.box is None:
            raise RuntimeError("this process cannot decrypt store values")
        if not names:
            return {}
        rows = await self.session.execute(
            select(SecretStoreEntry.key, SecretStoreEntry.value_encrypted)
            .where(SecretStoreEntry.store_id == self.store_id, SecretStoreEntry.key.in_(list(names)))
        )
        return {key: self.box.decrypt(value) for key, value in rows.all()}
