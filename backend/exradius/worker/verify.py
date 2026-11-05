"""Project verification: a dry run of run preparation (onboarding §3.2).

The worker prepares the project exactly as a run would: it refreshes the mirrors, clones
every repository, creates the attachments folder and runs the setup commands in the
project image. It works in a throwaway directory, ``runs/verify-<id>``, records every
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
from exradius.worker import prepare
from exradius.worker.runs import VERIFY_PREFIX, RunManager

log = logging.getLogger(__name__)

LOG_TAIL = 4000
OPEN_STATES = ("pending", "running")


class StepFailed(Exception):
    pass


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
            repos = await self.runs.repo_specs(session, project.id)
            env = await self.runs._project_env(session, project.id)
            slug, image = project.slug, project.image or self.settings.project_default_image
            setup_commands = list(project.setup_commands or [])

        label = self.directory_name(verification_id)
        paths = prepare.run_paths(self.settings.data_dir, label)
        data_dir = self.settings.data_dir

        async def create_directory() -> str:
            await asyncio.to_thread(prepare.create_run_directory, data_dir, label)
            return f"created runs/{label}"

        async def remove_directory() -> str:
            await asyncio.to_thread(prepare.remove_run_directory, paths)
            return f"removed runs/{label}"

        plan: list[tuple[str, Callable[[], Awaitable[str]]]] = [("Create directory", create_directory)]
        for repo in repos:
            plan.append((f"Fetch mirror {repo.name}", self._mirror_step(slug, repo)))
            plan.append((f"Clone {repo.name} ({repo.branch})", self._clone_step(slug, repo, paths)))
        plan.append(("Prepare attachments folder", self._attachments_step(paths)))
        for command in setup_commands:
            plan.append((f"Setup: {command}", self._setup_step(image, label, paths, env, command)))
        cleanup = ("Remove directory", remove_directory)

        steps: list[dict[str, Any]] = [
            {"name": name, "state": "pending", "started_at": None, "ended_at": None, "log": ""}
            for name, _ in [*plan, cleanup]
        ]
        await self._save(verification_id, steps, state="running", started_at=utcnow())

        error: str | None = None
        for index, (_, action) in enumerate(plan):
            if error is not None:
                steps[index]["state"] = "skipped"
                continue
            error = await self._step(verification_id, steps, index, action)
        cleanup_error = await self._step(verification_id, steps, len(plan), cleanup[1])
        error = error or cleanup_error
        await self._save(
            verification_id,
            steps,
            state="failed" if error else "passed",
            ended_at=utcnow(),
            error=error,
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
            return f"checked out {repo.branch} at {head.strip()}"

        return action

    def _attachments_step(self, paths: prepare.RunPaths):
        async def action() -> str:
            await asyncio.to_thread(paths.attachments.mkdir, parents=True, exist_ok=True)
            await self.runs._hand_over(paths)
            return "created .exradius/attachments (read-only)"

        return action

    def _setup_step(self, image: str, label: str, paths: prepare.RunPaths, env: dict[str, str], command: str):
        async def action() -> str:
            result = await self.containers.run_setup_command(
                image=image, run_id=label, paths=paths, env=env, command=command
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
            await asyncio.to_thread(prepare.remove_run_directory, prepare.run_paths(self.settings.data_dir, label))
            failed.append(verification_id)
        return failed
