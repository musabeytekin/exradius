"""Project verification: a dry run of run preparation (onboarding §3.2, config-repo.md §9,
docs/kubernetes.md §13).

"Read config" runs in the worker: commit, errors, warnings, missing keys, and whether this
version can run the config. Then the worker creates a Run with ``purpose: verify``: the same
pod as a task's, whose steps are the Run's steps ("Prepare workspace", "Clone <repo>
(<branch>)", "Setup: <command>"), and whose runner lists the tools of every connection an
agent uses ("List tools <name>", docs/agent-routing.md §3.1) with the services and setup of
the pod, reporting them through the internal API. The worker mirrors the Run's status and
the reports into ``project_verifications.steps`` as they change, and deletes the Run at the
end, which removes everything the verification made.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select

from exradius.crypto import hash_token, new_run_token
from exradius.models import ConnectionTools, Project, ProjectVerification, utcnow
from exradius.project_state import assess, read_config, remember, store_values
from exradius.resolve import StoreValues
from exradius.routing import definition_hash
from exradius.schemas.project_config import ProjectConfig
from exradius.worker import run_objects
from exradius.worker.diagnose import diagnose_steps
from exradius.worker.run_objects import RunView, run_name
from exradius.worker.runs import RunManager, exceeded_memory

log = logging.getLogger(__name__)

LOG_TAIL = 4000
OPEN_STATES = ("pending", "running")
POLL_SECONDS = 1.0


class StepFailed(Exception):
    pass


def _pending(name: str, container: str | None = None, connection: str | None = None) -> dict[str, Any]:
    step: dict[str, Any] = {"name": name, "state": "pending", "started_at": None, "ended_at": None, "log": ""}
    if container:
        step["container"] = container
    if connection:
        step["connection"] = connection
    return step


def _tail(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= LOG_TAIL else "…" + text[-LOG_TAIL:]


def used_connections(config: ProjectConfig) -> list[str]:
    """Connections some agent uses, in file order."""
    return [name for name in config.connections if any(name in a.connections for a in config.agents.values())]


def run_steps(config: ProjectConfig) -> list[dict[str, Any]]:
    """The verify Run's steps, by container (docs/kubernetes.md §13)."""
    steps = [_pending("Prepare workspace", "prepare")]
    steps += [_pending(f"Clone {repo.name} ({repo.branch or 'default branch'})", f"clone-{index}")
              for index, repo in enumerate(config.repositories)]
    steps += [_pending(f"Setup: {command}", f"setup-{index}") for index, command in enumerate(config.workstation.setup)]
    steps += [_pending(f"List tools {name}", connection=name) for name in used_connections(config)]
    return steps


def _setup_log(step: dict[str, Any], status: dict[str, Any], config: ProjectConfig) -> str:
    log_text = status.get("log") or ""
    if status.get("state") != "failed":
        return log_text
    if status.get("reason") == "OOMKilled" and config.resources.memory:
        return f"exited with 137: {exceeded_memory(config)}\n{log_text}"
    return f"exited with {status.get('exitCode')}\n{log_text}"


def apply_status(steps: list[dict[str, Any]], view: RunView, reports: dict[str, Any], config: ProjectConfig) -> None:
    """Mirror the Run's status and the runner's tool reports into the verification's steps."""
    by_container = {s.get("container"): s for s in view.steps if s.get("container")}
    for step in steps:
        container = step.get("container")
        if container is None or container not in by_container:
            continue
        status = by_container[container]
        state = status.get("state") or "pending"
        step["state"] = state if state in ("pending", "running", "passed", "failed") else "pending"
        step["started_at"] = status.get("startedAt") or step["started_at"]
        step["ended_at"] = status.get("finishedAt") or step["ended_at"]
        text = _setup_log(step, status, config) if step["name"].startswith("Setup: ") else status.get("log") or ""
        step["log"] = _tail(text)
    first = steps[0] if steps else None
    if first is not None and first["state"] == "pending" and (message := view.unschedulable):
        first["state"], first["log"] = "running", f"waiting for cluster capacity: {message}"
        first["started_at"] = first["started_at"] or utcnow().isoformat()

    tools_running = view.current and view.phase == "Running"
    for step in steps:
        name = step.get("connection")
        if name is None:
            continue
        report = reports.get(name)
        if report is None:
            if tools_running and step["state"] == "pending":
                step["state"], step["started_at"] = "running", utcnow().isoformat()
                tools_running = False  # one at a time, in order
            elif step["state"] == "running":
                tools_running = False
            continue
        step["ended_at"] = step["ended_at"] or utcnow().isoformat()
        step["started_at"] = step["started_at"] or step["ended_at"]
        if report.get("ok"):
            tools = report.get("tools") or []
            names = ", ".join(t.get("name", "?") for t in tools)
            step["state"] = "passed"
            step["log"] = f"{len(tools)} {'tool' if len(tools) == 1 else 'tools'}" + (f": {names}" if names else "")
        else:
            step["state"] = "failed"
            step["log"] = _tail("\n".join(p for p in (report.get("error") or "failed", (report.get("log") or "").strip()) if p))


def finish_steps(steps: list[dict[str, Any]], view: RunView) -> None:
    """Once the Run ended: the step it stopped in fails with the reason, later ones are skipped."""
    now = utcnow().isoformat()
    failed = any(s["state"] == "failed" for s in steps)
    for step in steps:
        if step["state"] in ("passed", "failed", "skipped"):
            continue
        if failed:
            step["state"] = "skipped"
            continue
        if not view.exists:
            message = "the verify Run disappeared"
        elif view.phase == "Succeeded" and step.get("connection"):
            message = "the runner ended without listing this connection's tools"
        else:
            workstation = view.workstation
            message = "\n".join(p for p in (view.message, _tail(workstation.get("log") or "")) if p) \
                or f"the verify pod ended ({view.reason or view.phase})"
        step.update(state="failed", log=_tail(message), started_at=step["started_at"] or now, ended_at=now)
        failed = True


class ProjectVerifier:
    def __init__(self, runs: RunManager):
        self.runs = runs
        self.settings = runs.settings
        self.sessions = runs.sessions
        self.cluster = runs.cluster
        self.poll = POLL_SECONDS

    async def claim(self, verification_id: uuid.UUID, worker_id: str | None) -> bool:
        """Take a pending verification before acknowledging its control message, so a
        worker that dies holding it can be detected at the next start."""
        async with self.sessions() as session:
            verification = await session.get(
                ProjectVerification, verification_id, with_for_update=True, populate_existing=True
            )
            if verification is None or verification.state != "pending" or verification.worker_id:
                return False
            verification.worker_id = worker_id
            await session.commit()
            return True

    async def run(self, verification_id: uuid.UUID) -> None:
        async with self.sessions() as session:
            verification = await session.get(ProjectVerification, verification_id)
            if verification is None or verification.state != "pending":
                return
            project = await session.get(Project, verification.project_id)
            if project is None:
                return
            project_id, slug = project.id, project.slug

        read: dict[str, Any] = {}  # filled by "Read config"

        async def read_config_step() -> str:
            async with self.sessions() as session:
                row = await session.get(Project, project_id)
                assert row is not None
                snapshot = await read_config(session, self.runs.box, self.settings.cache_dir, row)
                remember(row, snapshot)
                health = await assess(session, row, snapshot)
                secrets = await store_values(session, self.runs.box, snapshot.store)
                verification = await session.get(ProjectVerification, verification_id)
                if verification is not None:
                    verification.commit = snapshot.commit  # also when the file has errors
                await session.commit()
            await self.runs.check_directory()
            lines = [
                f"commit {snapshot.commit[:12]} ({snapshot.path} on {snapshot.branch})" if snapshot.commit
                else "the config could not be read",
                *(f"error: {e}" for e in health.errors),
                *(f"not available yet: {e}" for e in health.unsupported),
                *(f"warning: {w}" for w in snapshot.warnings),
                f"missing keys: {', '.join(health.missing_keys) or 'none'}"
                + (f" (store {snapshot.store}{'' if health.store_exists else ', which does not exist'})"
                   if snapshot.required_keys else ""),
            ]
            if health.reason is not None:
                raise StepFailed("\n".join([health.reason, *lines]))
            assert snapshot.config is not None and snapshot.commit is not None
            read.update(config=snapshot.config, commit=snapshot.commit, secrets=secrets)
            return "\n".join(lines)

        steps: list[dict[str, Any]] = [_pending("Read config")]
        await self._save(verification_id, steps, state="running", started_at=utcnow())
        error = await self._step(verification_id, steps, 0, read_config_step)
        if error is None:
            error = await self._verify_run(verification_id, project_id, slug, read, steps)
        await self._finish(verification_id, steps, slug, error)

    async def _verify_run(self, verification_id: uuid.UUID, project_id: uuid.UUID, slug: str,
                          read: dict[str, Any], steps: list[dict[str, Any]]) -> str | None:
        config: ProjectConfig = read["config"]
        secrets: StoreValues = read["secrets"]
        offset = len(steps)
        steps += run_steps(config)
        token = new_run_token()
        await self._save(verification_id, steps, token_hash=hash_token(token), tool_reports={})
        runtime = next(iter(config.agents.values())).runtime
        objects = run_objects.verify_run(
            verification_id=verification_id, namespace=self.runs.namespace, project=slug, config=config,
            secrets=secrets, runtime=runtime, runtime_image=self.settings.runtime_image(runtime),
            workstation_image=self.runs.workstation_image(config), token=token,
        )
        own = steps[offset:]
        try:
            await self.runs.create(objects)
        except Exception as exc:  # recorded on the first step
            own[0].update(state="failed", log=f"could not create the verify Run: {type(exc).__name__}: {exc}",
                          started_at=utcnow().isoformat(), ended_at=utcnow().isoformat())
            finish_steps(own, RunView({}))
            await self._save(verification_id, steps, token_hash=None)
            return self._failure(steps)
        try:
            view = RunView(None)
            while True:
                view = RunView(await self.cluster.get_run(objects.name))
                reports = await self._reports(verification_id)
                before = [dict(s) for s in own]
                apply_status(own, view, reports, config)
                if not view.exists or view.final:
                    finish_steps(own, view)
                if own != before:
                    await self._save(verification_id, steps)
                if not view.exists or view.final:
                    break
                await asyncio.sleep(self.poll)
            await self._store_tools(project_id, verification_id, read["commit"], config, reports)
        finally:
            await self.runs._delete_run(objects.name)
            await self._save(verification_id, steps, token_hash=None)
        return self._failure(steps)

    @staticmethod
    def _failure(steps: list[dict[str, Any]]) -> str | None:
        failed = next((s for s in steps if s["state"] == "failed"), None)
        if failed is None:
            return None
        message = failed.get("log") or ""
        return f"{failed['name']}: {message.splitlines()[0] if message else 'failed'}"

    async def _reports(self, verification_id: uuid.UUID) -> dict[str, Any]:
        async with self.sessions() as session:
            reports = await session.scalar(
                select(ProjectVerification.tool_reports).where(ProjectVerification.id == verification_id)
            )
        return dict(reports or {})

    async def _finish(self, verification_id: uuid.UUID, steps: list[dict[str, Any]], slug: str, error: str | None) -> None:
        await self._save(
            verification_id,
            steps,
            state="failed" if error else "passed",
            ended_at=utcnow(),
            error=error,
            diagnosis=diagnose_steps(steps) if error else None,
        )
        log.info("verification %s of project %s: %s", verification_id, slug, "failed" if error else "passed")

    async def _step(self, verification_id, steps, index, action) -> str | None:
        step = steps[index]
        step.update(state="running", started_at=utcnow().isoformat())
        await self._save(verification_id, steps)
        try:
            step["log"] = _tail(await action())
            step["state"] = "passed"
            failure = None
        except Exception as exc:  # recorded on the step, shown in the panel
            message = str(exc) if isinstance(exc, StepFailed) else f"{type(exc).__name__}: {exc}"
            step["log"] = _tail(message)
            step["state"] = "failed"
            failure = f"{step['name']}: {message.splitlines()[0] if message else 'failed'}"
        step["ended_at"] = utcnow().isoformat()
        await self._save(verification_id, steps)
        return failure

    async def _store_tools(self, project_id, verification_id, commit, config: ProjectConfig, reports) -> None:
        """The tools agent routing sees, for every connection whose listing succeeded."""
        for name, report in reports.items():
            if not report.get("ok") or name not in config.connections:
                continue
            async with self.sessions() as session:
                row = await session.scalar(
                    select(ConnectionTools).where(ConnectionTools.project_id == project_id,
                                                  ConnectionTools.connection == name).with_for_update()
                )
                if row is None:
                    row = ConnectionTools(project_id=project_id, connection=name)
                    session.add(row)
                row.definition_hash, row.tools, row.commit = definition_hash(config, name), list(report.get("tools") or []), commit
                row.verification_id, row.listed_at = verification_id, utcnow()
                await session.commit()

    async def _save(self, verification_id: uuid.UUID, steps: list[dict[str, Any]], **fields: Any) -> None:
        async with self.sessions() as session:
            verification = await session.get(ProjectVerification, verification_id)
            if verification is None:
                return
            verification.steps = [dict(step) for step in steps]  # new list: JSON change tracking
            for key, value in fields.items():
                setattr(verification, key, value)
            await session.commit()

    async def reconcile(self, is_alive: Callable[[str], Awaitable[bool]], worker_id: str) -> list[uuid.UUID]:
        """Fail verifications held by a worker that is no longer alive, and delete their Runs."""
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(ProjectVerification.id, ProjectVerification.worker_id).where(
                        ProjectVerification.state.in_(OPEN_STATES),
                        ProjectVerification.worker_id.is_not(None),
                    )
                )
            ).all()
        failed = []
        for verification_id, owner in rows:
            if owner == worker_id or await is_alive(owner):
                continue
            async with self.sessions() as session:
                verification = await session.get(
                    ProjectVerification, verification_id, with_for_update=True, populate_existing=True
                )
                if verification is None or verification.state not in OPEN_STATES or verification.worker_id != owner:
                    continue
                verification.state = "failed"
                verification.ended_at = utcnow()
                verification.error = "worker restarted during verification"
                verification.token_hash = None
                verification.steps = [
                    {**step, "state": "failed" if step["state"] == "running" else
                     ("skipped" if step["state"] == "pending" else step["state"])}
                    for step in verification.steps or []
                ]
                await session.commit()
            await self.runs._delete_run(run_name("verify", verification_id))
            failed.append(verification_id)
        return failed

    async def open_ids(self) -> set[uuid.UUID]:
        async with self.sessions() as session:
            return set((await session.scalars(
                select(ProjectVerification.id).where(ProjectVerification.state.in_(OPEN_STATES))
            )).all())
