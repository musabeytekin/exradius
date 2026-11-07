"""Admin endpoints for tasks: read, send, answer, follow live, cancel (architecture §8,
onboarding §4)."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy import select

from exradius import downloads, messaging
from exradius.api.deps import DB, Admin, State, get_or_404, require_admin
from exradius.models import Artifact, Event, InfoRequest, Project, Run, Task, TranscriptChunk
from exradius.schemas import api as s
from exradius.schemas.messages import AnswerMessage, TaskMessage
from exradius.states import is_terminal
from exradius.storage import ObjectNotFound

router = APIRouter(prefix="/api")

STREAM_POLL_SECONDS = 1.0
STREAM_HEARTBEAT_SECONDS = 15.0
# Events committed out of creation order (concurrent transactions) are caught by
# re-reading a short window behind the cursor and skipping ids already sent.
STREAM_LOOKBACK = timedelta(seconds=5)


async def _run_for(session, task_id: str) -> Run | None:
    stmt = select(Run).where(Run.task_id == task_id).order_by(Run.started_at.desc().nulls_last())
    return (await session.scalars(stmt)).first()


@router.get("/tasks", response_model=list[s.TaskSummaryOut])
async def list_tasks(
    session: DB,
    _: Admin,
    state: str | None = None,
    project: str | None = None,
    agent: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    stmt = (
        select(Task, Run.id, Run.waiting_for)
        .join(Run, Run.task_id == Task.id, isouter=True)
        .join(Project, Project.id == Task.project_id)
        .order_by(Task.received_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if state:
        stmt = stmt.where(Task.state == state)
    if project:
        stmt = stmt.where(Project.slug == project)
    if agent:
        stmt = stmt.where(Task.agent == agent)
    rows = (await session.execute(stmt)).unique().all()
    return [
        s.TaskSummaryOut(
            task_id=task.id,
            project=task.project.slug,
            agent=task.agent,
            state=task.state,
            received_at=task.received_at,
            run_id=run_id,
            waiting_for=waiting_for,
        )
        for task, run_id, waiting_for in rows
    ]


@router.get("/tasks/{task_id}", response_model=s.TaskDetailOut)
async def get_task(task_id: str, session: DB, _: Admin):
    task = await get_or_404(session, Task, task_id)
    run = await _run_for(session, task_id)
    info, artifacts = [], []
    if run:
        info = (
            await session.scalars(
                select(InfoRequest).where(InfoRequest.run_id == run.id).order_by(InfoRequest.created_at)
            )
        ).all()
        artifacts = (await session.scalars(select(Artifact).where(Artifact.run_id == run.id))).all()
    events = (
        await session.scalars(select(Event).where(Event.task_id == task_id).order_by(Event.created_at))
    ).all()
    return s.TaskDetailOut(
        task_id=task.id,
        project=task.project.slug,
        agent=task.agent,
        state=task.state,
        received_at=task.received_at,
        run_id=run.id if run else None,
        waiting_for=run.waiting_for if run else None,
        instructions=task.instructions,
        context=task.context,
        correlation=task.correlation,
        attachments=[s.AttachmentOut.model_validate(a) for a in task.attachments],
        run=s.RunOut.model_validate(run) if run else None,
        info_requests=[s.InfoRequestOut.model_validate(i) for i in info],
        artifacts=[s.ArtifactOut.model_validate(a) for a in artifacts],
        events=[s.EventOut.model_validate(e) for e in events],
    )


@router.get("/tasks/{task_id}/transcript", response_model=list[s.TranscriptChunkOut])
async def get_transcript(
    task_id: str,
    session: DB,
    _: Admin,
    after_seq: int = 0,
    limit: int = Query(500, ge=1, le=5000),
):
    run = await _run_for(session, task_id)
    if run is None:
        raise HTTPException(404, "task has no run")
    stmt = (
        select(TranscriptChunk)
        .where(TranscriptChunk.run_id == run.id, TranscriptChunk.seq > after_seq)
        .order_by(TranscriptChunk.seq)
        .limit(limit)
    )
    return (await session.scalars(stmt)).all()


@router.post("/tasks/{task_id}/cancel", status_code=202)
async def cancel_task(task_id: str, session: DB, state: State, _: Admin):
    task = await get_or_404(session, Task, task_id)
    run = await _run_for(session, task_id)
    if run is None or is_terminal(run.state):
        raise HTTPException(409, f"task is {task.state}; only unfinished tasks can be cancelled")
    await state.broker.cancel(str(run.id))
    return {"run_id": str(run.id), "status": "cancel requested"}


async def _authorise_download(request: Request, session, state, path: str, sig: str | None) -> None:
    """Downloads accept the admin session or the signature carried by task.result URLs."""
    if downloads.verify(state.settings, path, sig):
        return
    await require_admin(request, session)


@router.get("/artifacts/{artifact_id}/content")
async def download_artifact(
    artifact_id: uuid.UUID, request: Request, session: DB, state: State, sig: str | None = None
):
    await _authorise_download(request, session, state, downloads.artifact_path(artifact_id), sig)
    artifact = await get_or_404(session, Artifact, artifact_id)
    try:
        data = await state.store.get(artifact.object_key)
    except ObjectNotFound as exc:
        raise HTTPException(404, "artifact content not found") from exc
    return Response(
        content=data,
        media_type=artifact.content_type,
        headers={"Content-Disposition": f'attachment; filename="{artifact.name}"'},
    )


@router.get("/runs/{run_id}/transcript")
async def download_transcript(
    run_id: uuid.UUID, request: Request, session: DB, state: State, sig: str | None = None
):
    """The run's transcript as JSON lines: the stored file once the run has ended,
    assembled from the stored chunks while it is still going."""
    await _authorise_download(request, session, state, downloads.transcript_path(run_id), sig)
    await get_or_404(session, Run, run_id)
    try:
        data = await state.store.get(downloads.transcript_key(run_id))
    except ObjectNotFound:
        chunks = (
            await session.scalars(
                select(TranscriptChunk).where(TranscriptChunk.run_id == run_id).order_by(TranscriptChunk.seq)
            )
        ).all()
        data = "".join(json.dumps(c.payload, ensure_ascii=False) + "\n" for c in chunks).encode()
    return Response(
        content=data,
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="transcript-{run_id}.jsonl"'},
    )


# Sending tasks and answers -------------------------------------------------------------------


async def _publish(state, queue: str, message: dict) -> s.PublishedOut:
    try:
        await state.broker.publish(queue, message)
    except Exception as exc:
        raise HTTPException(503, f"could not reach the broker: {exc}") from exc
    return s.PublishedOut(queue=queue, message=message)


@router.post("/tasks", response_model=s.PublishedOut, status_code=202)
async def send_task(body: TaskMessage, state: State, _: Admin):
    """Publish a task to ``exradius.tasks`` exactly as an integration would. The body is
    the task envelope; ``version`` may be left out. The worker validates project and
    agent and answers with ``task.status`` events (``queued`` or ``rejected``)."""
    return await _publish(state, messaging.TASKS_QUEUE, body.model_dump(mode="json"))


@router.post("/tasks/{task_id}/answer", response_model=s.PublishedOut, status_code=202)
async def answer_task(task_id: str, body: s.AnswerSubmission, state: State, _: Admin):
    """Publish an answer to ``exradius.answers`` exactly as an integration would."""
    try:
        message = AnswerMessage(task_id=task_id, request_id=body.request_id, answer=body.answer,
                                attachments=body.attachments)
    except ValidationError as exc:  # e.g. duplicate attachment names
        raise HTTPException(422, exc.errors(include_url=False, include_context=False, include_input=False)) from exc
    return await _publish(state, messaging.ANSWERS_QUEUE, message.model_dump(mode="json"))


# Events ----------------------------------------------------------------------------------


@router.get("/tasks/{task_id}/events", response_model=list[s.EventOut])
async def task_events(task_id: str, session: DB, _: Admin):
    """Stored outbound events of a task, oldest first (including rejections of
    messages that reused its id)."""
    stmt = select(Event).where(Event.task_id == task_id).order_by(Event.created_at, Event.id)
    return (await session.scalars(stmt)).all()


@router.get("/events", response_model=list[s.EventOut])
async def recent_events(
    session: DB,
    _: Admin,
    since: datetime | None = None,
    limit: int = Query(100, ge=1, le=1000),
):
    """Recent events across tasks, oldest first. With ``since``: the first ``limit``
    events created after it. Without: the latest ``limit`` events."""
    if since is not None:
        stmt = select(Event).where(Event.created_at > since).order_by(Event.created_at, Event.id).limit(limit)
        return (await session.scalars(stmt)).all()
    stmt = select(Event).order_by(Event.created_at.desc(), Event.id.desc()).limit(limit)
    return list(reversed((await session.scalars(stmt)).all()))


def _sse(event: str, data: dict, event_id: str | None = None) -> str:
    lines = [f"event: {event}"]
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False, default=str)}")
    return "\n".join(lines) + "\n\n"


@router.get("/tasks/{task_id}/stream")
async def stream_task(task_id: str, request: Request, state: State, _: Admin):
    """Server-sent events for one task: every stored ``status``, ``info_request`` and
    ``result`` event (data: the event envelope) and every ``transcript`` chunk (data:
    ``{run_id, seq, payload}``), replayed from the start and then live. Postgres is
    polled about once a second; a comment line is sent every 15 s of silence. The
    stream ends after the ``result`` event, or after a rejection of a task that was
    never stored. A task that does not exist yet is waited for (it may still be on its
    way through the broker)."""

    async def generate() -> AsyncIterator[str]:
        sent_events: set[uuid.UUID] = set()
        cursor: datetime | None = None
        seqs: dict[uuid.UUID, int] = {}
        last_sent = time.monotonic()
        yield "retry: 3000\n\n"
        while True:
            if await request.is_disconnected():
                return
            async with state.sessions() as session:
                stmt = select(Event).where(Event.task_id == task_id)
                if cursor is not None:
                    stmt = stmt.where(Event.created_at >= cursor - STREAM_LOOKBACK)
                events = [
                    e for e in (await session.scalars(stmt.order_by(Event.created_at, Event.id))).all()
                    if e.id not in sent_events
                ]
                run_ids = (await session.scalars(select(Run.id).where(Run.task_id == task_id))).all()
                chunks = []
                for run_id in run_ids:
                    chunks += (
                        await session.scalars(
                            select(TranscriptChunk)
                            .where(TranscriptChunk.run_id == run_id, TranscriptChunk.seq > seqs.get(run_id, 0))
                            .order_by(TranscriptChunk.seq)
                        )
                    ).all()
                task_stored = await session.get(Task, task_id) is not None

            out, finished = [], False
            for chunk in chunks:
                seqs[chunk.run_id] = max(seqs.get(chunk.run_id, 0), chunk.seq)
                out.append(_sse("transcript", {"run_id": str(chunk.run_id), "seq": chunk.seq, "payload": chunk.payload},
                                f"{chunk.run_id}:{chunk.seq}"))
            for event in events:
                sent_events.add(event.id)
                cursor = event.created_at if cursor is None else max(cursor, event.created_at)
                out.append(_sse(event.type, event.payload, str(event.id)))
                rejected = event.type == "status" and event.payload.get("payload", {}).get("state") == "rejected"
                if event.type == "result" or (rejected and not task_stored):
                    finished = True
            for item in out:
                yield item
            now = time.monotonic()
            if out:
                last_sent = now
            if finished:
                return
            if now - last_sent >= STREAM_HEARTBEAT_SECONDS:
                yield ": heartbeat\n\n"
                last_sent = now
            await asyncio.sleep(STREAM_POLL_SECONDS)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
