"""State transitions and the events they produce (architecture §5)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from exradius.models import Event, Run, Task, utcnow
from exradius.schemas.messages import EventEnvelope, EventType, status_payload
from exradius.states import State, check_transition


@dataclass
class PendingEvent:
    """An event stored in Postgres, to publish once the transaction has committed."""

    id: uuid.UUID
    envelope: EventEnvelope


def make_event(
    session: AsyncSession,
    *,
    task_id: str,
    run_id: uuid.UUID | None,
    project: str | None,
    agent: str | None,
    correlation: Any,
    type: EventType,
    payload: dict[str, Any],
) -> PendingEvent:
    envelope = EventEnvelope(
        task_id=task_id,
        run_id=str(run_id) if run_id else None,
        project=project,
        agent=agent,
        correlation=correlation,
        type=type,
        payload=payload,
    )
    event = Event(
        id=uuid.uuid4(),
        task_id=task_id,
        run_id=run_id,
        type=type,
        payload=envelope.model_dump(mode="json"),
    )
    session.add(event)
    return PendingEvent(id=event.id, envelope=envelope)


def run_event(session: AsyncSession, run: Run, type: EventType, payload: dict[str, Any]) -> PendingEvent:
    task: Task = run.task
    return make_event(
        session,
        task_id=task.id,
        run_id=run.id,
        project=task.project.slug,
        agent=task.agent,
        correlation=task.correlation,
        type=type,
        payload=payload,
    )


def transition(
    session: AsyncSession, run: Run, new: State, reason: str | None = None, *, routing: dict[str, Any] | None = None
) -> PendingEvent:
    """Move the run and its task to ``new`` and record the ``task.status`` event;
    ``routing`` is for the ``queued`` event of an accepted task (docs/agent-routing.md §8)."""
    previous = State(run.state) if run.state else None
    check_transition(previous, new)
    run.state = new
    run.task.state = new
    run.waiting_for = None  # a run that changes state no longer waits for memory
    # ended_at is set by finalisation (worker/runs.py), which also guards it.
    if new == State.PREPARING and run.started_at is None:
        run.started_at = utcnow()
    return run_event(session, run, "status", status_payload(new, previous, reason, routing))
