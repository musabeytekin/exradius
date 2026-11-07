"""Run lifecycle end to end, with a fake container manager standing in for Docker."""

from __future__ import annotations

import io
import json
import tarfile
import uuid
from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy import select

from exradius import downloads
from exradius.crypto import hash_token
from exradius.models import Artifact, ImageBuild, InfoRequest, Run, SecretStore, SecretStoreEntry, Task, TranscriptChunk
from exradius.states import State
from exradius.worker.bundles import RuntimeMount
from exradius.worker.containers import ContainerExit, RunContainer, ServiceFailed, ServiceStarted, run_network_name
from exradius.worker.intake import accept_answer
from exradius.worker.runs import RunManager
from tests.conftest import BASE_CONFIG, commit_config

Behaviour = Callable[[uuid.UUID, dict], Awaitable[int]]


class FakeContainers:
    def __init__(self):
        self.behaviours: list[Behaviour] = []
        self.started: list[dict] = []
        self.setup_commands: list[str] = []
        self.setup_exit = 0
        self.stopped: list[str] = []
        self.removed: list[str] = []
        self.runtimes_ensured: list[str] = []
        self.existing: dict[str, list[RunContainer]] = {}  # containers left on the daemon
        self._pending: dict[str, tuple[Behaviour, uuid.UUID, dict]] = {}
        self.networks: set[str] = set()
        self.services: dict[str, dict[str, object]] = {}  # run label -> service name -> spec
        self.service_failures: dict[str, str] = {}  # service name -> reason
        self.service_log: list[tuple[str, str]] = []  # (event, detail)
        self.setup_networks: list[str | None] = []
        self.inspect_output = ""
        self.inspected: list[str] = []
        self.images: set[str] = set()
        self.builds: list[dict] = []
        self.build_ok = True
        self.setup_images: list[str] = []
        self.setup_limits: list[tuple] = []  # (memory, caches) per setup command

    async def ensure_runtime(self, runtime):
        self.runtimes_ensured.append(runtime)
        return RuntimeMount(runtime, "0.1.0", f"exradius-runtime-{runtime}-0.1.0-test")

    async def start_runner(self, *, image, run_id, paths, env, runtime, service_network=None, git_env=None,
                           memory=None, caches=()):
        assert paths.project.is_dir()
        cid = f"c{len(self.started)}"
        env = {**env, **(git_env or {})}  # what the container sees
        self.started.append({"image": image, "run_id": run_id, "env": env, "paths": paths, "runtime": runtime,
                             "service_network": service_network, "memory": memory, "caches": list(caches)})
        self._pending[cid] = (self.behaviours.pop(0), uuid.UUID(run_id), env)
        return cid

    async def image_exists(self, tag):
        return tag in self.images

    async def build_image(self, context, *, dockerfile, tag, labels=None):
        with tarfile.open(fileobj=io.BytesIO(context)) as archive:
            files = {m.name for m in archive.getmembers()}
        self.builds.append({"dockerfile": dockerfile, "tag": tag, "files": files})
        if not self.build_ok:
            return False, "Step 1/1 : FROM nothing\npull access denied for nothing\n"
        self.images.add(tag)
        return True, "Successfully built\n"

    async def run_setup_command(self, *, image, run_id, paths, env, command, service_network=None, memory=None,
                                caches=()):
        self.setup_commands.append(command)
        self.setup_limits.append((memory, list(caches)))
        self.setup_images.append(image)
        self.setup_networks.append(service_network)
        return ContainerExit(self.setup_exit, "setup output\nboom" if self.setup_exit else "ok")

    async def wait(self, container_id):
        behaviour, run_id, env = self._pending.pop(container_id)
        return ContainerExit(await behaviour(run_id, env), "runner log line")

    async def stop_run_containers(self, run_id):
        self.stopped.append(run_id)

    async def list_run_containers(self, run_id):
        return list(self.existing.get(run_id, []))

    async def remove_containers(self, container_ids):
        self.removed.extend(container_ids)
        for run_id, containers in self.existing.items():
            self.existing[run_id] = [c for c in containers if c.id not in container_ids]

    async def run_network(self, run_id):
        name = run_network_name(run_id)
        return name if name in self.networks else None

    async def create_run_network(self, run_id):
        self.networks.add(run_network_name(run_id))
        self.service_log.append(("network", run_id))
        return run_network_name(run_id)

    async def start_service(self, *, run_id, network, service, timeout=120):
        assert network == run_network_name(run_id) and network in self.networks
        running = self.services.setdefault(run_id, {})
        if service.name in running:
            return ServiceStarted(service.name, f"s-{service.name}", 0.0, "already running")
        if service.name in self.service_failures:
            raise ServiceFailed(f"service {service.name} ({service.image}) {self.service_failures[service.name]}\nlog tail")
        running[service.name] = service
        self.service_log.append(("start", service.name))
        return ServiceStarted(service.name, f"s-{service.name}", 0.5, "running")

    async def remove_run_services(self, run_id):
        removed = self.services.pop(run_id, {})
        self.networks.discard(run_network_name(run_id))
        self.service_log.append(("remove", run_id))
        return len(removed)

    async def service_run_ids(self):
        return set(self.services) | {n.removeprefix("exradius-run-") for n in self.networks}

    async def inspect_clones(self, *, image, run_id, paths):
        self.inspected.append(run_id)
        return ContainerExit(0, self.inspect_output)

    def leave_running(self, run_id: uuid.UUID, container_id: str, behaviour: Behaviour, kind: str = "run"):
        """A container that outlived its worker; waiting on it runs ``behaviour``."""
        self.existing.setdefault(str(run_id), []).append(RunContainer(container_id, kind, True))
        self._pending[container_id] = (behaviour, run_id, {})


@pytest.fixture
def containers():
    return FakeContainers()


@pytest.fixture
def published():
    return []


@pytest.fixture
def manager(settings, sessions, store, containers, published):
    async def emit(events):
        published.extend(e.envelope for e in events)

    return RunManager(settings, sessions, store, containers, emit)


def states(published) -> list[str]:
    return [e.payload["state"] for e in published if e.type == "status"]


def results(published):
    return [e.payload for e in published if e.type == "result"]


async def submit_task(accept, published, task_id="T-1", **extra) -> uuid.UUID:
    body = {"version": 1, "task_id": task_id, "project": "billing-platform", "agent": "bug-fixer",
            "instructions": "Fix it.", **extra}
    intake = await accept(json.dumps(body).encode())
    published.extend(e.envelope for e in intake.events)
    assert intake.run_id
    return intake.run_id


def runner(sessions, *, status="completed", summary="All fixed.", question=None, answer_early=False,
           error=None, exit_code=0, check=None, strict=True) -> Behaviour:
    """Simulate what the runner reports over the internal API."""

    async def behaviour(run_id: uuid.UUID, env: dict) -> int:
        async with sessions() as session:
            run = await session.get(Run, run_id)
            if strict:
                assert run.token_hash == hash_token(env["EXRADIUS_RUN_TOKEN"])
                assert run.state == State.RUNNING
            if check:
                check(run, env)
            seq = await session.scalar(select(TranscriptChunk.seq).where(TranscriptChunk.run_id == run_id).order_by(TranscriptChunk.seq.desc())) or 0
            session.add(TranscriptChunk(run_id=run_id, seq=seq + 1, payload={"type": "AssistantMessage", "n": seq + 1}))
            if question:
                request = InfoRequest(run_id=run_id, question=question)
                session.add(request)
                await session.flush()
                if answer_early:
                    request.answer = "early answer"
                    request.answered_at = request.created_at
            if status:
                run.reported_status = status
                run.runtime_session_id = "sess-1"
                run.summary = summary
                run.output = {"fixed": True} if status == "completed" else None
                run.error = error
            await session.commit()
        return exit_code

    return behaviour


async def set_config(configured, extra: str) -> str:
    return commit_config(configured["remote"], {"exradius.yaml": BASE_CONFIG + extra})


async def add_store(sessions, box, name: str, **values: str) -> None:
    async with sessions() as session:
        secret_store = SecretStore(name=name)
        session.add(secret_store)
        await session.flush()
        for key, value in values.items():
            session.add(SecretStoreEntry(store_id=secret_store.id, key=key, value_encrypted=box.encrypt(value)))
        await session.commit()


async def test_completed_run(manager, sessions, store, containers, published, settings, configured, box, accept):
    commit = await set_config(configured, (
        "secret_store: billing\n"
        "env:\n  LOG_LEVEL: debug\n  DB_URL: postgres://app:${secret:DB_PASSWORD}@db/app\n"
    ))
    await add_store(sessions, box, "billing", DB_PASSWORD="hunter2", OTHER="x")

    run_id = await submit_task(accept, published)
    containers.behaviours.append(runner(sessions))
    await manager.execute(run_id)

    assert states(published) == ["queued", "preparing", "running", "completed"]
    [result] = results(published)
    assert result["state"] == "completed"
    assert result["summary"] == "All fixed."
    assert result["output"] == {"fixed": True}
    assert result["error"] is None
    path = f"/api/runs/{run_id}/transcript"
    assert result["transcript_url"] == f"{settings.public_api_url}{path}?sig={downloads.sign(settings, path)}"
    transcript = store.objects[f"runs/{run_id}/transcript.jsonl"][0].decode().splitlines()
    assert json.loads(transcript[0])["type"] == "AssistantMessage"

    env = containers.started[0]["env"]
    assert env["ANTHROPIC_API_KEY"] == "sk-test"
    assert env["LOG_LEVEL"] == "debug" and env["DB_URL"] == "postgres://app:hunter2@db/app"
    assert "DB_PASSWORD" not in env and "OTHER" not in env  # used by reference only
    assert env["GIT_AUTHOR_NAME"] == "Bug Fixer Bot" and env["EXRADIUS_AGENT"] == "bug-fixer"
    assert env["EXRADIUS_API_URL"] == settings.run_api_url and env["EXRADIUS_RUN_ID"] == str(run_id)
    assert containers.started[0]["image"] == settings.project_default_image
    assert containers.started[0]["runtime"].volume == "exradius-runtime-claude_code-0.1.0-test"

    async with sessions() as session:
        run = await session.get(Run, run_id)
        assert run.state == State.COMPLETED and run.ended_at is not None
        assert run.token_hash is None  # revoked
        assert run.config_commit == commit
        assert run.config["env"]["DB_URL"] == "postgres://app:${secret:DB_PASSWORD}@db/app"  # as written
        assert run.agent_snapshot == {
            "slug": "bug-fixer", "name": "Bug fixer", "model": None, "provider": None, "runtime": "claude_code",
            "identity": {"name": "Bug Fixer Bot", "email": "bug-fixer@agents.example"}, "connections": ["docs"],
        }
        assert (await session.get(Task, "T-1")).state == State.COMPLETED
    assert not containers.started[0]["paths"].root.exists()  # run directory deleted


async def test_config_is_read_in_preparing(manager, sessions, store, containers, published, configured, accept):
    """A config that stops running between intake and preparing fails the run with the reason."""
    run_id = await submit_task(accept, published)
    await set_config(configured, "env:\n  X: ${secret:GONE}\nsecret_store: nowhere\n")
    await manager.execute(run_id)
    assert containers.started == []
    [result] = results(published)
    assert result["state"] == "failed" and result["error"] == "PreparationError: secret store nowhere does not exist"


async def test_agent_removed_before_preparing(manager, sessions, store, containers, published, configured, accept):
    run_id = await submit_task(accept, published, agent="scout")
    commit_config(configured["remote"], {"exradius.yaml": BASE_CONFIG.replace("  scout:\n    role: You investigate.\n", "")})
    await manager.execute(run_id)
    [result] = results(published)
    assert result["error"] == "PreparationError: agent 'scout' is no longer defined in project 'billing-platform'"


async def test_workstation_is_built_from_the_config_repository(
    manager, sessions, store, containers, published, configured, accept
):
    await set_config(configured, "workstation:\n  dockerfile: docker/Dockerfile\n  setup: [make deps]\n")
    commit_config(configured["remote"], {"docker/Dockerfile": "FROM ubuntu:24.04\n", "docker/tools.sh": "echo\n"})
    run_id = await submit_task(accept, published)
    containers.behaviours.append(runner(sessions))
    await manager.execute(run_id)

    [build] = containers.builds
    assert build["dockerfile"] == "Dockerfile" and {"Dockerfile", "tools.sh"} <= build["files"]
    assert build["tag"].startswith("exradius/billing-platform/workstation:") and len(build["tag"].rsplit(":", 1)[1]) == 12
    assert containers.started[0]["image"] == build["tag"]
    assert containers.setup_images == [build["tag"]]
    async with sessions() as session:
        [row] = (await session.scalars(select(ImageBuild))).all()
        assert (row.name, row.state, row.image) == ("workstation", "succeeded", build["tag"])

    # Same context: no second build, even though the commit changed.
    commit_config(configured["remote"], {"README": "unrelated\n"})
    run_id = await submit_task(accept, published, task_id="T-2")
    containers.behaviours.append(runner(sessions))
    await manager.execute(run_id)
    assert len(containers.builds) == 1 and containers.started[1]["image"] == build["tag"]


async def test_failed_build_fails_the_run(manager, sessions, store, containers, published, configured, accept):
    await set_config(configured, "workstation:\n  dockerfile: Dockerfile\n")
    commit_config(configured["remote"], {"Dockerfile": "FROM nothing\n"})
    containers.build_ok = False
    run_id = await submit_task(accept, published)
    await manager.execute(run_id)
    [result] = results(published)
    assert result["state"] == "failed"
    assert result["error"].startswith("PreparationError: building workstation from Dockerfile failed:\n")
    assert "pull access denied" in result["error"]
    async with sessions() as session:
        assert (await session.scalar(select(ImageBuild))).state == "failed"


async def test_repositories_and_attachments_are_prepared(
    manager, sessions, store, containers, published, configured, remote_repo, accept
):
    await set_config(configured, f"repositories:\n  - {{name: app, url: {remote_repo}, branch: feature}}\n")

async def test_info_request_waits_and_resumes(manager, sessions, store, containers, published, configured, accept):
    run_id = await submit_task(accept, published)
    containers.behaviours.append(runner(sessions, status="waiting_for_input", question="Which region?"))
    await manager.execute(run_id)

    assert states(published)[-1] == "waiting_for_input"
    [info] = [e for e in published if e.type == "info_request"]
    assert info.payload["question"] == "Which region?"
    assert results(published) == []
    paths = containers.started[0]["paths"]
    assert paths.root.exists()  # kept while waiting

    async with sessions() as session:
        assert (await session.get(Run, run_id)).token_hash is None  # no container, no token

    answer = json.dumps({"version": 1, "task_id": "T-1", "request_id": info.payload["request_id"],
                         "answer": "eu-west-1"}).encode()
    intake = await accept_answer(sessions, store, answer)
    assert intake.run_id == run_id

    def check(run, env):
        assert run.resume_request_ids == [info.payload["request_id"]]

    containers.behaviours.append(runner(sessions, check=check))
    await manager.resume(run_id)

    assert states(published) == [
        "queued", "preparing", "running", "waiting_for_input", "resuming", "running", "completed",
    ]
    assert results(published)[0]["state"] == "completed"
    assert not paths.root.exists()
    async with sessions() as session:
        chunks = (await session.scalars(select(TranscriptChunk.seq).where(TranscriptChunk.run_id == run_id))).all()
        assert sorted(chunks) == [1, 2]  # one transcript across both containers


async def test_answer_before_container_exit_resumes_immediately(
    manager, sessions, store, containers, published, configured, accept):
    run_id = await submit_task(accept, published)
    containers.behaviours.append(runner(sessions, status="waiting_for_input", question="Q?", answer_early=True))
    containers.behaviours.append(runner(sessions))
    await manager.execute(run_id)
    assert states(published)[-4:] == ["waiting_for_input", "resuming", "running", "completed"]


async def test_setup_command_failure_fails_the_run(manager, sessions, store, containers, published, configured, accept):
    await set_config(configured, "workstation:\n  setup: [make deps]\n")
    containers.setup_exit = 2
    run_id = await submit_task(accept, published)
    await manager.execute(run_id)

    assert containers.setup_commands == ["make deps"]
    assert containers.started == []
    assert states(published)[-1] == "failed"
    [result] = results(published)
    assert result["state"] == "failed" and "make deps" in result["error"] and "boom" in result["error"]


async def test_runner_crash_without_outcome(manager, sessions, store, containers, published, configured, accept):
    run_id = await submit_task(accept, published)
    containers.behaviours.append(runner(sessions, status=None, exit_code=137))
    await manager.execute(run_id)
    [result] = results(published)
    assert result["state"] == "failed" and "137" in result["error"]


async def test_runtime_failure_is_reported(manager, sessions, store, containers, published, configured, accept):
    run_id = await submit_task(accept, published)
    containers.behaviours.append(runner(sessions, status="failed", error="invalid api key", exit_code=1))
    await manager.execute(run_id)
    [result] = results(published)
    assert result["state"] == "failed" and result["error"] == "invalid api key"


async def test_cancel_waiting_run_frees_directory(manager, sessions, store, containers, published, configured, accept):
    run_id = await submit_task(accept, published)
    containers.behaviours.append(runner(sessions, status="waiting_for_input", question="Q?"))
    await manager.execute(run_id)
    paths = containers.started[0]["paths"]

    await manager.cancel(run_id)
    assert states(published)[-1] == "cancelled"
    [result] = results(published)
    assert result["state"] == "cancelled" and result["error"] is None
    assert not paths.root.exists()

    await manager.cancel(run_id)  # idempotent
    assert len(results(published)) == 1


async def test_cancel_while_running(manager, sessions, store, containers, published, configured, accept):
    run_id = await submit_task(accept, published)
    base = runner(sessions, strict=False)

    async def cancelled_mid_run(rid, env):
        await manager.cancel(rid)  # admin cancels; the worker stops the container
        await base(rid, env)  # the runner still reports before it dies
        return 137

    containers.behaviours.append(cancelled_mid_run)
    await manager.execute(run_id)

    assert containers.stopped == [str(run_id)]
    assert states(published) == ["queued", "preparing", "running", "cancelled"]
    assert [r["state"] for r in results(published)] == ["cancelled"]  # exactly one result


async def test_execute_ignores_runs_that_are_not_queued(manager, sessions, store, containers, published, configured, accept):
    run_id = await submit_task(accept, published)
    await manager.cancel(run_id)
    await manager.execute(run_id)
    assert containers.started == []
    assert [r["state"] for r in results(published)] == ["cancelled"]


async def test_resumable_runs(manager, sessions, store, containers, published, configured, accept):
    run_id = await submit_task(accept, published)
    containers.behaviours.append(runner(sessions, status="waiting_for_input", question="Q?"))
    await manager.execute(run_id)
    assert await manager.resumable_runs() == []
    async with sessions() as session:
        request = await session.scalar(select(InfoRequest))
        request.answer, request.answered_at = "A", request.created_at
        await session.commit()
    assert await manager.resumable_runs() == [run_id]


async def test_result_links_artifacts_by_signed_http_url(
    manager, sessions, store, containers, published, settings, configured, accept):
    settings.public_api_url = "https://exradius.example.com/"
    run_id = await submit_task(accept, published)
    base = runner(sessions)

    async def with_artifact(rid, env):
        async with sessions() as session:
            session.add(Artifact(run_id=rid, name="report.md", content_type="text/markdown", object_key="runs/x/report.md"))
            await session.commit()
        return await base(rid, env)

    containers.behaviours.append(with_artifact)
    await manager.execute(run_id)
    [result] = results(published)
    async with sessions() as session:
        artifact = await session.scalar(select(Artifact))
    [ref] = result["artifacts"]
    path = f"/api/artifacts/{artifact.id}/content"
    assert ref == {
        "name": "report.md",
        "content_type": "text/markdown",
        "url": f"https://exradius.example.com{path}?sig={downloads.sign(settings, path)}",
    }
    assert result["transcript_url"].startswith(f"https://exradius.example.com/api/runs/{run_id}/transcript?sig=")
