"""Run orchestration on Kubernetes: preparing, running, waiting/resuming and ending
(architecture §5, docs/kubernetes.md §8).

Postgres is the source of truth. Every state change happens inside a transaction that
locks the run row, records a ``task.status`` event and is published after commit. A ``Run``
object in the runs namespace holds only the live execution: the worker creates it with its
per-run Secret, the operator turns it into a pod and mirrors the pod into ``status``, and
the worker deletes it at finalize. A run is finalised (transcript uploaded, ``task.result``
published, the Run deleted) exactly once, guarded by ``runs.ended_at``.

``settle`` is the one place that turns what happened into transitions. It is idempotent and
called for every Run event of the watch, for every ``run_reported`` nudge from the api, and
by the sweep every 15 s, so a lost event or message only delays.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC
from typing import Any

import httpx
from sqlalchemy import select, update

from exradius import downloads
from exradius.config import Settings
from exradius.crypto import SecretBox, hash_token, new_run_token
from exradius.db import SessionFactory
from exradius.models import Artifact, InfoRequest, Project, Run, TranscriptChunk, utcnow
from exradius.project_state import assess, read_config, remember, store_values
from exradius.providers import provider_problem, resolve_provider, runtime_environment
from exradius.runtimes import runtime_class
from exradius.schemas.messages import ArtifactRef, InfoRequestPayload, ResultPayload
from exradius.schemas.project_config import ProjectConfig
from exradius.states import State, can_transition, is_terminal
from exradius.storage import ObjectStore
from exradius.worker import git_identity, run_objects
from exradius.worker.builds import BuildAbandoned, BuildFailed, ImageBuilder, Images, build_jobs
from exradius.worker.kube import Cluster
from exradius.worker.lifecycle import PendingEvent, run_event, transition
from exradius.worker.run_objects import RunObjects, RunView, run_name, tail

log = logging.getLogger(__name__)

Emit = Callable[[list[PendingEvent]], Awaitable[None]]

WAITING_FOR_CAPACITY = "capacity"
"""``runs.waiting_for`` while the run's pod cannot be scheduled (it stays preparing)."""
WAITING_FOR_BUILD = "build"
"""``runs.waiting_for`` while the run waits for its images to be built (it stays preparing)."""
CANCEL_GRACE = 90.0
"""Seconds a cancelled run waits for its pod to go before it is finalised anyway."""
ABANDONED_AFTER = 1800.0
"""Seconds after which a run in preparing without a Run counts as abandoned by its worker."""


class PreparationError(Exception):
    """The run cannot start; the message is the run's error."""


class RunCancelled(Exception):
    pass


def exceeded_memory(config: ProjectConfig) -> str:
    return f"exceeded its declared memory ({config.resources.memory})"


def agent_snapshot(config: ProjectConfig, agent: str) -> dict:
    """What the transcript page shows about the agent a run used (``runs.agent_snapshot``)."""
    definition = config.agents[agent]
    name, email = config.identity(agent)
    return {
        "slug": agent,
        "name": config.agent_name(agent),
        "model": definition.model,
        "provider": definition.provider,
        "runtime": definition.runtime,
        "identity": {"name": name, "email": email},
        "connections": list(definition.connections),
    }


def step_error(view: RunView, config: ProjectConfig | None) -> str:
    """The run's error when its pod ended during preparation: the failed step with its log."""
    step = view.failed_step()
    if step is None:
        return view.message or f"the run's pod ended ({view.reason or view.phase}) before the agent started"
    name, log_tail = step.get("name") or "", tail(step.get("log") or "")
    code = step.get("exitCode")
    if name.startswith("Setup: "):
        command = name.removeprefix("Setup: ")
        if step.get("reason") == "OOMKilled" and config is not None and config.resources.memory:
            return f"setup command {command!r} {exceeded_memory(config)} and was killed (exit 137):\n{log_tail}"
        return f"setup command {command!r} exited with {code}:\n{log_tail}"
    if name.startswith("Clone "):
        return f"{name[0].lower()}{name[1:]} failed:\n{log_tail}"
    return f"{name} failed" + (f" ({step.get('reason')})" if step.get("reason") else "") + f":\n{log_tail}"


def runner_error(view: RunView, config: ProjectConfig | None) -> str:
    """The run's error when its pod ended without the runner reporting an outcome."""
    workstation = view.workstation
    log_tail = tail(workstation.get("log") or workstation.get("message") or "")
    if view.reason == "PodLost":
        return f"the run's pod was lost: {view.message or 'it was deleted'}"
    if workstation.get("reason") == "OOMKilled" and config is not None and config.resources.memory:
        return f"the run {exceeded_memory(config)} and was killed (exit 137):\n{log_tail}"
    if workstation.get("state") == "terminated":
        return (f"runner exited with code {workstation.get('exitCode')} without reporting an outcome:\n"
                f"{log_tail}").strip()
    return view.message or f"the run's pod ended ({view.reason or view.phase})"


class RunManager:
    def __init__(self, settings: Settings, sessions: SessionFactory, store: ObjectStore, cluster: Cluster,
                 emit: Emit):
        self.settings = settings
        self.sessions = sessions
        self.store = store
        self.cluster = cluster
        self.emit = emit
        self.box = SecretBox(settings.secret_key)
        self.builder = ImageBuilder(settings, sessions, store, cluster)
        self.check_directory: Callable[[], Awaitable[None]] = _nothing
        """Run after this worker stored a config snapshot (docs/integration-api.md §3); the
        worker sets it once it can publish."""
        self.spawn: Callable[[Awaitable[Any]], None] = _run_now
        """Starts background work; the worker sets it to its own task set."""
        self.resume_later: Callable[[uuid.UUID], None] = lambda run_id: self.spawn(self.resume(run_id))
        """Resumes a run in the background; the worker wraps it in a concurrency slot."""
        self.other_workers_alive: Callable[[], Awaitable[bool]] = _no
        """Whether another worker sent a heartbeat lately; the worker sets it."""
        self._released: dict[uuid.UUID, asyncio.Event] = {}
        self._settling: dict[uuid.UUID, asyncio.Lock] = {}

    # -- helpers ------------------------------------------------------------------------

    @property
    def namespace(self) -> str:
        return self.settings.runs_namespace

    async def _move(self, run_id: uuid.UUID, new: State, *, expect: set[State] | None = None,
                    reason: str | None = None, **fields) -> Run:
        """Transition the run; raise ``RunCancelled`` if it already ended (e.g. cancelled)."""
        async with self.sessions() as session:
            run = await self._lock(session, run_id)
            assert run is not None
            current = State(run.state)
            if is_terminal(current) or (expect and current not in expect) or not can_transition(current, new):
                raise RunCancelled(f"run is {current}, cannot move to {new}")
            for key, value in fields.items():
                setattr(run, key, value)
            event = transition(session, run, new, reason)
            await session.commit()
        await self.emit([event])
        return run

    @staticmethod
    async def _lock(session, run_id: uuid.UUID) -> Run | None:
        """Load the run row FOR UPDATE (only the run row; joined tables are not locked)."""
        return await session.get(Run, run_id, with_for_update={"of": Run}, populate_existing=True)

    def _release(self, run_id: uuid.UUID) -> None:
        event = self._released.get(run_id)
        if event is not None:
            event.set()

    async def hold(self, run_id: uuid.UUID) -> None:
        """Return once the run waits for input or has ended: the time it holds a slot of
        ``worker.concurrency``. Re-checked every 15 s in case another worker settled it."""
        event = self._released.setdefault(run_id, asyncio.Event())
        try:
            while not event.is_set():
                try:
                    await asyncio.wait_for(event.wait(), 15)
                except TimeoutError:
                    async with self.sessions() as session:
                        run = await session.get(Run, run_id)
                    if run is None or run.ended_at is not None or run.state == State.WAITING_FOR_INPUT:
                        return
        finally:
            self._released.pop(run_id, None)

    def _hold_from_now(self, run_id: uuid.UUID) -> None:
        """Arm the slot event before anything can release it."""
        self._released.setdefault(run_id, asyncio.Event()).clear()

    # -- preparing ----------------------------------------------------------------------

    async def execute(self, run_id: uuid.UUID) -> None:
        """Prepare the run and create its Run; return when it waits for input or ends."""
        self._hold_from_now(run_id)
        try:
            run = await self._move(run_id, State.PREPARING, expect={State.QUEUED}, waiting_for=None)
            await self._launch(run)
        except RunCancelled:
            self._released.pop(run_id, None)
            await self.finalize(run_id)
            return
        except Exception as exc:
            if not isinstance(exc, PreparationError):
                log.exception("run %s failed", run_id)
            self._released.pop(run_id, None)
            message = str(exc) if isinstance(exc, PreparationError) else f"{type(exc).__name__}: {exc}"
            await self.end(run_id, State.FAILED, error=message)
            return
        await self.hold(run_id)

    async def read_run_config(self, run: Run) -> tuple[ProjectConfig, str]:
        """Read the project's config afresh (config-repo.md §9) and record what the run
        uses: the commit, the parsed config and the agent. A config that cannot run, or
        that no longer defines the task's agent, fails the run with the reason."""
        task = run.task
        async with self.sessions() as session:
            project = await session.get(Project, task.project_id)
            assert project is not None
            snapshot = await read_config(session, self.box, self.settings.cache_dir, project)
            remember(project, snapshot)
            health = await assess(session, project, snapshot)
            await session.commit()
        await self.check_directory()
        if health.reason is not None:
            raise PreparationError(health.reason)
        config = snapshot.config
        assert config is not None and snapshot.commit is not None
        if task.agent not in config.agents:
            raise PreparationError(f"agent {task.agent!r} is no longer defined in project {project.slug!r}")
        async with self.sessions() as session:
            row = await self._lock(session, run.id)
            assert row is not None
            row.config_commit = snapshot.commit
            row.config = config.model_dump(mode="json")
            row.agent_snapshot = agent_snapshot(config, task.agent)
            await session.commit()
        return config, snapshot.commit

    def workstation_image(self, config: ProjectConfig) -> str:
        return config.workstation.image or self.settings.project_default_image

    async def _images(self, run_id: uuid.UUID, project: Project, commit: str, config: ProjectConfig) -> Images:
        """The run's images, waiting in preparing while a Build makes them (§8.1). A
        cancel stops the wait; the Build goes on for whoever else needs the image."""
        if not build_jobs(config):
            return Images(workstation=self.workstation_image(config))

        async def wanted() -> bool:
            async with self.sessions() as session:
                row = await session.get(Run, run_id, populate_existing=True)
                return row is not None and row.ended_at is None and row.state == State.PREPARING

        await self._set_waiting_for(run_id, WAITING_FOR_BUILD)
        try:
            return await self.builder.images(project.id, project.slug, commit, config, wanted=wanted)
        except BuildFailed as exc:
            raise PreparationError(str(exc)) from exc
        except BuildAbandoned as exc:
            raise RunCancelled("cancelled while waiting for its image build") from exc
        finally:
            await self._set_waiting_for(run_id, None, only=WAITING_FOR_BUILD)

    async def _set_waiting_for(self, run_id: uuid.UUID, value: str | None, *, only: str | None = None) -> None:
        async with self.sessions() as session:
            query = update(Run).where(Run.id == run_id, Run.state == State.PREPARING)
            if only is not None:
                query = query.where(Run.waiting_for == only)
            await session.execute(query.values(waiting_for=value))
            await session.commit()

    async def _launch(self, run: Run) -> None:
        config, commit = await self.read_run_config(run)
        task = run.task
        agent = config.agents[task.agent]
        images = await self._images(run.id, task.project, commit, config)
        await self._store_url_attachments(run.id)
        token = new_run_token()
        name = run_name("task", run.id)
        async with self.sessions() as session:
            row = await self._lock(session, run.id)
            if row is None or row.state != State.PREPARING:
                raise RunCancelled(f"run is {row.state if row else 'gone'}")
            provider = await resolve_provider(session, agent.provider)
            if provider is None:
                raise PreparationError(provider_problem(agent.provider))
            secrets = await store_values(session, self.box, config.secret_store)
            provider_env = runtime_environment(agent.runtime, self.box, provider)
            identity_name, email = config.identity(task.agent)
            objects = run_objects.task_run(
                run_id=run.id, namespace=self.namespace, task_id=task.id, project=task.project.slug,
                config=config, secrets=secrets, runtime=agent.runtime,
                runtime_image=self.settings.runtime_image(agent.runtime),
                workstation_image=images.workstation, provider_env=provider_env,
                credential_variables=runtime_class(agent.runtime).credential_variables,
                identity=git_identity.Identity(name=identity_name, email=email, agent=task.agent, task_id=task.id),
                token=token,
            )
            # Recorded before the Run exists: its runner authenticates with this token, and
            # recovery tells "disappeared" from "never created" by the name.
            row.token_hash = hash_token(token)
            row.run_object = name
            await session.commit()
        await self.create(objects)
        log.info("run %s: created Run %s", run.id, name)

    async def create(self, objects: RunObjects) -> dict[str, Any]:
        """Create the Run, then its Secret owned by it (docs/kubernetes.md §5)."""
        created = await self.cluster.create_run(objects.run)
        await self.cluster.create_secret(objects.secret_body(created, self.namespace))
        return created

    async def _fetch(self, url: str) -> bytes:
        async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.content

    async def _store_url_attachments(self, run_id: uuid.UUID) -> None:
        """Download the task's URL attachments into the object store (§8.1): the pod's
        ``prepare`` step reads every attachment from the internal API."""
        async with self.sessions() as session:
            run = await session.get(Run, run_id)
            assert run is not None
            for attachment in run.task.attachments:
                if attachment.object_key or not attachment.source_url:
                    continue
                key = f"tasks/{run.task_id}/attachments/{uuid.uuid4().hex[:8]}-{attachment.name}"
                await self.store.put(key, await self._fetch(attachment.source_url), attachment.content_type)
                attachment.object_key = key
            await session.commit()

    # -- settling -------------------------------------------------------------------------

    async def settle(self, run_id: uuid.UUID, obj: dict[str, Any] | None | Any = ...) -> None:
        """Act on the Run's status and the runner's reports. ``obj`` is the Run as a watch
        event delivered it (``None``: deleted); without it the Run is read."""
        lock = self._settling.setdefault(run_id, asyncio.Lock())
        async with lock:
            try:
                await self._settle(run_id, obj)
            finally:
                if not lock.locked():
                    self._settling.pop(run_id, None)

    async def _settle(self, run_id: uuid.UUID, obj: Any) -> None:
        async with self.sessions() as session:
            run = await session.get(Run, run_id)
            if run is None:
                return
            if run.ended_at is not None:
                self._release(run_id)
                if obj is not None and obj is not ... and run.run_object:
                    await self._delete_run(run.run_object)  # a stray Run of a finished run
                return
            name = run.run_object
        if obj is ...:
            obj = await self.cluster.get_run(name) if name else None
        view = RunView(obj)

        events: list[PendingEvent] = []
        after: str | None = None  # "finalize" | "resume"
        async with self.sessions() as session:
            run = await self._lock(session, run_id)
            if run is None or run.ended_at is not None:
                return
            state = State(run.state)
            config = ProjectConfig.model_validate(run.config, strict=False) if run.config else None

            if is_terminal(state):  # cancelled or failed, waiting for its pod to go
                if not view.exists or view.final:
                    after = "finalize"
            elif not view.exists:
                if state == State.PREPARING and run.run_object is None:
                    if not await self._abandoned(run):
                        return  # the Run is still being created (by this worker or another)
                    run.error = "the worker restarted while preparing the run"
                    events.append(transition(session, run, State.FAILED, run.error))
                    await session.commit()
                    await self.emit(events)
                    await self.finalize(run_id)
                    return
                run.error = run.error or "the run's Kubernetes object disappeared"
                events.append(transition(session, run, State.FAILED, run.error))
                after = "finalize"
            else:
                if state == State.PREPARING:
                    capacity = WAITING_FOR_CAPACITY if view.unschedulable else None
                    if run.waiting_for != capacity:
                        run.waiting_for = capacity
                    if view.current and (view.phase == "Running" or view.final and view.workstation.get("startedAt")):
                        run.waiting_for = None
                        events.append(transition(session, run, State.RUNNING))
                        state = State.RUNNING
                    elif view.final:
                        run.error = step_error(view, config)
                        events.append(transition(session, run, State.FAILED, run.error[:500]))
                        after = "finalize"
                if state == State.RESUMING:
                    if run.reported_status is not None:  # the runner resumed (and maybe more)
                        events.append(transition(session, run, State.RUNNING))
                        state = State.RUNNING
                    elif view.final and (fresh := RunView(await self.cluster.get_run(run.run_object))).final:
                        # Read again under the row lock: resume() may have asked for a new pod.
                        run.error = f"the run's pod ended before it resumed: {runner_error(fresh, config)}"
                        events.append(transition(session, run, State.FAILED, run.error[:500]))
                        after = "finalize"
                if state == State.RUNNING:
                    reported = run.reported_status
                    if reported == "waiting_for_input":
                        events += await self._wait_for_input(session, run)
                        if await self._has_undelivered_answers(session, run_id):
                            after = "resume"  # the answer arrived before the waiting state
                        else:
                            after = "release"
                    elif view.final:
                        if reported == "completed":
                            events.append(transition(session, run, State.COMPLETED))
                        else:
                            if reported != "failed" or not run.error:
                                run.error = runner_error(view, config)
                            events.append(transition(session, run, State.FAILED, run.error[:500]))
                        after = "finalize"
                # waiting_for_input: nothing until the answer arrives; a pod lost meanwhile
                # is recreated by resume().
            await session.commit()
        await self.emit(events)
        if after == "finalize":
            await self.finalize(run_id)
        elif after == "resume":
            self._release(run_id)
            self.resume_later(run_id)
        elif after == "release":
            self._release(run_id)

    async def _abandoned(self, run: Run) -> bool:
        """A run in preparing whose Run was never created and that no live worker is
        preparing: this process is not (it would hold it), and either no other worker
        is alive or it has been preparing for longer than any preparation takes."""
        if run.id in self._released:
            return False
        if not await self.other_workers_alive():
            return True
        started = run.started_at
        if started is not None and started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        return started is not None and (utcnow() - started).total_seconds() > ABANDONED_AFTER

    async def _wait_for_input(self, session, run: Run) -> list[PendingEvent]:
        """running -> waiting_for_input, with the questions not published yet."""
        events = [transition(session, run, State.WAITING_FOR_INPUT)]
        pending = (
            await session.scalars(
                select(InfoRequest)
                .where(InfoRequest.run_id == run.id, InfoRequest.published_at.is_(None))
                .order_by(InfoRequest.created_at)
            )
        ).all()
        for request in pending:
            request.published_at = utcnow()
            payload = InfoRequestPayload(request_id=str(request.id), question=request.question, context=request.context)
            events.append(run_event(session, run, "info_request", payload.model_dump()))
        return events

    # -- waiting and resuming -----------------------------------------------------------

    @staticmethod
    async def _has_undelivered_answers(session, run_id: uuid.UUID) -> bool:
        found = await session.scalar(
            select(InfoRequest.id).where(
                InfoRequest.run_id == run_id,
                InfoRequest.answered_at.is_not(None),
                InfoRequest.delivered_at.is_(None),
            )
        )
        return found is not None

    async def resume(self, run_id: uuid.UUID) -> None:
        """Deliver answered info requests to a waiting run: its runner, long-polling in the
        same pod, resumes the session (§8.2). A pod lost while waiting is recreated on the
        same workspace (§8.3). Returns when the run waits again or ends."""
        self._hold_from_now(run_id)
        try:
            resumed = await self._resume(run_id)
        except RunCancelled:
            resumed = False
        except Exception as exc:
            log.exception("resuming run %s failed", run_id)
            self._released.pop(run_id, None)
            await self.end(run_id, State.FAILED, error=f"{type(exc).__name__}: {exc}")
            return
        if not resumed:
            self._released.pop(run_id, None)
            return
        await self.settle(run_id)
        await self.hold(run_id)

    async def _resume(self, run_id: uuid.UUID) -> bool:
        await self._store_answer_attachments(run_id)
        async with self.sessions() as session:
            run = await self._lock(session, run_id)
            if run is None or run.state != State.WAITING_FOR_INPUT:
                return False
            answered = (
                await session.scalars(
                    select(InfoRequest).where(
                        InfoRequest.run_id == run_id,
                        InfoRequest.answered_at.is_not(None),
                        InfoRequest.delivered_at.is_(None),
                    )
                )
            ).all()
            if not answered:
                return False
            name = run.run_object
            view = RunView(await self.cluster.get_run(name) if name else None)
            if view.exists and view.final:
                # The pod went while the run waited: a new one on the same workspace, whose
                # runner starts in resume mode (/spec says "resume"). Asked for under the
                # row lock, so no settle sees the old pod's end as this resume's.
                generation = view.spec_generation + 1
                log.info("run %s: its pod is gone (%s); recreating it (generation %s)", run_id,
                         view.reason or view.phase, generation)
                await self.cluster.patch_run(name, {"podGeneration": generation})
            for request in answered:
                request.delivered_at = utcnow()
            run.resume_request_ids = [str(r.id) for r in answered]
            run.reported_status = None  # the runner reports "resumed" once it has them
            event = transition(session, run, State.RESUMING)
            await session.commit()
        await self.emit([event])
        return True

    async def _store_answer_attachments(self, run_id: uuid.UUID) -> None:
        """Answer attachments given by URL go into the object store first, so the pod's
        ``files`` sidecar reads all of them from the internal API."""
        async with self.sessions() as session:
            requests = (await session.scalars(select(InfoRequest).where(
                InfoRequest.run_id == run_id, InfoRequest.answered_at.is_not(None), InfoRequest.delivered_at.is_(None),
            ))).all()
            for request in requests:
                attachments = [dict(a) for a in request.answer_attachments or []]
                changed = False
                for attachment in attachments:
                    if attachment.get("object_key") or not attachment.get("source_url"):
                        continue
                    key = f"tasks/{request.run_id}/answers/{request.id}/{uuid.uuid4().hex[:8]}-{attachment['name']}"
                    await self.store.put(key, await self._fetch(attachment["source_url"]),
                                         attachment.get("content_type") or "application/octet-stream")
                    attachment["object_key"] = key
                    changed = True
                if changed:
                    request.answer_attachments = attachments
            await session.commit()

    # -- ending -------------------------------------------------------------------------

    async def end(self, run_id: uuid.UUID, state: State, *, error: str | None = None, reason: str | None = None) -> None:
        """Move a run to a terminal state (if it is not in one yet) and finalise it."""
        events = []
        async with self.sessions() as session:
            run = await self._lock(session, run_id)
            if run is None:
                return
            if not is_terminal(run.state):
                if error:
                    run.error = error
                events.append(transition(session, run, state, reason or (error or "")[:500] or None))
                await session.commit()
        await self.emit(events)
        await self.finalize(run_id)

    async def cancel(self, run_id: uuid.UUID) -> None:
        """Admin cancel (§8.4): the operator deletes the pod with its grace period, the
        runner reports its delivery check, and the run is finalised once the pod is gone."""
        try:
            run = await self._move(run_id, State.CANCELLED, reason="cancelled by admin")
        except RunCancelled:
            return  # already ended
        self._release(run_id)
        name = run.run_object
        if name is None or await self.cluster.patch_run(name, {"cancel": True}) is None:
            await self.finalize(run_id)
            return
        self.spawn(self._finalize_later(run_id))

    async def _finalize_later(self, run_id: uuid.UUID) -> None:
        """If the pod does not go (no operator), finalise anyway; deleting the Run removes it."""
        await asyncio.sleep(CANCEL_GRACE)
        await self.finalize(run_id)

    async def finalize(self, run_id: uuid.UUID) -> None:
        async with self.sessions() as session:
            claimed = await session.execute(
                update(Run).where(Run.id == run_id, Run.ended_at.is_(None)).values(ended_at=utcnow(), token_hash=None)
            )
            if claimed.rowcount != 1:
                await session.rollback()
                self._release(run_id)
                return  # someone else finalised it
            run = await session.get(Run, run_id, populate_existing=True)
            assert run is not None
            events = []
            if not is_terminal(run.state):  # defensive: never finalise a live run silently
                events.append(transition(session, run, State.FAILED, "ended unexpectedly"))

            chunks = (
                await session.scalars(
                    select(TranscriptChunk).where(TranscriptChunk.run_id == run_id).order_by(TranscriptChunk.seq)
                )
            ).all()
            transcript = "".join(json.dumps(c.payload, ensure_ascii=False) + "\n" for c in chunks)
            await self.store.put(downloads.transcript_key(run_id), transcript.encode(), "application/x-ndjson")
            artifacts = (await session.scalars(select(Artifact).where(Artifact.run_id == run_id))).all()
            payload = ResultPayload(
                state=run.state,
                summary=run.summary,
                output=run.output,
                artifacts=[
                    ArtifactRef(name=a.name, content_type=a.content_type, url=downloads.artifact_url(self.settings, a.id))
                    for a in artifacts
                ],
                transcript_url=downloads.transcript_url(self.settings, run_id),
                error=run.error if run.state != State.COMPLETED else None,
                warnings=list(run.delivery_warnings or []),
            )
            events.append(run_event(session, run, "result", payload.model_dump(mode="json")))
            name = run.run_object
            await session.commit()
        await self.emit(events)
        self._release(run_id)
        if name:
            await self._delete_run(name)
        log.info("run %s finished: %s", run_id, payload.state)

    async def _delete_run(self, name: str) -> None:
        """Deleting the Run removes its pod, workspace and Secret (ownerReferences)."""
        try:
            await self.cluster.delete_run(name)
        except Exception:  # noqa: BLE001 - recovery deletes Runs of finished runs
            log.exception("could not delete Run %s", name)

    # -- recovery -----------------------------------------------------------------------

    async def recover(self) -> list[uuid.UUID]:
        """At worker start (§4): compare the Runs with Postgres. An unfinished run with a
        Run is settled and kept watching; one without fails; a Run of a finished run is
        deleted. Returns the runs that hold a concurrency slot (preparing, running,
        resuming) for the caller to hold."""
        items, _ = await self.cluster.list_runs()
        objects: dict[uuid.UUID, dict] = {}
        for obj in items:
            parsed = run_objects.parse_name(obj.get("metadata", {}).get("name", ""))
            if parsed and parsed[0] == "task":
                objects[parsed[1]] = obj
        async with self.sessions() as session:
            rows = (await session.execute(
                select(Run.id, Run.state).where(Run.ended_at.is_(None), Run.state != State.QUEUED)
            )).all()
        unfinished = {run_id: State(state) for run_id, state in rows if state is not None}
        for run_id, obj in objects.items():
            if run_id not in unfinished:
                log.warning("deleting Run %s: its run has ended", obj["metadata"]["name"])
                await self._delete_run(obj["metadata"]["name"])
        held = []
        for run_id in unfinished:
            await self.settle(run_id, objects.get(run_id))
            async with self.sessions() as session:
                run = await session.get(Run, run_id)
            if run is not None and run.ended_at is None and run.state in (State.PREPARING, State.RUNNING, State.RESUMING):
                held.append(run_id)
        return held

    async def unfinished_runs(self) -> list[uuid.UUID]:
        async with self.sessions() as session:
            return list((await session.scalars(
                select(Run.id).where(Run.ended_at.is_(None), Run.state != State.QUEUED, Run.state.is_not(None))
            )).all())

    async def sweep(self) -> None:
        """Settle every unfinished run from its Run as it is now (every 15 s)."""
        for run_id in await self.unfinished_runs():
            try:
                await self.settle(run_id)
            except Exception:  # noqa: BLE001 - the next sweep retries
                log.exception("settling run %s failed", run_id)

    async def resumable_runs(self) -> list[uuid.UUID]:
        """Waiting runs whose answer arrived while no worker could resume them."""
        async with self.sessions() as session:
            rows = await session.scalars(
                select(Run.id)
                .join(InfoRequest, InfoRequest.run_id == Run.id)
                .where(
                    Run.state == State.WAITING_FOR_INPUT,
                    InfoRequest.answered_at.is_not(None),
                    InfoRequest.delivered_at.is_(None),
                )
                .distinct()
            )
            return list(rows.all())

    async def on_event(self, kind: str, obj: dict[str, Any]) -> None:
        """A watch event of a task Run."""
        parsed = run_objects.parse_name(obj.get("metadata", {}).get("name", ""))
        if parsed is None or parsed[0] != "task":
            return
        try:
            await self.settle(parsed[1], None if kind == "DELETED" else obj)
        except Exception:  # noqa: BLE001 - the sweep settles it again
            log.exception("settling run %s failed", parsed[1])


async def _nothing() -> None:
    return None


async def _no() -> bool:
    return False


def _run_now(coro: Awaitable[Any]) -> None:
    asyncio.ensure_future(coro)
