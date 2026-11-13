"""Live updates to browsers (design §9): notify, then fetch. A stream gets the key of each
changed task of a project its person may see, never the task itself."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(eq=False)
class Listener:
    allowed: Callable[[str], bool]
    """Whether the stream's person may see a project, asked on each change."""
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=200))


class Hub:
    def __init__(self) -> None:
        self.listeners: set[Listener] = set()

    def subscribe(self, allowed: Callable[[str], bool]) -> Listener:
        listener = Listener(allowed)
        self.listeners.add(listener)
        return listener

    def unsubscribe(self, listener: Listener) -> None:
        self.listeners.discard(listener)

    def changed(self, project: str, key: str) -> None:
        for listener in list(self.listeners):
            if not listener.allowed(project):
                continue
            try:
                listener.queue.put_nowait({"key": key, "project": project})
            except asyncio.QueueFull:
                pass  # a stalled browser reads everything again when it reconnects
