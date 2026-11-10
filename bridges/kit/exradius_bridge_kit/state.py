"""An item's state, stored by the tracker on the item itself (design §8). The kit keeps no
database."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

State = Literal["starting", "active", "waiting", "ended"]


class ItemState(BaseModel):
    # Unknown fields are kept, so states written by another version survive a save.
    model_config = ConfigDict(extra="allow")

    runs: int = 0
    """Tasks started for this item; the next task id derives from it."""
    task_id: str | None = None
    project: str | None = None
    agent: str | None = None
    """Set from the ``queued`` event: Exradius picks the agent."""
    label: str | None = None
    """How comments name the agent of the current run, kept from its start."""
    state: State | None = None
    request_id: str | None = None
    asked_at: datetime | None = None
    retries: int = 0
    """Republished after a duplicate task id rejection, for the current start."""
    told_still_working: bool = False
    """"Still working on this" was posted for the current run."""

    @property
    def running(self) -> bool:
        return self.state in ("starting", "active", "waiting")

    @classmethod
    def load(cls, data: dict[str, Any] | None) -> ItemState:
        """The stored state; a missing or unreadable one is a fresh state (keeping the run
        counter when it can, so task ids do not repeat)."""
        if not isinstance(data, dict):
            return cls()
        try:
            return cls.model_validate(data)
        except ValueError:
            runs = data.get("runs")
            return cls(runs=runs if isinstance(runs, int) and runs >= 0 else 0)

    def dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def task_id_for(key: str, runs: int) -> str:
    """``PLANE-101`` for the first run, then ``PLANE-101-2``, ``-3`` (design §7)."""
    return key if runs <= 1 else f"{key}-{runs}"
