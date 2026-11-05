"""Task and run states and the allowed transitions between them (architecture §5)."""

from __future__ import annotations

from enum import StrEnum


class State(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    RESUMING = "resuming"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


TERMINAL: frozenset[State] = frozenset(
    {State.COMPLETED, State.FAILED, State.CANCELLED, State.REJECTED}
)
ACTIVE: frozenset[State] = frozenset({State.PREPARING, State.RUNNING, State.RESUMING})

# ``None`` is "no state yet": intake either queues or rejects a task.
TRANSITIONS: dict[State | None, frozenset[State]] = {
    None: frozenset({State.QUEUED, State.REJECTED}),
    State.QUEUED: frozenset({State.PREPARING, State.CANCELLED, State.FAILED}),
    State.PREPARING: frozenset({State.RUNNING, State.CANCELLED, State.FAILED}),
    State.RUNNING: frozenset(
        {State.COMPLETED, State.WAITING_FOR_INPUT, State.CANCELLED, State.FAILED}
    ),
    State.WAITING_FOR_INPUT: frozenset({State.RESUMING, State.CANCELLED, State.FAILED}),
    State.RESUMING: frozenset({State.RUNNING, State.CANCELLED, State.FAILED}),
    State.COMPLETED: frozenset(),
    State.FAILED: frozenset(),
    State.CANCELLED: frozenset(),
    State.REJECTED: frozenset(),
}


class InvalidTransition(Exception):
    def __init__(self, current: State | None, new: State):
        super().__init__(f"invalid state transition {current} -> {new}")
        self.current = current
        self.new = new


def _coerce(state: State | str | None) -> State | None:
    return None if state is None else State(state)


def can_transition(current: State | str | None, new: State | str) -> bool:
    return State(new) in TRANSITIONS[_coerce(current)]


def check_transition(current: State | str | None, new: State | str) -> None:
    if not can_transition(current, new):
        raise InvalidTransition(_coerce(current), State(new))


def is_terminal(state: State | str | None) -> bool:
    return state is not None and State(state) in TERMINAL
