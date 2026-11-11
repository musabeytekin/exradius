"""Intake of inbound tasks (§4.1) and answers (§4.2). Everything is persisted in Postgres
before the broker message is acknowledged."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from exradius import routing
from exradius.crypto import SecretBox
from exradius.db import SessionFactory
from exradius.models import Attachment, InfoRequest, Project, Run, Task, utcnow
from exradius.project_state import assess, read_config, remember
from exradius.providers import provider_problem, resolve_provider
from exradius.schemas.messages import AnswerMessage, AttachmentIn, TaskMessage, status_payload
from exradius.secret_backends import SecretBackends
from exradius.states import State
from exradius.storage import ObjectStore
from exradius.worker.lifecycle import PendingEvent, make_event, transition

log = logging.getLogger(__name__)


@dataclass
class TaskIntake:
    run_id: uuid.UUID | None
    events: list[PendingEvent] = field(default_factory=list)
    config_read: bool = False
    """Intake read the project's config and stored the snapshot, so the directory may have
    changed (docs/integration-api.md §3)."""


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


async def accept_task(
    sessions: SessionFactory,
    store: ObjectStore,
    body: bytes,
    *,
    box: SecretBox,
    cache_dir: Path,
    router: routing.Router | None = None,
    backends: SecretBackends | None = None,
) -> TaskIntake:
    """Persist a valid task for a runnable project, or record its rejection. The project's
    config is read afresh (and cached on the project) to check the agent; a task that names
    no agent is routed to one (docs/agent-routing.md §4)."""
    router = router or routing.Router()
    backends = backends or SecretBackends(box)
    try:
        raw = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        log.warning("dropping task message that is not JSON")
        return TaskIntake(run_id=None)
    raw = raw if isinstance(raw, dict) else {}

    config_read = False

    async with sessions() as session:

        def reject(reason: str) -> TaskIntake:
            task_id = raw.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                log.warning("dropping task message without task_id: %s", reason)
                return TaskIntake(run_id=None, config_read=config_read)
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
            return TaskIntake(run_id=None, events=[event], config_read=config_read)

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
        # The agent must exist in the project's current config (config-repo.md §1, §3).
        snapshot = await read_config(session, box, cache_dir, project)
        remember(project, snapshot)
        config_read = True
        health = await assess(session, project, snapshot, backends)
        if health.reason is not None:
            return await rejected(health.reason)
        assert snapshot.config is not None
        config = snapshot.config
        if message.agent is not None:
            if message.agent not in config.agents:
                known = ", ".join(config.agents)
                return await rejected(f"unknown agent {message.agent!r} in project {project.slug!r} (it defines {known})")
            picked = routing.Routing(method="named", agent=message.agent)
        elif len(config.agents) == 1:
            picked = routing.Routing(method="only_agent", agent=next(iter(config.agents)))
        else:
            key = await router.key(session, box)
            if key is None:
                return await rejected(
                    f"agent routing is not configured (no OpenAI API key), and project {project.slug} "
                    f"has {len(config.agents)} agents"
                )
            request = routing.build_request(
                project_slug=project.slug,
                project_name=config.project.display_name,
                agent_profiles=routing.profiles(config, await routing.stored_tools(session, project.id)),
                task_id=message.task_id,
                instructions=message.instructions,
                context=message.context,
                attachments=[(a.name, a.content_type) for a in message.attachments],
            )
            # The snapshot is stored first, so the call holds no transaction (or row lock).
            await session.commit()
            try:
                picked = await router.decide(key, request)
            except routing.RoutingFailed as exc:
                return await rejected(str(exc))
            log.info("routed task %s to %s (confidence %s)", message.task_id, picked.agent, picked.confidence)
        agent = config.agents[picked.agent]
        if await resolve_provider(session, agent.provider) is None:
            return await rejected(provider_problem(agent.provider))

        task = Task(
            id=message.task_id,
            project=project,
            agent=picked.agent,
            instructions=message.instructions,
            context=message.context,
            correlation=message.correlation,
            state=State.QUEUED,
            received_at=utcnow(),
        )
        for attachment in message.attachments:
            record = await _store_attachment(store, f"tasks/{task.id}/attachments", attachment)
            task.attachments.append(Attachment(**record))
        run = Run(id=uuid.uuid4(), task=task, state=None, routing=picked.record())
        session.add_all([task, run])
        event = transition(session, run, State.QUEUED, routing=picked.payload())
        try:
            await session.commit()
        except IntegrityError:  # the same task_id raced in through another worker
            await session.rollback()
            return await rejected(f"duplicate task_id {message.task_id!r}")
        log.info("accepted task %s as run %s", task.id, run.id)
        return TaskIntake(run_id=run.id, events=[event], config_read=True)


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
