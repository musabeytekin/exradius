"""Intake of inbound tasks (§4.1) and answers (§4.2). Everything is persisted in Postgres
before the broker message is acknowledged."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from exradius.db import SessionFactory
from exradius.models import Agent, Attachment, InfoRequest, Project, ProjectAgent, Run, Task, utcnow
from exradius.providers import NO_PROVIDER, resolve_provider
from exradius.schemas.messages import AnswerMessage, AttachmentIn, TaskMessage, status_payload
from exradius.states import State
from exradius.storage import ObjectStore
from exradius.worker.lifecycle import PendingEvent, make_event, transition

log = logging.getLogger(__name__)


@dataclass
class TaskIntake:
    run_id: uuid.UUID | None
    events: list[PendingEvent] = field(default_factory=list)


def _first_error(exc: ValidationError) -> str:
    errors = exc.errors(include_url=False)
    parts = [f"{'.'.join(map(str, e['loc'])) or 'message'}: {e['msg']}" for e in errors[:3]]
    return "invalid task: " + "; ".join(parts)


async def _store_attachment(store: ObjectStore, prefix: str, attachment: AttachmentIn) -> dict[str, Any]:
    record: dict[str, Any] = {
        "name": attachment.name,
        "content_type": attachment.content_type,
        "object_key": None,
        "source_url": attachment.url,
    }
    if attachment.content_base64 is not None:
        key = f"{prefix}/{uuid.uuid4().hex[:8]}-{attachment.name}"
        await store.put(key, attachment.decoded(), attachment.content_type)
        record["object_key"] = key
    return record


async def accept_task(sessions: SessionFactory, store: ObjectStore, body: bytes) -> TaskIntake:
    try:
        raw = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        log.warning("dropping task message that is not JSON")
        return TaskIntake(run_id=None)
    raw = raw if isinstance(raw, dict) else {}

    async with sessions() as session:

        def reject(reason: str) -> TaskIntake:
            task_id = raw.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                log.warning("dropping task message without task_id: %s", reason)
                return TaskIntake(run_id=None)
            event = make_event(
                session,
                task_id=task_id,
                run_id=None,
                project=raw.get("project") if isinstance(raw.get("project"), str) else None,
                agent=raw.get("agent") if isinstance(raw.get("agent"), str) else None,
                correlation=raw.get("correlation"),
                type="status",
                payload=status_payload(State.REJECTED, None, reason),
            )
            return TaskIntake(run_id=None, events=[event])

        async def rejected(reason: str) -> TaskIntake:
            result = reject(reason)
            await session.commit()
            log.info("rejected task %s: %s", raw.get("task_id"), reason)
            return result

        try:
            message = TaskMessage.model_validate(raw)
        except ValidationError as exc:
            return await rejected(_first_error(exc))

        if await session.get(Task, message.task_id) is not None:
            return await rejected(f"duplicate task_id {message.task_id!r}")
        project = await session.scalar(select(Project).where(Project.slug == message.project))
        if project is None:
            return await rejected(f"unknown project {message.project!r}")
        agent = await session.scalar(select(Agent).where(Agent.slug == message.agent))
        if agent is None:
            return await rejected(f"unknown agent {message.agent!r}")
        if await session.get(ProjectAgent, (project.id, agent.id)) is None:
            return await rejected(f"agent {agent.slug!r} is not attached to project {project.slug!r}")
        if await resolve_provider(session, agent) is None:
            return await rejected(NO_PROVIDER)

        task = Task(
            id=message.task_id,
            project=project,
            agent=agent,
            instructions=message.instructions,
            context=message.context,
            correlation=message.correlation,
            state=State.QUEUED,
            received_at=utcnow(),
        )
        for attachment in message.attachments:
            record = await _store_attachment(store, f"tasks/{task.id}/attachments", attachment)
            task.attachments.append(Attachment(**record))
        run = Run(id=uuid.uuid4(), task=task, state=None)
        session.add_all([task, run])
        event = transition(session, run, State.QUEUED)
        try:
            await session.commit()
        except IntegrityError:  # the same task_id raced in through another worker
            await session.rollback()
            return await rejected(f"duplicate task_id {message.task_id!r}")
        log.info("accepted task %s as run %s", task.id, run.id)
        return TaskIntake(run_id=run.id, events=[event])


@dataclass
class AnswerIntake:
    run_id: uuid.UUID | None
    """Set when the run is waiting and can be resumed now."""


async def accept_answer(sessions: SessionFactory, store: ObjectStore, body: bytes) -> AnswerIntake:
    try:
        message = AnswerMessage.model_validate_json(body)
    except ValidationError as exc:
        log.warning("dropping invalid answer: %s", _first_error(exc))
        return AnswerIntake(run_id=None)
    try:
        request_id = uuid.UUID(message.request_id)
    except ValueError:
        log.warning("dropping answer for unknown request %s", message.request_id)
        return AnswerIntake(run_id=None)

    async with sessions() as session:
        request = await session.get(InfoRequest, request_id, with_for_update=True)
        run = await session.get(Run, request.run_id) if request else None
        if request is None or run is None or run.task_id != message.task_id:
            log.warning("dropping answer: no request %s on task %s", message.request_id, message.task_id)
            return AnswerIntake(run_id=None)

        prefix = f"tasks/{run.task_id}/answers/{request.id}"
        attachments = [await _store_attachment(store, prefix, a) for a in message.attachments]
        if request.answered_at is not None:
            # Only the first answer is delivered; later ones are stored and logged.
            request.late_answers = [
                *(request.late_answers or []),
                {"answer": message.answer, "attachments": attachments, "received_at": utcnow().isoformat()},
            ]
            await session.commit()
            log.info("stored late answer for request %s (not delivered)", request.id)
            return AnswerIntake(run_id=None)

        request.answer = message.answer
        request.answer_attachments = attachments
        request.answered_at = utcnow()
        await session.commit()
        log.info("answer received for request %s on run %s", request.id, run.id)
        return AnswerIntake(run_id=run.id if run.state == State.WAITING_FOR_INPUT else None)
