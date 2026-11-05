"""Run container control through the Docker SDK (architecture §5.2, §9)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

import docker
from docker.errors import APIError, NotFound
from docker.types import Mount

from exradius.config import Settings
from exradius.worker.prepare import RunPaths

log = logging.getLogger(__name__)

WORK_DIR = "/work"
HOME_DIR = "/home/runner"
LABEL_RUN = "exradius.run_id"
LABEL_KIND = "exradius.kind"


@dataclass
class ContainerExit:
    exit_code: int
    logs: str


@dataclass
class RunContainer:
    id: str
    kind: str  # "run" or "setup"
    running: bool


class ContainerManager:
    def __init__(self, settings: Settings, client: docker.DockerClient | None = None):
        self.settings = settings
        self._client = client

    @property
    def client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = (
                docker.DockerClient(base_url=self.settings.docker_url)
                if self.settings.docker_url
                else docker.from_env()
            )
        return self._client

    def host_path(self, path: Path) -> str:
        """Translate a path inside the worker to the path the Docker daemon sees."""
        relative = path.resolve().relative_to(self.settings.data_dir.resolve())
        return str(self.settings.effective_host_data_dir / relative)

    def _mounts(self, paths: RunPaths) -> list[Mount]:
        return [
            Mount(target=WORK_DIR, source=self.host_path(paths.project), type="bind"),
            Mount(
                target=f"{WORK_DIR}/.exradius/attachments",
                source=self.host_path(paths.attachments),
                type="bind",
                read_only=True,
            ),
            Mount(target=self.settings.run_state_mount, source=self.host_path(paths.state), type="bind"),
        ]

    def _create(
        self,
        *,
        image: str,
        run_id: str,
        kind: str,
        paths: RunPaths,
        env: dict[str, str],
        entrypoint: list[str],
        command: list[str],
    ):
        uid, gid = self.settings.run_uid_gid
        return self.client.containers.run(
            image,
            entrypoint=entrypoint,
            command=command,
            detach=True,
            environment={
                **env,
                "HOME": HOME_DIR,
                "EXRADIUS_STATE_DIR": self.settings.run_state_mount,
                "EXRADIUS_WORK_DIR": WORK_DIR,
            },
            working_dir=WORK_DIR,
            mounts=self._mounts(paths),
            user=f"{uid}:{gid}",
            read_only=True,
            tmpfs={
                "/tmp": "rw,exec,mode=1777",
                HOME_DIR: f"rw,exec,uid={uid},gid={gid},mode=0755",
            },
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            network=self.settings.run_network,
            labels={LABEL_RUN: run_id, LABEL_KIND: kind},
            name=f"exradius-{kind}-{run_id[:8]}-{_suffix()}",
        )

    async def start_runner(
        self, *, image: str, run_id: str, paths: RunPaths, env: dict[str, str]
    ) -> str:
        container = await asyncio.to_thread(
            self._create,
            image=image,
            run_id=run_id,
            kind="run",
            paths=paths,
            env=env,
            entrypoint=["exradius-runner"],
            command=[run_id],
        )
        return container.id

    async def run_setup_command(
        self, *, image: str, run_id: str, paths: RunPaths, env: dict[str, str], command: str
    ) -> ContainerExit:
        container = await asyncio.to_thread(
            self._create,
            image=image,
            run_id=run_id,
            kind="setup",
            paths=paths,
            env=env,
            entrypoint=["/bin/sh", "-c"],
            command=[command],
        )
        return await self.wait(container.id)

    async def wait(self, container_id: str) -> ContainerExit:
        def _wait() -> ContainerExit:
            container = self.client.containers.get(container_id)
            status = container.wait()
            logs = container.logs(tail=200).decode(errors="replace")
            try:
                container.remove(force=True)
            except (NotFound, APIError):
                pass
            return ContainerExit(exit_code=int(status.get("StatusCode", -1)), logs=logs)

        try:
            return await asyncio.to_thread(_wait)
        except NotFound:
            return ContainerExit(exit_code=-1, logs="container disappeared")

    async def list_run_containers(self, run_id: str) -> list[RunContainer]:
        """Every container (running or exited) labelled with this run id."""

        def _list() -> list[RunContainer]:
            found = self.client.containers.list(all=True, filters={"label": f"{LABEL_RUN}={run_id}"})
            return [
                RunContainer(id=c.id, kind=c.labels.get(LABEL_KIND, ""), running=c.status in ("running", "created", "restarting"))
                for c in found
            ]

        return await asyncio.to_thread(_list)

    async def remove_containers(self, container_ids: list[str]) -> None:
        """Stop (if needed) and remove containers."""

        def _remove() -> None:
            for container_id in container_ids:
                try:
                    self.client.containers.get(container_id).remove(force=True)
                except NotFound:
                    pass
                except APIError as exc:
                    log.warning("could not remove %s: %s", container_id[:12], exc)

        await asyncio.to_thread(_remove)

    async def stop_run_containers(self, run_id: str) -> None:
        """Stop every container of a run (runner and setup)."""

        def _stop() -> None:
            for container in self.client.containers.list(all=True, filters={"label": f"{LABEL_RUN}={run_id}"}):
                try:
                    container.stop(timeout=10)
                except (NotFound, APIError) as exc:
                    log.warning("could not stop %s: %s", container.id[:12], exc)

        await asyncio.to_thread(_stop)


def _suffix() -> str:
    import secrets

    return secrets.token_hex(3)
