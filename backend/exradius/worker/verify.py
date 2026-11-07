"""Project verification: a dry run of run preparation (onboarding §3.2, config-repo.md §9).

The worker prepares the project exactly as a run would: it reads the config ("Read config":
commit, errors, warnings, missing keys), builds the images the config builds, refreshes
the mirrors, clones every repository, creates the attachments folder, starts the services
and runs the setup commands in the workstation. It works in a throwaway directory, ``runs/verify-<id>``, records every
step in ``project_verifications.steps`` as it goes, and removes the directory at the end.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select

from exradius.models import Project, ProjectVerification, utcnow
from exradius.project_state import assess, read_config, remember, store_values
from exradius.resolve import StoreValues, repo_specs, service_environment, workstation_environment
from exradius.schemas.project_config import ProjectConfig, ServiceDef
from exradius.worker import prepare
from exradius.worker.builds import BuildFailed, BuildJob, build_jobs
from exradius.worker.containers import ServiceFailed, ServiceSpec
from exradius.worker.diagnose import diagnose_steps
from exradius.worker.runs import VERIFY_PREFIX, RunManager

log = logging.getLogger(__name__)

LOG_TAIL = 4000
OPEN_STATES = ("pending", "running")


class StepFailed(Exception):
    pass


def _pending(name: str) -> dict[str, Any]:
    return {"name": name, "state": "pending", "started_at": None, "ended_at": None, "log": ""}


def _tail(text: str) -> str:
    text = text.strip()
    return text if len(text) <= LOG_TAIL else "…" + text[-LOG_TAIL:]


class ProjectVerifier:
    def __init__(self, runs: RunManager):
        self.runs = runs
        self.settings = runs.settings
        self.sessions = runs.sessions
        self.containers = runs.containers

    def directory_name(self, verification_id: uuid.UUID) -> str:
        return f"{VERIFY_PREFIX}{verification_id}"

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

        label = self.directory_name(verification_id)
        paths = prepare.run_paths(self.settings.data_dir, label)
        data_dir = self.settings.data_dir
        read: dict[str, Any] = {}  # filled by "Read config"

        async def read_config_step() -> str:
            async with self.sessions() as session:
                row = await session.get(Project, project_id)
                assert row is not None
                snapshot = await read_config(session, self.runs.box, data_dir, row)
                remember(row, snapshot)
                health = await assess(session, row, snapshot)
                secrets = await store_values(session, self.runs.box, snapshot.store)
                verification = await session.get(ProjectVerification, verification_id)
                if verification is not None:
                    verification.commit = snapshot.commit  # also when the file has errors
                await session.commit()
            lines = [
                f"commit {snapshot.commit[:12]} ({snapshot.path} on {snapshot.branch})" if snapshot.commit
                else "the config could not be read",
                *(f"error: {e}" for e in health.errors),
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

        async def create_directory() -> str:
            await asyncio.to_thread(prepare.create_run_directory, data_dir, label)
            return f"created runs/{label}"

        async def remove_directory() -> str:
            await asyncio.to_thread(prepare.remove_run_directory, paths)
            return f"removed runs/{label}"

        steps: list[dict[str, Any]] = [_pending("Read config")]
        await self._save(verification_id, steps, state="running", started_at=utcnow())
        error = await self._step(verification_id, steps, 0, read_config_step)
        if error is not None:
            await self._finish(verification_id, steps, slug, error)
            return

        config: ProjectConfig = read["config"]
        secrets: StoreValues = read["secrets"]
        images: dict[str, str] = {
            "workstation": config.workstation.image or self.settings.project_default_image,
            **{name: service.image or "" for name, service in config.services.items()},
        }
        repos = repo_specs(config, secrets)
        env = workstation_environment(config, secrets)  # complete: "Read config" found every key

        plan: list[tuple[str, Callable[[], Awaitable[str]]]] = []
        for job in build_jobs(config):
            plan.append((job.step, self._build_step(project_id, slug, read["commit"], job, images)))
        plan.append(("Create directory", create_directory))
        for repo in repos:
            plan.append((f"Fetch mirror {repo.name}", self._mirror_step(slug, repo)))
            plan.append((f"Clone {repo.name} ({repo.branch or 'default branch'})", self._clone_step(slug, repo, paths)))
        plan.append(("Prepare attachments folder", self._attachments_step(paths)))
        network: list[str] = []  # set by the first service step
        for name, service in config.services.items():
            plan.append((f"Start service {name}", self._service_step(label, name, service, images, secrets, network)))
        for command in config.workstation.setup:
            plan.append((f"Setup: {command}", self._setup_step(images, label, paths, env, command, network)))
        cleanups = [("Remove directory", remove_directory)]
        if config.services:
            cleanups.insert(0, ("Stop services", self._stop_services_step(label)))

        offset = len(steps)
        steps += [_pending(name) for name, _ in [*plan, *cleanups]]
        await self._save(verification_id, steps)
        for index, (_, action) in enumerate(plan):
            if error is not None:
                steps[offset + index]["state"] = "skipped"
                continue
            error = await self._step(verification_id, steps, offset + index, action)
        for extra, (_, action) in enumerate(cleanups):
            cleanup_error = await self._step(verification_id, steps, offset + len(plan) + extra, action)
            error = error or cleanup_error
        await self._finish(verification_id, steps, slug, error)

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

    def _build_step(self, project_id: uuid.UUID, slug: str, commit: str, job: BuildJob, images: dict[str, str]):
        async def action() -> str:
            try:
                result = await self.runs.builder.ensure(project_id, slug, commit, job)
            except BuildFailed as exc:
                raise StepFailed(str(exc)) from exc
            images[job.name] = result.image
            return result.describe()

        return action

    async def _step(self, verification_id, steps, index, action) -> str | None:
        step = steps[index]
        step.update(state="running", started_at=utcnow().isoformat())
        await self._save(verification_id, steps)
        try:
            step["log"] = _tail(await action())
            step["state"] = "passed"
            failure = None
        except Exception as exc:  # recorded on the step, shown in the panel
            message = str(exc) if isinstance(exc, (StepFailed, prepare.PreparationError)) else f"{type(exc).__name__}: {exc}"
            step["log"] = _tail(message)
            step["state"] = "failed"
            failure = f"{step['name']}: {message.splitlines()[0] if message else 'failed'}"
        step["ended_at"] = utcnow().isoformat()
        await self._save(verification_id, steps)
        return failure

    def _mirror_step(self, slug: str, repo: prepare.RepoSpec):
        async def action() -> str:
            mirror = await asyncio.to_thread(prepare.refresh_mirror, self.settings.data_dir, slug, repo)
            return prepare.redact(f"mirror of {repo.url} is up to date at mirrors/{slug}/{mirror.name}", repo.auth)

        return action

    def _clone_step(self, slug: str, repo: prepare.RepoSpec, paths: prepare.RunPaths):
        async def action() -> str:
            mirror = prepare.mirror_path(self.settings.data_dir, slug, repo.name)
            dest = paths.project / repo.name
            await asyncio.to_thread(prepare.clone_from_mirror, mirror, dest, repo)
            head = await asyncio.to_thread(prepare.git, "log", "-1", "--format=%h %s", cwd=dest)
            return f"checked out {repo.branch or 'the default branch'} at {head.strip()}"

        return action

    def _attachments_step(self, paths: prepare.RunPaths):
        async def action() -> str:
            await asyncio.to_thread(paths.attachments.mkdir, parents=True, exist_ok=True)
            await self.runs._hand_over(paths)
            return "created .exradius/attachments (read-only)"

        return action

    def _service_step(
        self, label: str, name: str, service: ServiceDef, images: dict[str, str], secrets: StoreValues, network: list[str]
    ):
        async def action() -> str:
            spec = ServiceSpec(
                name=name,
                image=images[name],
                env=service_environment(service, secrets),
                ready=service.ready.model_dump(exclude_none=True) if service.ready else None,
            )
            if not network:
                network.append(await self.containers.create_run_network(label))
            try:
                started = await self.containers.start_service(run_id=label, network=network[0], service=spec)
            except ServiceFailed as exc:
                raise StepFailed(str(exc)) from exc
            return f"{spec.image} is ready after {started.seconds:g}s ({started.check}); host name {name}"

        return action

    def _stop_services_step(self, label: str):
        async def action() -> str:
            count = await self.containers.remove_run_services(label)
            return f"removed {count} service {'container' if count == 1 else 'containers'} and the run network"

        return action

    def _setup_step(
        self,
        images: dict[str, str],
        label: str,
        paths: prepare.RunPaths,
        env: dict[str, str],
        command: str,
        network: list[str],
    ):
        async def action() -> str:
            result = await self.containers.run_setup_command(
                image=images["workstation"], run_id=label, paths=paths, env=env, command=command,
                service_network=network[0] if network else None,
            )
            if result.exit_code != 0:
                raise StepFailed(f"exited with {result.exit_code}\n{result.logs}")
            return result.logs

        return action

    async def _save(self, verification_id: uuid.UUID, steps: list[dict[str, Any]], **fields: Any) -> None:
        async with self.sessions() as session:
            verification = await session.get(ProjectVerification, verification_id)
            if verification is None:
                return
            verification.steps = [dict(step) for step in steps]  # new list: JSON change tracking
            for key, value in fields.items():
                setattr(verification, key, value)
            await session.commit()

    async def reconcile(self, is_alive: Callable[[str], Awaitable[bool]]) -> list[uuid.UUID]:
        """Fail verifications held by a worker that is no longer alive."""
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
            if owner == self.runs.worker_id or await is_alive(owner):
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
                verification.steps = [
                    {**step, "state": "failed" if step["state"] == "running" else
                     ("skipped" if step["state"] == "pending" else step["state"])}
                    for step in verification.steps or []
                ]
                await session.commit()
            label = self.directory_name(verification_id)
            await self.containers.remove_containers(
                [c.id for c in await self.containers.list_run_containers(label)]
            )
            await self.containers.remove_run_services(label)
            await asyncio.to_thread(prepare.remove_run_directory, prepare.run_paths(self.settings.data_dir, label))
            failed.append(verification_id)
        return failed
