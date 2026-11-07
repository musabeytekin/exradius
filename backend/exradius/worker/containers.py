"""Run container control through the Docker SDK (architecture §5.2, §9)."""

from __future__ import annotations

import asyncio
import io
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import docker
from docker.errors import APIError, NotFound
from docker.types import Mount

from exradius.config import Settings
from exradius.worker.bundles import ENTRYPOINT, LABEL_ROLE, ROLE_BUNDLE, RuntimeBundles, RuntimeMount
from exradius.worker import prepare
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
LABEL_SERVICE = "exradius.service"
KIND_SERVICE = "service"
KIND_NETWORK = "network"
NETWORK_PREFIX = "exradius-run-"
SERVICE_CAPS = ["CHOWN", "SETUID", "SETGID", "FOWNER", "DAC_OVERRIDE", "NET_BIND_SERVICE"]
"""What common server images need on top of nothing (docs/environment-and-services.md §2)."""
SERVICE_READY_TIMEOUT = 120
SERVICE_POLL_INTERVAL = 1.0
SERVICE_RUNNING_GRACE = 1.0
"""A service without a readiness check counts as ready once it has run this long."""
INSPECT_TIMEOUT = 60

INSPECT_CLONES_SCRIPT = r"""
cd /work || exit 0
for d in */; do
  name=${d%/}
  [ -e "$name/.git" ] || continue
  (
    cd "$name" || exit 0
    n=$(git --no-optional-locks status --porcelain 2>/dev/null | wc -l | tr -d ' ')
    printf 'dirty	%s	%s
' "$name" "$n"
    git for-each-ref --format='%(refname:short)' refs/heads 2>/dev/null | while read -r b; do
      c=$(git rev-list --count "refs/heads/$b" --not --remotes 2>/dev/null)
      printf 'unpushed	%s	%s	%s
' "$name" "$b" "$c"
    done
    if ! git symbolic-ref -q HEAD >/dev/null 2>&1; then
      c=$(git rev-list --count HEAD --not --remotes --branches 2>/dev/null)
      printf 'unpushed	%s		%s
' "$name" "$c"
    fi
  )
done
"""
"""Delivery check (docs/environment-and-services.md §4): per clone, the number of
uncommitted paths and, per local branch, the commits no remote-tracking ref contains.
It runs in a container, never in the worker: the clone's config is the agent's."""


def run_network_name(run_id: str) -> str:
    return f"{NETWORK_PREFIX}{run_id}"


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    image: str
    env: dict[str, str] = field(default_factory=dict)
    ready: dict | None = None


class ServiceFailed(Exception):
    """A service did not start or become ready; the message names it and has its log tail."""


@dataclass
class ServiceStarted:
    name: str
    container_id: str
    seconds: float
    check: str
    """How readiness was decided, for the verify log."""


def listening_ports(proc_net: str) -> set[int]:
    """Ports in LISTEN state (``0A``) in ``/proc/net/tcp`` and ``tcp6`` text."""
    ports = set()
    for line in proc_net.splitlines():
        fields = line.split()
        if len(fields) > 3 and fields[3] == "0A" and ":" in fields[1]:
            try:
                ports.add(int(fields[1].rsplit(":", 1)[1], 16))
            except ValueError:
                continue
    return ports


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
        service_network: str | None = None,
    ):
        """``paths=None`` creates a container without the run mounts (provider probes);
        ``runtime`` mounts that bundle read-only (runs and probes, not setup commands);
        ``service_network`` also joins the run's own network, where its services are."""
        uid, gid = self.settings.run_uid_gid
        mounts = self._mounts(paths) + self._identity_mounts(paths) if paths else []
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
            **self._network_args(service_network),
            labels={LABEL_RUN: run_id, LABEL_KIND: kind},
            name=f"exradius-{kind}-{run_id[:8]}-{_suffix()}",
        )

    def _network_args(self, service_network: str | None) -> dict:
        """The shared run network (the API is there) and, with services, the run's own."""
        networks = [n for n in (self.settings.run_network, service_network) if n]
        if len(networks) < 2:
            return {"network": networks[0] if networks else None}
        # Both at create (Docker API 1.44+), so the container never runs without either.
        return {
            "network": networks[0],
            "networking_config": {n: self.client.api.create_endpoint_config() for n in networks},
        }

    def _identity_mounts(self, paths: RunPaths) -> list[Mount]:
        """Generated ``/etc/passwd`` and ``/etc/group`` mapping the run uid/gid to ``runner``
        with home ``/home/runner`` (tools such as Java read the home from passwd)."""
        uid, gid = self.settings.run_uid_gid
        passwd, group = prepare.write_identity_files(paths, uid, gid, HOME_DIR)
        return [
            Mount(target="/etc/passwd", source=self.host_path(passwd), type="bind", read_only=True),
            Mount(target="/etc/group", source=self.host_path(group), type="bind", read_only=True),
        ]

    async def start_runner(
        self,
        *,
        image: str,
        run_id: str,
        paths: RunPaths,
        env: dict[str, str],
        runtime: RuntimeMount,
        service_network: str | None = None,
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
            service_network=service_network,
        )
        return container.id

    async def run_setup_command(
        self,
        *,
        image: str,
        run_id: str,
        paths: RunPaths,
        env: dict[str, str],
        command: str,
        service_network: str | None = None,
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
            service_network=service_network,
        )
        return await self.wait(container.id)

    async def inspect_clones(self, *, image: str, run_id: str, paths: RunPaths) -> ContainerExit:
        """Run the delivery check over ``/work`` mounted read-only, with no network, no
        variables and no secrets."""
        uid, gid = self.settings.run_uid_gid

        def _run():
            return self.client.containers.run(
                image,
                entrypoint=["/bin/sh", "-c"],
                command=[INSPECT_CLONES_SCRIPT],
                detach=True,
                environment={"HOME": HOME_DIR, "GIT_OPTIONAL_LOCKS": "0", **GIT_ENV},
                working_dir=WORK_DIR,
                mounts=[Mount(target=WORK_DIR, source=self.host_path(paths.project), type="bind", read_only=True)],
                user=f"{uid}:{gid}",
                read_only=True,
                tmpfs={"/tmp": "rw,mode=1777", HOME_DIR: f"rw,uid={uid},gid={gid},mode=0755"},
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                network_mode="none",
                labels={LABEL_RUN: run_id, LABEL_KIND: "inspect"},
                name=f"exradius-inspect-{run_id[:8]}-{_suffix()}",
            )

        container = await asyncio.to_thread(_run)
        try:
            async with asyncio.timeout(INSPECT_TIMEOUT):
                return await self.wait(container.id)
        except TimeoutError:
            await self.remove_containers([container.id])
            return ContainerExit(exit_code=-1, logs=f"the check did not finish within {INSPECT_TIMEOUT}s")

    # -- image builds (docs/config-repo.md §4) ---------------------------------------------

    async def image_exists(self, tag: str) -> bool:
        def _get() -> bool:
            try:
                self.client.images.get(tag)
                return True
            except docker.errors.ImageNotFound:
                return False

        return await asyncio.to_thread(_get)

    async def build_image(
        self, context: bytes, *, dockerfile: str, tag: str, labels: dict[str, str] | None = None
    ) -> tuple[bool, str]:
        """Build ``tag`` from a tar context; ``(ok, output)`` with the build's log."""

        def _build() -> tuple[bool, str]:
            lines: list[str] = []
            ok = True
            stream = self.client.api.build(
                fileobj=io.BytesIO(context), custom_context=True, dockerfile=dockerfile, tag=tag,
                rm=True, forcerm=True, decode=True, labels=labels or {},
            )
            for chunk in stream:
                if "stream" in chunk:
                    lines.append(chunk["stream"])
                elif "error" in chunk:
                    ok = False
                    lines.append(str(chunk.get("error")) + "\n")
                elif chunk.get("status") and "progress" not in chunk:
                    lines.append(" ".join(str(chunk[k]) for k in ("id", "status") if chunk.get(k)) + "\n")
            return ok, "".join(lines)

        try:
            return await asyncio.to_thread(_build)
        except (APIError, docker.errors.BuildError) as exc:
            return False, _api_error(exc)

    # -- services (docs/environment-and-services.md §2) ----------------------------------

    sleep = staticmethod(time.sleep)
    clock = staticmethod(time.monotonic)

    async def run_network(self, run_id: str) -> str | None:
        """The run's own network if it has one (it has services)."""

        def _get() -> str | None:
            try:
                return self.client.networks.get(run_network_name(run_id)).name
            except NotFound:
                return None

        return await asyncio.to_thread(_get)

    async def create_run_network(self, run_id: str) -> str:
        name = run_network_name(run_id)

        def _create() -> str:
            try:
                return self.client.networks.get(name).name
            except NotFound:
                pass
            network = self.client.networks.create(
                name, driver="bridge", labels={LABEL_RUN: run_id, LABEL_KIND: KIND_NETWORK}
            )
            return network.name

        return await asyncio.to_thread(_create)

    async def start_service(
        self, *, run_id: str, network: str, service: ServiceSpec, timeout: float = SERVICE_READY_TIMEOUT
    ) -> ServiceStarted:
        """Start a service on the run network under its name and wait until it is ready.
        Idempotent: a running container of that service is kept (resume)."""
        return await asyncio.to_thread(self._start_service, run_id, network, service, timeout)

    def _start_service(self, run_id: str, network: str, service: ServiceSpec, timeout: float) -> ServiceStarted:
        labels = {LABEL_RUN: run_id, LABEL_KIND: KIND_SERVICE, LABEL_SERVICE: service.name}
        existing = self.client.containers.list(
            all=True, filters={"label": [f"{LABEL_RUN}={run_id}", f"{LABEL_SERVICE}={service.name}"]}
        )
        for container in existing:
            if container.status == "running":
                return ServiceStarted(service.name, container.id, 0.0, "already running")
            self._remove(container)
        try:
            # run() pulls a missing image first; the readiness clock starts after it.
            container = self.client.containers.run(
                service.image,
                detach=True,
                environment=dict(service.env),
                hostname=service.name,
                network=network,
                networking_config={network: self.client.api.create_endpoint_config(aliases=[service.name])},
                cap_drop=["ALL"],
                cap_add=list(SERVICE_CAPS),
                security_opt=["no-new-privileges"],
                labels=labels,
                name=f"exradius-service-{run_id[:8]}-{service.name}-{_suffix()}",
            )
        except (APIError, docker.errors.ImageNotFound) as exc:
            raise ServiceFailed(f"service {service.name} ({service.image}) could not start: {_api_error(exc)}") from exc
        started = self.clock()
        check = self._readiness(container, service)
        try:
            self._wait_ready(container, service, check, started, timeout)
        except ServiceFailed as exc:
            raise ServiceFailed(f"service {service.name} ({service.image}) {exc}\n{self._log_tail(container)}") from exc
        return ServiceStarted(service.name, container.id, round(self.clock() - started, 1), check)

    def _readiness(self, container, service: ServiceSpec) -> str:
        ready = service.ready or {}
        if "tcp" in ready:
            return f"port {int(ready['tcp'])} listening"
        if "command" in ready:
            return "command " + " ".join(ready["command"])
        test = ((container.attrs.get("Config") or {}).get("Healthcheck") or {}).get("Test") or []
        if test and test[0] != "NONE":
            return "image healthcheck"
        return "running"

    def _wait_ready(self, container, service: ServiceSpec, check: str, started: float, timeout: float) -> None:
        ready = service.ready or {}
        last = ""
        while True:
            container.reload()
            state = container.attrs.get("State") or {}
            if state.get("Status") in ("exited", "dead") or (state and not state.get("Running", True)):
                raise ServiceFailed(f"exited with code {state.get('ExitCode')} before it was ready")
            if "tcp" in ready:
                code, output = _exec(container, ["cat", "/proc/net/tcp", "/proc/net/tcp6"])
                if int(ready["tcp"]) in listening_ports(output):
                    return
                last = "" if code == 0 else f" (last check: exit {code}: {output.strip()[:200]})"
            elif "command" in ready:
                code, output = _exec(container, list(ready["command"]))
                if code == 0:
                    return
                last = f" (last check: exit {code}: {output.strip()[-300:]})"
            elif check == "image healthcheck":
                health = (state.get("Health") or {}).get("Status")
                if health == "healthy":
                    return
                last = f" (health: {health})"
            elif self.clock() - started >= SERVICE_RUNNING_GRACE:
                return
            if self.clock() - started >= timeout:
                raise ServiceFailed(f"did not become ready within {timeout:.0f}s waiting for {check}{last}")
            self.sleep(SERVICE_POLL_INTERVAL)

    @staticmethod
    def _log_tail(container) -> str:
        try:
            return container.logs(tail=40).decode(errors="replace").strip()[-2000:]
        except (NotFound, APIError):
            return ""

    @staticmethod
    def _remove(container) -> None:
        try:
            container.remove(force=True, v=True)  # v: the anonymous volumes of VOLUME lines
        except NotFound:
            pass
        except APIError as exc:
            log.warning("could not remove %s: %s", container.id[:12], exc)

    async def remove_run_services(self, run_id: str) -> int:
        """Remove the run's services (with their anonymous volumes) and its network."""

        def _remove_all() -> int:
            services = self.client.containers.list(
                all=True, filters={"label": [f"{LABEL_RUN}={run_id}", f"{LABEL_KIND}={KIND_SERVICE}"]}
            )
            for container in services:
                self._remove(container)
            try:
                self.client.networks.get(run_network_name(run_id)).remove()
            except NotFound:
                pass
            except APIError as exc:
                log.warning("could not remove network %s: %s", run_network_name(run_id), exc)
            return len(services)

        return await asyncio.to_thread(_remove_all)

    async def service_run_ids(self) -> set[str]:
        """Run ids (labels) that own service containers or run networks, for the sweep."""

        def _list() -> set[str]:
            found = {
                c.labels.get(LABEL_RUN, "")
                for c in self.client.containers.list(all=True, filters={"label": f"{LABEL_KIND}={KIND_SERVICE}"})
            }
            found |= {
                (n.attrs.get("Labels") or {}).get(LABEL_RUN, "")
                for n in self.client.networks.list(filters={"label": f"{LABEL_KIND}={KIND_NETWORK}"})
            }
            return {run_id for run_id in found if run_id}

        return await asyncio.to_thread(_list)

    @property
    def entrypoint(self) -> str:
        return ENTRYPOINT.replace("/opt/exradius/runtime", self.settings.runtime_mount, 1)

    async def run_probe(
        self,
        *,
        image: str,
        label: str,
        env: dict[str, str],
        timeout: float,
        runtime: RuntimeMount,
        model: str | None = None,
    ) -> ContainerExit:
        """Run ``exradius-runner --probe <runtime> [--model <id>]`` with a provider injected;
        no run directory."""
        container = await asyncio.to_thread(
            self._create,
            image=image,
            run_id=label,
            kind="probe",
            paths=None,
            env=env,
            entrypoint=[self.entrypoint],
            command=["--probe", runtime.name, *(["--model", model] if model else [])],
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


def _exec(container, command: list[str]) -> tuple[int, str]:
    try:
        result = container.exec_run(command, demux=False)
    except APIError as exc:
        return -1, _api_error(exc)
    output = result.output.decode(errors="replace") if isinstance(result.output, bytes) else str(result.output or "")
    return int(result.exit_code if result.exit_code is not None else -1), output


def _api_error(exc: Exception) -> str:
    explanation = getattr(exc, "explanation", None)
    return str(explanation or exc)


def _suffix() -> str:
    import secrets

    return secrets.token_hex(3)
