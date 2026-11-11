"""Where a secret store's values live (docs/config-repo.md §7).

A store is a name the admin owns; its ``kind`` says which backend holds its values. Every
read of a store goes through a ``SecretBackend``, which answers two questions: which key
names the store holds, and the values of some of them. Nothing outside this package knows
where a value came from: ``project_state`` turns the answers into ``StoreValues``, which
``resolve.py``, the per-run Secret and the egress proxy's policy use as they always have.

A backend that cannot answer raises ``StoreUnreachable``. That is not a missing key: the
key may well be there. Intake rejects a task, ``preparing`` fails a run and the panel shows
the store's state with that reason, never as "missing secret".
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from exradius.crypto import SecretBox
from exradius.models import SecretStore
from exradius.resolve import StoreError


class StoreUnreachable(StoreError):
    """The store's backend could not answer (the network, a timeout, a refused sign-in, a
    sealed vault): whether a key exists is unknown."""

    def __init__(self, store: str, why: str):
        super().__init__(f"secret store {store} is unreachable: {why}")
        self.store = store
        self.why = why


class SecretBackend(Protocol):
    async def keys(self) -> set[str]:
        """The key names the store holds, without reading any value."""
        ...

    async def values(self, names: Collection[str]) -> dict[str, str]:
        """The values of those ``names`` the store holds; a name it lacks is left out (the
        caller reports it as missing). Values are read now, never cached."""
        ...


class SecretBackends:
    """Makes the backend of a store. One per process: an external backend keeps its sign-in
    and a short cache of key names here."""

    def __init__(self, box: SecretBox | None = None):
        self.box = box
        """Decrypts internal values; ``None`` gives backends that can only list key names."""

    def unavailable(self, kind: str) -> str | None:
        """Why this install cannot use stores of ``kind``, or ``None`` when it can."""
        return None if kind == "internal" else f"this version does not know the store kind {kind!r}"

    def of(self, session: AsyncSession, store: SecretStore, *, retry: bool = True) -> SecretBackend:
        """The store's backend. ``retry``: an external backend tries again for a few
        seconds before it gives up (intake, ``preparing``, Verify, the run spec); the panel
        and the directory ask once."""
        from exradius.secret_backends.internal import InternalBackend

        if store.kind == "internal":
            return InternalBackend(session, self.box, store.id)
        raise StoreUnreachable(store.name, self.unavailable(store.kind) or store.kind)


__all__ = ["SecretBackend", "SecretBackends", "StoreUnreachable"]
