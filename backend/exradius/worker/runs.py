"""Run orchestration: preparing, running, waiting/resuming and ending (architecture §5).

Postgres is the source of truth. Every state change happens inside a transaction that
locks the run row, records a ``task.status`` event and is published after commit. A run is
finalised (transcript uploaded, ``task.result`` published, directory deleted) exactly
once, guarded by ``runs.ended_at``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
from sqlalchemy import select, update

from exradius import downloads
from exradius.config import Settings
from exradius.crypto import SecretBox, hash_token, new_run_token
from exradius.db import SessionFactory
from exradius.models import (
    Artifact,
    InfoRequest,
    ProjectVerification,
    Repository,
    Run,
    Secret,
    TranscriptChunk,
    utcnow,
)
from exradius.providers import NO_PROVIDER, resolve_provider, runtime_environment, strip_credentials
from exradius.resolve import load_secrets, project_environment
from exradius.schemas.messages import ArtifactRef, InfoRequestPayload, ResultPayload
from exradius.states import ACTIVE, TERMINAL, State, can_transition, is_terminal
from exradius.storage import ObjectStore
from exradius.worker import prepare
from exradius.worker.containers import ContainerExit, ContainerManager
from exradius.worker.lifecycle import PendingEvent, run_event, transition

log = logging.getLogger(__name__)

Emit = Callable[[list[PendingEvent]], Awaitable[None]]

VERIFY_PREFIX = "verify-"
"""Run directories of project verifications are ``runs/verify-<verification_id>``."""


class RunCancelled(Exception):
    pass


class RunManager:
    def __init__(
        self,
        settings: Settings,
        sessions: SessionFactory,
        store: ObjectStore,
        containers: ContainerManager,
        emit: Emit,
    ):
        self.settings = settings
        self.sessions = sessions
        self.store = store
        self.containers = containers
        self.emit = emit
        self.box = SecretBox(settings.secret_key)
        self.worker_id: str | None = None
        """Stamped on runs this worker drives through preparing/running/resuming, so a
        restarting worker can tell its own dead runs from a live worker's."""

    # -- helpers ------------------------------------------------------------------------

    def paths(self, run_id: uuid.UUID) -> prepare.RunPaths:
        return prepare.run_paths(self.settings.data_dir, str(run_id))

    async def _move(self, run_id: uuid.UUID, new: State, *, expect: set[State] | None = None, reason: str | None = None, **fields) -> Run:
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

    async def _state(self, run_id: uuid.UUID) -> State:
        async with self.sessions() as session:
            return State(await session.scalar(select(Run.state).where(Run.id == run_id)))

    # -- preparing ----------------------------------------------------------------------

    async def execute(self, run_id: uuid.UUID) -> None:
        """Prepare and run a freshly queued run until it ends or waits for input."""
        try:
            run = await self._move(
                run_id,
                State.PREPARING,
                expect={State.QUEUED},
                directory=str(self.paths(run_id).root),
                worker_id=self.worker_id,
            )
            await self._prepare(run)
            await self._start_container(run_id, from_state=State.PREPARING)
            await self._settle(run_id)
        except RunCancelled:
            await self.finalize(run_id)
        except Exception as exc:
            log.exception("run %s failed", run_id)
            await self.end(run_id, State.FAILED, error=f"{type(exc).__name__}: {exc}")

    async def _prepare(self, run: Run) -> None:
        task = run.task
        async with self.sessions() as session:
            repo_specs = await self.repo_specs(session, task.project_id)
            env = await self._project_env(session, task.project_id)

        paths = await asyncio.to_thread(prepare.create_run_directory, self.settings.data_dir, str(run.id))
        await asyncio.to_thread(
            prepare.checkout_repositories, self.settings.data_dir, task.project.slug, paths, repo_specs
        )
        files = [(a.name, await self._fetch(a.object_key, a.source_url)) for a in task.attachments]
        await asyncio.to_thread(prepare.write_attachments, paths.attachments, files)
        await self._hand_over(paths)

        image = task.project.image or self.settings.project_default_image
        for command in task.project.setup_commands or []:
            if await self._state(run.id) != State.PREPARING:
                raise RunCancelled("cancelled during setup")
            result = await self.containers.run_setup_command(
                image=image, run_id=str(run.id), paths=paths, env=env, command=command
            )
            if result.exit_code != 0:
                tail = result.logs[-2000:].strip()
                raise prepare.PreparationError(
                    f"setup command {command!r} exited with {result.exit_code}:\n{tail}"
                )

    async def repo_specs(self, session, project_id: uuid.UUID) -> list[prepare.RepoSpec]:
        repos = (
            await session.scalars(
                select(Repository).where(Repository.project_id == project_id).order_by(Repository.name)
            )
        ).all()
        specs = []
        for repo in repos:
            auth = None
            if repo.auth_secret_id:
                secret = await session.get(Secret, repo.auth_secret_id)
                auth = self.box.decrypt(secret.value_encrypted) if secret else None
            specs.append(prepare.RepoSpec(repo.name, repo.url, repo.branch, auth))
        return specs

    async def _hand_over(self, paths: prepare.RunPaths) -> None:
        uid, gid = self.settings.run_uid_gid
        await asyncio.to_thread(prepare.chown_tree, paths.project, uid, gid, paths.attachments)
        await asyncio.to_thread(prepare.chown_tree, paths.state, uid, gid)
        await asyncio.to_thread(prepare.seal_read_only, paths.attachments)

    async def _fetch(self, object_key: str | None, source_url: str | None) -> bytes:
        if object_key:
            return await self.store.get(object_key)
        assert source_url
        async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
            response = await client.get(source_url)
            response.raise_for_status()
            return response.content

    async def _project_env(self, session, project_id: uuid.UUID) -> dict[str, str]:
        secrets = await load_secrets(session, self.box, project_id)
        return await project_environment(session, project_id, secrets)

    # -- running ------------------------------------------------------------------------

    async def _start_container(self, run_id: uuid.UUID, from_state: State) -> None:
        token = new_run_token()
        async with self.sessions() as session:
            run = await self._lock(session, run_id)
            if run is None or run.state != from_state:
                raise RunCancelled(f"run is {run.state if run else 'gone'}")
            task = run.task
            provider = await resolve_provider(session, task.agent)
            if provider is None:
                raise prepare.PreparationError(NO_PROVIDER)
            runtime = task.agent.runtime
            # Only this provider's variables reach the container, decrypted now.
            env = strip_credentials(runtime, await self._project_env(session, task.project_id))
            env.update(runtime_environment(runtime, self.box, provider))
            run.token_hash = hash_token(token)
            image = task.project.image or self.settings.project_default_image
            await session.commit()

        env.update(
            EXRADIUS_RUN_ID=str(run_id),
            EXRADIUS_RUN_TOKEN=token,
            EXRADIUS_API_URL=self.settings.run_api_url,
        )
        runtime_mount = await self.containers.ensure_runtime(runtime)
        container_id = await self.containers.start_runner(
            image=image, run_id=str(run_id), paths=self.paths(run_id), env=env, runtime=runtime_mount
        )
        try:
            await self._move(
                run_id,
                State.RUNNING,
                expect={from_state},
                container_id=container_id,
                reported_status=None,
            )
        except RunCancelled:
            await self.containers.stop_run_containers(str(run_id))
            await self.containers.wait(container_id)
            raise

    async def _settle(self, run_id: uuid.UUID, container_id: str | None = None, *, wait: bool = True) -> None:
        """Wait for the runner container to exit and act on what it reported.

        ``wait=False`` settles a run whose container is already gone (recovery)."""
        if container_id is None:
            async with self.sessions() as session:
                container_id = await session.scalar(select(Run.container_id).where(Run.id == run_id))
        if wait:
            assert container_id
            exit_info = await self.containers.wait(container_id)
        else:
            exit_info = ContainerExit(exit_code=-1, logs="the runner container no longer exists")

        events: list[PendingEvent] = []
        resume_now = False
        async with self.sessions() as session:
            run = await self._lock(session, run_id)
            assert run is not None
            if is_terminal(run.state):  # cancelled while running
                run.token_hash = None
                run.container_id = None
                await session.commit()
                await self.finalize(run_id)
                return
            if run.state != State.RUNNING or run.container_id != container_id:
                # Another worker already settled this container (both waited on it).
                await session.rollback()
                return
            run.token_hash = None  # revoke this container's token
            run.container_id = None
            reported = run.reported_status
            if reported == "waiting_for_input":
                events.append(transition(session, run, State.WAITING_FOR_INPUT))
                pending = (
                    await session.scalars(
                        select(InfoRequest)
                        .where(InfoRequest.run_id == run_id, InfoRequest.published_at.is_(None))
                        .order_by(InfoRequest.created_at)
                    )
                ).all()
                for request in pending:
                    request.published_at = utcnow()
                    payload = InfoRequestPayload(
                        request_id=str(request.id), question=request.question, context=request.context
                    )
                    events.append(run_event(session, run, "info_request", payload.model_dump()))
                resume_now = await self._has_undelivered_answers(session, run_id)
                await session.commit()
            elif reported == "completed":
                events.append(transition(session, run, State.COMPLETED))
                await session.commit()
            else:
                if not run.error:
                    tail = exit_info.logs[-2000:].strip()
                    run.error = f"runner exited with code {exit_info.exit_code} without reporting an outcome:\n{tail}"
                events.append(transition(session, run, State.FAILED, reason=run.error[:500]))
                await session.commit()
        await self.emit(events)
        if reported != "waiting_for_input":
            await self.finalize(run_id)
        elif resume_now:  # the answer arrived before the container had exited
            await self.resume(run_id)

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
        """Deliver answered info requests to a waiting run in a new container."""
        try:
            async with self.sessions() as session:
                run = await self._lock(session, run_id)
                if run is None or run.state != State.WAITING_FOR_INPUT:
                    return
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
                    return
                for request in answered:
                    request.delivered_at = utcnow()
                run.resume_request_ids = [str(r.id) for r in answered]
                run.worker_id = self.worker_id
                event = transition(session, run, State.RESUMING)
                attachments = [
                    (str(r.id), a) for r in answered for a in (r.answer_attachments or [])
                ]
                await session.commit()
            await self.emit([event])

            paths = self.paths(run_id)
            for request_id, attachment in attachments:
                data = await self._fetch(attachment.get("object_key"), attachment.get("source_url"))
                target = paths.attachments / request_id
                await asyncio.to_thread(self._write_answer_attachment, target, attachment["name"], data)
            await self._start_container(run_id, from_state=State.RESUMING)
            await self._settle(run_id)
        except RunCancelled:
            await self.finalize(run_id)
        except Exception as exc:
            log.exception("resuming run %s failed", run_id)
            await self.end(run_id, State.FAILED, error=f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _write_answer_attachment(directory: Path, name: str, data: bytes) -> None:
        directory.parent.chmod(0o755)
        prepare.write_attachments(directory, [(name, data)])
        prepare.seal_read_only(directory.parent)

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
        """Admin cancel: stop containers, free the directory, publish the result."""
        try:
            await self._move(run_id, State.CANCELLED, reason="cancelled by admin")
        except RunCancelled:
            return  # already ended
        await self.containers.stop_run_containers(str(run_id))
        await self.finalize(run_id)

    async def finalize(self, run_id: uuid.UUID) -> None:
        async with self.sessions() as session:
            claimed = await session.execute(
                update(Run)
                .where(Run.id == run_id, Run.ended_at.is_(None))
                .values(ended_at=utcnow(), token_hash=None)
            )
            if claimed.rowcount != 1:
                await session.rollback()
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
            await self.store.put(
                downloads.transcript_key(run_id), transcript.encode(), "application/x-ndjson"
            )
            artifacts = (await session.scalars(select(Artifact).where(Artifact.run_id == run_id))).all()
            payload = ResultPayload(
                state=run.state,
                summary=run.summary,
                output=run.output,
                artifacts=[
                    ArtifactRef(
                        name=a.name,
                        content_type=a.content_type,
                        url=downloads.artifact_url(self.settings, a.id),
                    )
                    for a in artifacts
                ],
                transcript_url=downloads.transcript_url(self.settings, run_id),
                error=run.error if run.state != State.COMPLETED else None,
            )
            events.append(run_event(session, run, "result", payload.model_dump(mode="json")))
            await session.commit()
        await self.emit(events)
        await asyncio.to_thread(prepare.remove_run_directory, self.paths(run_id))
        log.info("run %s finished: %s", run_id, payload.state)

    # -- recovery -----------------------------------------------------------------------

    async def reconcile(self, is_alive: Callable[[str], Awaitable[bool]]) -> list[tuple[uuid.UUID, str]]:
        """Startup recovery of runs left in preparing, running or resuming by a worker
        that is no longer alive.

        A run whose runner container still exists is returned for re-attachment (see
        ``reattach``). A run without one is settled from its reported outcome if the
        runner reported one, and otherwise failed and finalised. Runs owned by a live
        worker are left alone; claiming a run (stamping ``worker_id`` under the row lock)
        keeps two restarting workers from recovering the same run.
        """
        async with self.sessions() as session:
            rows = (
                await session.execute(select(Run.id, Run.worker_id).where(Run.state.in_(list(ACTIVE))))
            ).all()

        to_attach: list[tuple[uuid.UUID, str]] = []
        for run_id, owner in rows:
            if owner is not None and owner != self.worker_id and await is_alive(owner):
                continue
            claimed = await self._claim(run_id, owner)
            if claimed is None:
                continue
            state, reported, recorded_container = claimed
            containers = await self.containers.list_run_containers(str(run_id))
            runners = [c for c in containers if c.kind == "run"]
            if runners:
                live = next((c for c in runners if c.running), runners[0])
                stale = [c.id for c in containers if c.id != live.id]
                if stale:
                    await self.containers.remove_containers(stale)
                log.info("run %s: re-attaching to container %s", run_id, live.id[:12])
                to_attach.append((run_id, live.id))
                continue
            if containers:  # setup containers of a dead preparation
                await self.containers.remove_containers([c.id for c in containers])
            if state == State.RUNNING and reported is not None:
                log.info("run %s: container gone, settling reported outcome %s", run_id, reported)
                try:
                    await self._settle(run_id, recorded_container, wait=False)
                except Exception as exc:
                    log.exception("recovering run %s failed", run_id)
                    await self.end(run_id, State.FAILED, error=f"{type(exc).__name__}: {exc}")
                continue
            log.warning("run %s: no container left; failing it", run_id)
            await self.end(run_id, State.FAILED, error=f"worker restarted while the run was {state}")
        return to_attach

    async def _claim(self, run_id: uuid.UUID, owner: str | None) -> tuple[State, str | None, str | None] | None:
        async with self.sessions() as session:
            run = await self._lock(session, run_id)
            if run is None or run.state not in ACTIVE or run.worker_id != owner or run.ended_at is not None:
                await session.rollback()
                return None  # moved on, or claimed by another worker meanwhile
            run.worker_id = self.worker_id
            claimed = (State(run.state), run.reported_status, run.container_id)
            await session.commit()
            return claimed

    async def reattach(self, run_id: uuid.UUID, container_id: str) -> None:
        """Resume supervising a runner container that outlived its worker."""
        try:
            events: list[PendingEvent] = []
            async with self.sessions() as session:
                run = await self._lock(session, run_id)
                if run is None or is_terminal(run.state):
                    await session.rollback()
                    await self.containers.remove_containers([container_id])
                    if run is not None:
                        await self.finalize(run_id)
                    return
                if run.state in (State.PREPARING, State.RESUMING):
                    # The worker died between starting the container and recording it.
                    run.container_id = container_id
                    events.append(transition(session, run, State.RUNNING, "re-attached after worker restart"))
                elif run.state == State.RUNNING:
                    run.container_id = container_id
                else:
                    await session.rollback()
                    return
                await session.commit()
            await self.emit(events)
            await self._settle(run_id, container_id)
        except RunCancelled:
            await self.finalize(run_id)
        except Exception as exc:
            log.exception("re-attached run %s failed", run_id)
            await self.end(run_id, State.FAILED, error=f"{type(exc).__name__}: {exc}")

    async def sweep_orphans(self) -> list[str]:
        """Remove run directories that no unfinished run owns (e.g. left by a crash)."""
        runs_dir = self.settings.data_dir / "runs"
        if not runs_dir.is_dir():
            return []
        # List first, then ask Postgres: a directory is only created after its run is
        # in preparing, so a run started meanwhile cannot be swept by mistake.
        entries = await asyncio.to_thread(lambda: [p for p in runs_dir.iterdir() if p.is_dir()])
        ids, verification_ids = [], []
        for entry in entries:
            name, target = entry.name, ids
            if name.startswith(VERIFY_PREFIX):
                name, target = name.removeprefix(VERIFY_PREFIX), verification_ids
            try:
                target.append(uuid.UUID(name))
            except ValueError:
                pass
        keep: set[str] = set()
        async with self.sessions() as session:
            if ids:
                rows = await session.scalars(
                    select(Run.id).where(Run.id.in_(ids), Run.state.not_in(list(TERMINAL)))
                )
                keep |= {str(run_id) for run_id in rows.all()}
            if verification_ids:
                rows = await session.scalars(
                    select(ProjectVerification.id).where(
                        ProjectVerification.id.in_(verification_ids),
                        ProjectVerification.state.in_(["pending", "running"]),
                    )
                )
                keep |= {f"{VERIFY_PREFIX}{vid}" for vid in rows.all()}
        removed = []
        for entry in entries:
            if entry.name in keep:
                continue
            log.warning("removing orphaned run directory %s", entry)
            try:
                await asyncio.to_thread(prepare.remove_run_directory, prepare.run_paths(self.settings.data_dir, entry.name))
                removed.append(entry.name)
            except OSError:
                log.exception("could not remove %s", entry)
        return removed

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
