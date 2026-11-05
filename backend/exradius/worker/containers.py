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
from exradius.worker.bundles import ENTRYPOINT, LABEL_ROLE, ROLE_BUNDLE, RuntimeBundles, RuntimeMount
from exradius.worker.prepare import RunPaths

log = logging.getLogger(__name__)

WORK_DIR = "/work"
HOME_DIR = "/home/runner"
LABEL_RUN = "exradius.run_id"
LABEL_KIND = "exradius.kind"
GIT_ENV = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*"}
"""Command-line level git config for every container: the checkout under /work need not be
owned by the run user as git sees it (bind mounts from Docker Desktop hosts), and project
images carry no system git config of ours."""
INTERNAL_ROLES = {ROLE_BUNDLE, "backend"}
"""``exradius.role`` labels of Exradius' own images, which cannot be project images."""


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
        self.bundles = RuntimeBundles(settings, lambda: self.client)

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

    async def ensure_runtime(self, runtime: str) -> RuntimeMount:
        """The runtime's bundle volume, extracted on first use (raises ``BundleUnavailable``)."""
        return await self.bundles.ensure(runtime)

    def _runtime_mount(self, runtime: RuntimeMount) -> Mount:
        return Mount(target=self.settings.runtime_mount, source=runtime.volume, type="volume", read_only=True)

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
        paths: RunPaths | None,
        env: dict[str, str],
        entrypoint: list[str],
        command: list[str],
        runtime: RuntimeMount | None = None,
    ):
        """``paths=None`` creates a container without the run mounts (provider probes);
        ``runtime`` mounts that bundle read-only (runs and probes, not setup commands)."""
        uid, gid = self.settings.run_uid_gid
        mounts = self._mounts(paths) if paths else []
        if runtime is not None:
            mounts.append(self._runtime_mount(runtime))
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
                **GIT_ENV,
            },
            working_dir=WORK_DIR if paths else HOME_DIR,
            mounts=mounts,
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
        self, *, image: str, run_id: str, paths: RunPaths, env: dict[str, str], runtime: RuntimeMount
    ) -> str:
        container = await asyncio.to_thread(
            self._create,
            image=image,
            run_id=run_id,
            kind="run",
            paths=paths,
            env=env,
            entrypoint=[self.entrypoint],
            command=[run_id],
            runtime=runtime,
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

    @property
    def entrypoint(self) -> str:
        return ENTRYPOINT.replace("/opt/exradius/runtime", self.settings.runtime_mount, 1)

    async def run_probe(
        self, *, image: str, label: str, env: dict[str, str], timeout: float, runtime: RuntimeMount
    ) -> ContainerExit:
        """Run ``exradius-runner --probe <runtime>`` with a provider injected; no run directory."""
        container = await asyncio.to_thread(
            self._create,
            image=image,
            run_id=label,
            kind="probe",
            paths=None,
            env=env,
            entrypoint=[self.entrypoint],
            command=["--probe", runtime.name],
            runtime=runtime,
        )
        try:
            async with asyncio.timeout(timeout):
                return await self.wait(container.id)
        except TimeoutError:
            await self.remove_containers([container.id])
            return ContainerExit(exit_code=-1, logs=f"the probe did not finish within {timeout:.0f}s")

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

    async def docker_report(self) -> dict:
        """What the status page needs from this worker's Docker daemon."""

        def _report() -> dict:
            report = {"docker_ok": False, "docker_detail": None, "images": []}
            try:
                self.client.ping()
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                report["docker_detail"] = f"{type(exc).__name__}: {exc}"
                return report
            report["docker_ok"] = True
            report["docker_detail"] = "reachable"
            images = []
            for image in self.client.images.list():
                if (image.labels or {}).get(LABEL_ROLE) in INTERNAL_ROLES:
                    continue
                for tag in image.tags:
                    images.append(
                        {"tag": tag, "created_at": image.attrs.get("Created"), "size": image.attrs.get("Size")}
                    )
            report["images"] = sorted(images, key=lambda i: i["tag"])
            return report

        report = await asyncio.to_thread(_report)
        report["runtimes"] = await self.bundles.report() if report["docker_ok"] else [
            {"name": s.name, "version": s.version, "ready": False, "image": s.image, "error": "Docker is not reachable"}
            for s in self.bundles.status.values()
        ]
        return report

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
