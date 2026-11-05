"""Admin read endpoints for tasks, runs and transcripts, plus cancel (architecture §8)."""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import select

from exradius import downloads
from exradius.api.deps import DB, Admin, State, get_or_404, require_admin
from exradius.models import Agent, Artifact, Event, InfoRequest, Project, Run, Task, TranscriptChunk
from exradius.schemas import api as s
from exradius.states import is_terminal
from exradius.storage import ObjectNotFound

router = APIRouter(prefix="/api")


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
        select(Task, Run.id)
        .join(Run, Run.task_id == Task.id, isouter=True)
        .join(Project, Project.id == Task.project_id)
        .join(Agent, Agent.id == Task.agent_id)
        .order_by(Task.received_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if state:
        stmt = stmt.where(Task.state == state)
    if project:
        stmt = stmt.where(Project.slug == project)
    if agent:
        stmt = stmt.where(Agent.slug == agent)
    rows = (await session.execute(stmt)).unique().all()
    return [
        s.TaskSummaryOut(
            task_id=task.id,
            project=task.project.slug,
            agent=task.agent.slug,
            state=task.state,
            received_at=task.received_at,
            run_id=run_id,
        )
        for task, run_id in rows
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
        agent=task.agent.slug,
        state=task.state,
        received_at=task.received_at,
        run_id=run.id if run else None,
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
    await state.control.cancel(str(run.id))
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
