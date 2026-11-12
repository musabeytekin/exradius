"""Re-encrypt every stored secret with the primary key (``exradius rotate-key``).

The encrypted columns are found by the models' naming convention: every column whose name
ends in ``_encrypted`` holds a ``SecretBox`` token. One transaction re-encrypts them all, or
nothing when a value decrypts with none of the configured keys. Running it again is safe:
each value is simply encrypted again with the primary key (docs/kubernetes.md §10.1).
"""

from __future__ import annotations

from sqlalchemy import Column, Table, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from exradius.crypto import SecretBox
from exradius.models import Base

ENCRYPTED_SUFFIX = "_encrypted"


def encrypted_columns() -> dict[Table, list[Column]]:
    """Each table with columns holding ``SecretBox`` tokens, in a stable order."""
    found: dict[Table, list[Column]] = {}
    for table in Base.metadata.sorted_tables:
        columns = [column for column in table.columns if column.name.endswith(ENCRYPTED_SUFFIX)]
        if columns:
            found[table] = columns
    return found


async def rotate_encrypted(session: AsyncSession, box: SecretBox) -> dict[str, int]:
    """Re-encrypts every non-empty value of every encrypted column in ``session``'s
    transaction (the caller commits) and returns how many values each ``table.column`` had.
    Rows are locked (``FOR UPDATE`` on Postgres) so a concurrent write waits for the commit."""
    counts: dict[str, int] = {}
    for table, columns in encrypted_columns().items():
        keys = list(table.primary_key.columns)
        rows = (await session.execute(select(*keys, *columns).with_for_update())).all()
        for column in columns:
            counts[f"{table.name}.{column.name}"] = 0
        for row in rows:
            ident = {key.name: row._mapping[key] for key in keys}
            values = {}
            for column in columns:
                token = row._mapping[column]
                if not token:
                    continue
                try:
                    values[column.name] = box.rotate(token)
                except RuntimeError as exc:
                    where = ", ".join(f"{name}={value}" for name, value in ident.items())
                    raise RuntimeError(f"{table.name}.{column.name} ({where}): {exc}") from None
                counts[f"{table.name}.{column.name}"] += 1
            if values:
                await session.execute(
                    update(table).where(*(key == ident[key.name] for key in keys)).values(**values)
                )
    return counts
