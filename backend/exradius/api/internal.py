"""Internal API, called from run pods on the api's internal port (docs/kubernetes.md §9).

Task Runs use ``/internal/runs/{run_id}/*`` with the run token, which the worker generates
when it creates the Run, keeps hashed on the run and revokes at finalize: a cancelled
runner can still send its delivery check until then. Verify Runs use
``/internal/verifications/{id}/*`` and probe Runs ``/internal/probes/{id}/*``, each with its
own token. Build pods fetch their context from ``/internal/builds/{id}/context`` with
their Build's token (docs/kubernetes.md §12). The egress proxy's routes are in
``exradius.api.egress``.

No secret a pod does not need leaves here: an HTTP connection whose headers hold a secret
is given as the egress proxy's ``/mcp/<name>`` route without those headers, and the proxy
adds them (docs/kubernetes.md §10).

The runner never changes a run's state. What it reports is stored; ``/reported`` also
nudges the worker with a ``run_reported`` control message, and the worker's sweep picks up
a nudge that got lost.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from exradius.api.deps import DB, State
from exradius.crypto import hash_token
from exradius.environment import config_environment_text
from exradius.models import (
    Artifact,
    Attachment,
    ImageBuild,
    InfoRequest,
    Project,
    ProjectVerification,
    ProviderTest,
    Run,
    TranscriptChunk,
)
from exradius.project_state import cached, store_values
from exradius.network import route_connection
from exradius.resolve import StoreError, connection_spec
from exradius.schemas import internal as i
from exradius.schemas.project_config import ProjectConfig
from exradius.secret_backends import StoreUnreachable
from exradius.states import State as RunState
from exradius.states import is_terminal
from exradius.storage import ObjectNotFound

router = APIRouter(prefix="/internal/runs/{run_id}")
verifications = APIRouter(prefix="/internal/verifications/{verification_id}")
probes = APIRouter(prefix="/internal/probes/{test_id}")
builds = APIRouter(prefix="/internal/builds/{build_id}")

ATTACHMENTS_DIR = ".exradius/attachments"
MAX_WAIT = 60


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing run token")
    return authorization.removeprefix("Bearer ").strip()


async def authenticated_run(
    run_id: uuid.UUID,
    session: DB,
    authorization: Annotated[str | None, Header()] = None,
) -> Run:
    token = _bearer(authorization)
    run = await session.get(Run, run_id)
    if run is None or run.token_hash is None or run.token_hash != hash_token(token):
        raise HTTPException(401, "invalid run token")
    if run.ended_at is not None:
        raise HTTPException(401, "run has ended")
    return run


RunDep = Annotated[Run, Depends(authenticated_run)]


def answer_attachment_paths(request: InfoRequest) -> list[i.AttachmentPath]:
    return [
        i.AttachmentPath(path=f"{ATTACHMENTS_DIR}/{request.id}/{a['name']}", content_type=a["content_type"],
                         id=f"{request.id}.{n}")
        for n, a in enumerate(request.answer_attachments or [])
    ]


async def delivered_answers(session, run: Run) -> list[i.DeliveredAnswer]:
    if not run.resume_request_ids:
        return []
    ids = [uuid.UUID(x) for x in run.resume_request_ids]
    requests = (
        await session.scalars(select(InfoRequest).where(InfoRequest.id.in_(ids)).order_by(InfoRequest.created_at))
    ).all()
    return [
        i.DeliveredAnswer(request_id=str(r.id), question=r.question, answer=r.answer or "",
                          attachments=answer_attachment_paths(r))
        for r in requests
    ]


def _pod_connections(config: ProjectConfig, names, secrets, state) -> list[dict]:
    """The connections as a pod gets them, secrets substituted except in the headers of an
    HTTP connection, which goes through the egress proxy's route."""
    out = []
    for name in names:
        definition = config.connection_definition(name)
        out.append(route_connection(connection_spec(definition, secrets), definition, state.settings.egress_url))
    return out


@router.get("/spec", response_model=i.RunSpecResponse)
async def run_spec(run: RunDep, session: DB, state: State):
    """Built from the config the run read in ``preparing`` (``runs.config``), with secrets
    from the store that config names, decrypted now if it still allows the task's project. ``resume`` once an answer was
    delivered: a pod recreated for a resume starts there (docs/kubernetes.md §8.3)."""
    task = run.task
    if run.config is None:
        raise HTTPException(409, "the run has no config yet")
    config = ProjectConfig.model_validate(run.config, strict=False)
    try:
        secrets = await store_values(session, state.secrets, task.project_id, config)
        connections = _pod_connections(config, config.agents[task.agent].connections, secrets, state)
    except StoreUnreachable as exc:  # the runner retries a 5xx
        raise HTTPException(503, str(exc)) from exc
    except StoreError as exc:
        raise HTTPException(422, str(exc)) from exc
    agent = config.agents[task.agent]
    return i.RunSpecResponse(
        run_id=str(run.id),
        task_id=task.id,
        runtime=agent.runtime,
        mode="resume" if run.resume_request_ids else "start",
        agent_prompt=agent.role,
        environment_text=config_environment_text(config, state.settings.project_default_image, agent=task.agent,
                                                 install_allow=state.settings.egress_allow_hosts),
        model=agent.model,
        instructions=task.instructions,
        context=task.context,
        attachments=[
            i.AttachmentPath(path=f"{ATTACHMENTS_DIR}/{a.name}", content_type=a.content_type, id=str(a.id))
            for a in task.attachments
        ],
        connections=[i.ConnectionSpecModel.model_validate(c) for c in connections],
        session_id=run.runtime_session_id,
        answers=await delivered_answers(session, run),
    )


@router.post("/session", status_code=204)
async def report_session(body: i.SessionReport, run: RunDep, session: DB):
    run.runtime_session_id = body.session_id
    await session.commit()


@router.post("/transcript", status_code=204)
async def append_transcript(body: i.TranscriptBatch, run: RunDep, session: DB):
    seqs = [e.seq for e in body.events]
    existing = set(
        (
            await session.scalars(
                select(TranscriptChunk.seq).where(TranscriptChunk.run_id == run.id, TranscriptChunk.seq.in_(seqs))
            )
        ).all()
    )
    for event in body.events:
        if event.seq not in existing:  # retried batches are idempotent
            session.add(TranscriptChunk(run_id=run.id, seq=event.seq, payload=event.payload))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()


@router.post("/info-requests", response_model=i.InfoRequestCreated, status_code=201)
async def create_info_request(body: i.InfoRequestCreate, run: RunDep, session: DB):
    request = InfoRequest(run_id=run.id, question=body.question, context=body.context)
    session.add(request)
    await session.commit()
    return i.InfoRequestCreated(request_id=str(request.id))


@router.post("/result", status_code=204)
async def submit_result(body: i.ResultSubmission, run: RunDep, session: DB, state: State):
    # The last call wins: replace the output and the artifact list.
    await session.execute(delete(Artifact).where(Artifact.run_id == run.id))
    for upload in body.artifacts:
        key = f"runs/{run.id}/artifacts/{uuid.uuid4().hex[:8]}-{upload.name}"
        await state.store.put(key, base64.b64decode(upload.content_base64), upload.content_type)
        session.add(Artifact(run_id=run.id, name=upload.name, content_type=upload.content_type, object_key=key))
    run.output = body.output
    await session.commit()


@router.post("/outcome", status_code=204)
async def report_outcome(body: i.OutcomeReport, run: RunDep, session: DB):
    """The worker turns this into the state transition once the pod has ended. A run that
    already ended (cancelled) keeps its state and error; its warnings still count."""
    run.delivery_warnings = list(body.warnings)
    if body.session_id:
        run.runtime_session_id = body.session_id
    if body.final_message is not None:
        run.summary = body.final_message
    if not is_terminal(run.state):
        run.reported_status = body.status
        run.error = body.error
    await session.commit()


@router.post("/reported", status_code=204)
async def reported(body: i.Reported, run: RunDep, session: DB, state: State):
    """``waiting``: the session stopped for a question and the runner stays alive;
    ``resumed``: it resumed with the answers. The worker makes the transition."""
    if is_terminal(run.state):
        return
    run.reported_status = "waiting_for_input" if body.status == "waiting" else "resumed"
    await session.commit()
    try:
        await state.broker.run_reported(str(run.id))
    except Exception:  # noqa: BLE001 - the worker's sweep picks it up
        pass


@router.get("/answers", response_model=i.AnswersResponse)
async def answers(run: RunDep, state: State, wait: Annotated[float, Query(ge=0, le=MAX_WAIT)] = 0):
    """Long-poll: returns once the run is ``resuming`` (with the answers delivered to it)
    or has ended, else after ``wait`` seconds with none."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    while True:
        async with state.sessions() as session:
            current = await session.get(Run, run.id)
            assert current is not None
            if current.state == RunState.RESUMING or is_terminal(current.state) or current.ended_at is not None:
                found = await delivered_answers(session, current) if current.state == RunState.RESUMING else []
                return i.AnswersResponse(state=current.state, answers=found)
        if loop.time() >= deadline:
            return i.AnswersResponse(state=current.state)
        await asyncio.sleep(min(1.0, max(deadline - loop.time(), 0)))


@router.get("/attachments", response_model=list[i.AttachmentItem])
async def list_attachments(run: RunDep):
    return [i.AttachmentItem(id=str(a.id), name=a.name, content_type=a.content_type) for a in run.task.attachments]


@router.get("/attachments/{attachment_id}")
async def get_attachment(attachment_id: str, run: RunDep, session: DB, state: State):
    """A task attachment (its id) or an answer attachment (``<request id>.<n>``), from the
    object store, for the pod's ``prepare`` step and ``files`` sidecar."""
    key, content_type = None, "application/octet-stream"
    request_id, _, index = attachment_id.partition(".")
    try:
        object_id = uuid.UUID(request_id)
    except ValueError:
        raise HTTPException(404, "no such attachment") from None
    if index:
        request = await session.get(InfoRequest, object_id)
        if request is not None and request.run_id == run.id and request.answered_at is not None and index.isdigit():
            attachments = request.answer_attachments or []
            if int(index) < len(attachments):
                key = attachments[int(index)].get("object_key")
                content_type = attachments[int(index)].get("content_type") or content_type
    else:
        attachment = await session.get(Attachment, object_id)
        if attachment is not None and attachment.task_id == run.task_id:
            key, content_type = attachment.object_key, attachment.content_type
    if not key:
        raise HTTPException(404, "no such attachment")
    try:
        data = await state.store.get(key)
    except ObjectNotFound:
        raise HTTPException(404, "the attachment is not in the object store") from None
    return Response(content=data, media_type=content_type)


# -- verify Runs (docs/kubernetes.md §13) -------------------------------------------------------


async def authenticated_verification(
    verification_id: uuid.UUID, session: DB, authorization: Annotated[str | None, Header()] = None
) -> ProjectVerification:
    token = _bearer(authorization)
    verification = await session.get(ProjectVerification, verification_id)
    if verification is None or verification.token_hash is None or verification.token_hash != hash_token(token):
        raise HTTPException(401, "invalid token")
    if verification.state not in ("pending", "running"):
        raise HTTPException(401, "the verification has ended")
    return verification


VerificationDep = Annotated[ProjectVerification, Depends(authenticated_verification)]


@verifications.get("/spec", response_model=i.VerifySpec)
async def verify_spec(verification: VerificationDep, session: DB, state: State):
    """Every connection an agent uses, secrets substituted, from the config "Read config"
    read (the project's cached snapshot)."""
    project = await session.get(Project, verification.project_id)
    snapshot = cached(project) if project else None
    if snapshot is None or snapshot.config is None:
        raise HTTPException(409, "the project's config is not readable")
    config = snapshot.config
    used = [name for name in config.connections if any(name in a.connections for a in config.agents.values())]
    try:
        secrets = await store_values(session, state.secrets, verification.project_id, config)
        specs = _pod_connections(config, used, secrets, state)
    except StoreUnreachable as exc:  # the runner retries a 5xx
        raise HTTPException(503, str(exc)) from exc
    except StoreError as exc:
        raise HTTPException(422, str(exc)) from exc
    return i.VerifySpec(connections=[i.ConnectionSpecModel.model_validate(c) for c in specs])


@verifications.post("/tools", status_code=204)
async def verify_tools(body: i.ToolReport, verification: VerificationDep, session: DB):
    row = await session.get(ProjectVerification, verification.id, with_for_update=True, populate_existing=True)
    assert row is not None
    reports = dict(row.tool_reports or {})
    reports[body.name] = body.model_dump(exclude={"name"})
    row.tool_reports = reports
    await session.commit()


# -- probe Runs ---------------------------------------------------------------------------------


async def authenticated_probe(
    test_id: uuid.UUID, session: DB, authorization: Annotated[str | None, Header()] = None
) -> ProviderTest:
    token = _bearer(authorization)
    test = await session.get(ProviderTest, test_id)
    if test is None or test.token_hash is None or test.token_hash != hash_token(token):
        raise HTTPException(401, "invalid token")
    if test.state != "running":
        raise HTTPException(401, "the test has ended")
    return test


ProbeDep = Annotated[ProviderTest, Depends(authenticated_probe)]


@probes.post("/result", status_code=204)
async def probe_result(body: i.ProbeReport, test: ProbeDep, session: DB):
    test.report = body.model_dump()
    await session.commit()


# -- build pods (docs/kubernetes.md §12) --------------------------------------------------------


async def authenticated_build(
    build_id: uuid.UUID, session: DB, authorization: Annotated[str | None, Header()] = None
) -> ImageBuild:
    token = _bearer(authorization)
    build = await session.get(ImageBuild, build_id)
    if build is None or build.token_hash is None or build.token_hash != hash_token(token):
        raise HTTPException(401, "invalid token")
    if build.state != "running":
        raise HTTPException(401, "the build has ended")
    return build


BuildDep = Annotated[ImageBuild, Depends(authenticated_build)]


@builds.get("/context")
async def build_context(build: BuildDep, state: State):
    """The context tar the worker stored when it created the Build: the ``git archive`` of
    the context directory at the commit, with an outside Dockerfile added."""
    if not build.context_key:
        raise HTTPException(404, "the build has no context")
    try:
        data = await state.store.get(build.context_key)
    except ObjectNotFound:
        raise HTTPException(404, "the context is not in the object store") from None
    return Response(content=data, media_type="application/x-tar")
