"""Memory reservations (docs/run-environment.md §4).

The worker keeps a budget (``EXRADIUS_RUN_MEMORY_BUDGET``, default: the Docker daemon's
``MemTotal`` minus 1 GiB). A run or verification reserves what its config declares
(``resources.memory`` plus every service ``memory``) and starts only when the reservations
in progress plus its own fit. Undeclared is zero: a project that declares nothing never
waits. The gate sits beside the concurrency slot, which the waiter keeps holding, so the
broker keeps the backlog.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from exradius.schemas.project_config import format_memory

log = logging.getLogger(__name__)

HEADROOM = 1024**3
"""What the default budget leaves to the host and the stack."""
RECHECK_SECONDS = 10.0
"""How often a waiter asks whether it is still wanted (a cancelled run stops waiting)."""


class TooLarge(Exception):
    """The reservation is larger than the whole budget: it would never start."""


class Abandoned(Exception):
    """The waiter is no longer wanted (cancelled while it waited)."""


@dataclass
class Reservation:
    amount: int
    waited: float = 0.0
    """Seconds spent waiting for it."""


class MemoryGate:
    def __init__(self, budget: int | None = None):
        self.budget = budget
        """``None`` until known; while unknown every reservation fits."""
        self.in_use = 0
        self._changed = asyncio.Condition()

    def describe(self, amount: int) -> str:
        budget = f" of the worker's {format_memory(self.budget)}" if self.budget else ""
        return f"{format_memory(amount)}{budget}"

    def check(self, amount: int) -> None:
        if self.budget is not None and amount > self.budget:
            raise TooLarge(
                f"the project declares {format_memory(amount)} of memory, more than this worker's budget "
                f"for runs ({format_memory(self.budget)}, EXRADIUS_RUN_MEMORY_BUDGET)"
            )

    def _fits(self, amount: int) -> bool:
        return self.budget is None or self.in_use + amount <= self.budget

    @asynccontextmanager
    async def reserve(
        self,
        amount: int,
        *,
        on_wait: Callable[[], Awaitable[None]] | None = None,
        still_wanted: Callable[[], Awaitable[bool]] | None = None,
        wait: bool = True,
    ) -> AsyncIterator[Reservation]:
        """Hold ``amount`` bytes for the duration of the block. ``on_wait`` is called once
        when the reservation has to wait; ``wait=False`` takes it at once (a container that
        is already running, after a worker restart)."""
        reservation = Reservation(amount)
        if amount <= 0:
            yield reservation
            return
        if wait:
            self.check(amount)
        started = time.monotonic()
        async with self._changed:
            if wait and not self._fits(amount):
                if on_wait is not None:
                    await on_wait()
                log.info("waiting for %s of memory (%s in use)", self.describe(amount), format_memory(self.in_use))
                while not self._fits(amount):
                    try:
                        await asyncio.wait_for(self._changed.wait(), RECHECK_SECONDS)
                    except TimeoutError:
                        pass
                    if still_wanted is not None and not self._fits(amount) and not await still_wanted():
                        raise Abandoned()
            self.in_use += amount
        reservation.waited = time.monotonic() - started
        try:
            yield reservation
        finally:
            async with self._changed:
                self.in_use -= amount
                self._changed.notify_all()
