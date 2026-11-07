"""docs/run-environment.md §4-§6: declared memory (reserved, enforced, diagnosed), per-project
caches, and service commands."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from exradius.models import ProjectVerification, Run
from exradius.schemas.project_config import format_memory, memory_bytes, parse_config
from exradius.states import State
from exradius.worker import caches, prepare
from exradius.worker.bundles import RuntimeMount
from exradius.worker.containers import ContainerExit, ContainerManager, ServiceFailed, ServiceSpec
from exradius.worker.diagnose import diagnose_steps
from exradius.worker.memory import Abandoned, MemoryGate, TooLarge
from exradius.worker.verify import ProjectVerifier
from tests.conftest import BASE_CONFIG, commit_config
from tests.test_api import admin, client, control, make_run, setup_configuration, sync_db  # noqa: F401
from tests.test_environment_and_services import docker_fake, docker_manager  # noqa: F401
from tests.test_run_manager import containers, manager, published, results, runner, states, submit_task  # noqa: F401

GiB = 1024**3
MINIMAL = "version: 1\nproject: {slug: p}\nagents:\n  a: {}\n"

DECLARED = BASE_CONFIG + """\
resources:
  memory: 6g
workstation:
  setup: [pnpm install]
  cache:
    - ~/.local/share/pnpm/store
    - /var/cache/apt-lists
services:
  minio:
    image: minio/minio
    command: [server, /data]
    memory: 512m
"""


def errors(text: str) -> list[str]:
    result = parse_config(text)
    assert result.config is None
    return result.errors


# The config ---------------------------------------------------------------------------------


def test_resources_cache_and_command_parse():
    config = parse_config(DECLARED).config
    assert config.resources.memory == "6g" and config.workstation_memory == 6 * GiB
    assert config.services["minio"].command == ["server", "/data"] and config.services["minio"].memory == "512m"
    assert config.memory_reservation() == 6 * GiB + 512 * 1024**2
    assert config.workstation.cache == ["~/.local/share/pnpm/store", "/var/cache/apt-lists"]
    plain = parse_config(MINIMAL).config
    assert plain.resources.memory is None and plain.memory_reservation() == 0 and plain.workstation.cache == []


def test_memory_sizes():
    assert memory_bytes("512m") == 512 * 1024**2 and memory_bytes("1.5G") == int(1.5 * GiB)
    assert format_memory(6 * GiB) == "6g" and format_memory(1536 * 1024**2) == "1.5g"
    assert format_memory(512 * 1024**2) == "512m"
    for value, message in (("6", "must be a size with a unit"), ("6gb", "must be a size with a unit"),
                           ("1k", "must be at least 6m"), ("lots", "must be a size with a unit")):
        assert errors(MINIMAL + f"resources: {{memory: {value}}}\n") == [f"resources.memory: {message}, such as 512m or 6g"
                                                                         if "unit" in message else f"resources.memory: {message}"]
    assert errors(MINIMAL + "resources: {cpu: 2}\n") == ["resources.cpu: unknown key"]
    assert errors(MINIMAL + "services:\n  db: {image: postgres, memory: 512}\n") == [
        "services.db.memory: must be a size with a unit, such as 512m or 6g"]


@pytest.mark.parametrize("entry,message", [
    ("cache", "must start with ~/ (the home directory) or / (an absolute path)"),
    ("~", "must start with ~/ (the home directory) or / (an absolute path)"),
    ("~/", "must name a directory below ~ or /, not the home or the root itself"),
    ("/work/node_modules", "must be outside /work"),
    ("/work", "must be outside /work"),
    ("/exradius/state/x", "/exradius is reserved"),
    ("/etc/passwd", "/etc/passwd is reserved"),
    ("~/../etc", "must not use '..'"),
    ("/tmp", "must name a directory below /tmp, not /tmp itself"),
])
def test_cache_paths_are_checked(entry, message):
    assert errors(MINIMAL + f"workstation:\n  cache: ['{entry}']\n") == [f"workstation.cache[0]: {message}"]


def test_cache_entries_are_unique_and_commands_not_empty():
    assert errors(MINIMAL + "workstation:\n  cache: [~/.m2, ~/.m2/]\n") == ["workstation.cache[1]: '~/.m2/' is listed twice"]
    assert errors(MINIMAL + "services:\n  db: {image: postgres, command: []}\n") == [
        "services.db.command: List should have at least 1 item after validation, not 0"]


# Cache directories -----------------------------------------------------------------------------


def test_cache_mounts_and_mount_points(tmp_path):
    mounts = caches.mounts(tmp_path, "plane", ["~/.local/share/pnpm/store", "/var/cache/apt-lists"], "/home/runner")
    assert mounts == [
        caches.CacheMount(tmp_path / "caches/plane/0-local_share_pnpm_store", "/home/runner/.local/share/pnpm/store"),
        caches.CacheMount(tmp_path / "caches/plane/1-var_cache_apt-lists", "/var/cache/apt-lists"),
    ]
    home = tmp_path / "home"
    home.mkdir()
    caches.ensure(mounts, home, "/home/runner", 1000, 1000)
    assert all(m.source.is_dir() for m in mounts)
    # The mount point inside the home exists before Docker would create it as root.
    assert (home / ".local/share/pnpm/store").is_dir()
    (mounts[0].source / "pkg").write_bytes(b"x" * 10_000)
    assert caches.project_size(tmp_path, "plane") >= 10_000
    assert caches.project_size(tmp_path, "other") is None
    caches.remove(tmp_path, "plane")
    assert not (tmp_path / "caches/plane").exists()


async def test_cache_sizes_are_kept_for_a_while(tmp_path):
    now = {"t": 0.0}
    memo = caches.SizeMemo(ttl=60, clock=lambda: now["t"])
    target = tmp_path / "caches" / "p" / "0-x"
    target.mkdir(parents=True)
    (target / "a").write_bytes(b"a" * 8192)
    first = await memo.get(tmp_path, "p")  # never measured: waits for the walk
    assert first >= 8192
    (target / "b").write_bytes(b"b" * 65536)
    assert await memo.get(tmp_path, "p") == first  # young: as it was
    now["t"] = 61
    assert await memo.get(tmp_path, "p") == first  # old: returned at once, refreshed behind
    await asyncio.sleep(0.05)
    assert await memo.get(tmp_path, "p") >= first + 65536
    memo.forget(tmp_path, "p")
    assert await memo.get(tmp_path, "nothing") is None


# The memory gate ----------------------------------------------------------------------------------


async def test_reservations_wait_until_they_fit():
    gate = MemoryGate(budget=8 * GiB)
    order, waited = [], []

    async def run(name, amount, hold):
        async def on_wait():
            waited.append(name)

        async with gate.reserve(amount, on_wait=on_wait):
            order.append(f"{name} start")
            await hold.wait()
        order.append(f"{name} end")

    first, second = asyncio.Event(), asyncio.Event()
    a = asyncio.create_task(run("a", 6 * GiB, first))
    await asyncio.sleep(0)
    b = asyncio.create_task(run("b", 4 * GiB, second))
    async with gate.reserve(0):  # undeclared: never waits
        pass
    await asyncio.sleep(0.01)
    assert order == ["a start"] and waited == ["b"] and gate.in_use == 6 * GiB
    first.set()
    await asyncio.sleep(0.01)
    assert order == ["a start", "a end", "b start"] and gate.in_use == 4 * GiB
    second.set()
    await asyncio.gather(a, b)
    assert gate.in_use == 0

    with pytest.raises(TooLarge, match=r"declares 9g of memory, more than this worker's budget for runs \(8g"):
        async with gate.reserve(9 * GiB):
            pass
    async with gate.reserve(9 * GiB, wait=False):  # a running container after a restart
        assert gate.in_use == 9 * GiB


async def test_a_waiter_that_is_no_longer_wanted_gives_up(monkeypatch):
    from exradius.worker import memory

    monkeypatch.setattr(memory, "RECHECK_SECONDS", 0.01)
    gate = MemoryGate(budget=GiB)
    async with gate.reserve(GiB):
        with pytest.raises(Abandoned):
            async with gate.reserve(GiB, still_wanted=lambda: _value(False)):
                pass
    assert gate.in_use == 0


async def _value(value):
    return value


# Runs ------------------------------------------------------------------------------------------


@pytest.fixture
async def declared(configured):
    commit_config(configured["remote"], {"exradius.yaml": DECLARED})
    return configured["project"]


async def test_run_gets_its_limit_caches_and_service_command(manager, sessions, containers, published, declared,
                                                              accept, settings):
    run_id = await submit_task(accept, published)
    containers.behaviours.append(runner(sessions))
    await manager.execute(run_id, 6 * GiB + 512 * 1024**2)
    assert states(published)[-1] == "completed"
    started = containers.started[0]
    assert started["memory"] == 6 * GiB
    targets = [c.target for c in started["caches"]]
    assert targets == ["/home/runner/.local/share/pnpm/store", "/var/cache/apt-lists"]
    assert all(c.source.is_relative_to(settings.data_dir / "caches" / "billing-platform") for c in started["caches"])
    assert containers.setup_limits == [(6 * GiB, started["caches"])]
    [minio] = containers.services[str(run_id)].values() if str(run_id) in containers.services else [None]
    assert minio is None  # removed at finalize; the spec is checked below
    spec = manager.service_specs(parse_config(DECLARED).config, SimpleNamespace(services={"minio": "minio/minio"}), {})[0]
    assert spec.command == ["server", "/data"] and spec.memory == 512 * 1024**2 and spec.memory_text == "512m"
    assert manager.memory.in_use == 0


async def test_a_run_waits_for_memory_in_queued(manager, sessions, containers, published, declared, accept):
    manager.memory.budget = 8 * GiB
    run_id = await submit_task(accept, published)
    containers.behaviours.append(runner(sessions))
    blocker = asyncio.Event()

    async def hold():
        async with manager.memory.reserve(4 * GiB):
            await blocker.wait()

    held = asyncio.create_task(hold())
    await asyncio.sleep(0)
    execution = asyncio.create_task(manager.execute(run_id, 6 * GiB))
    for _ in range(100):
        await asyncio.sleep(0.01)
        async with sessions() as session:
            run = await session.get(Run, run_id, populate_existing=True)
            if run.waiting_for:
                break
    assert run.state == State.QUEUED and run.waiting_for == "memory" and run.worker_id == manager.worker_id
    blocker.set()
    await asyncio.gather(held, execution)
    async with sessions() as session:
        run = await session.get(Run, run_id, populate_existing=True)
    assert run.state == State.COMPLETED and run.waiting_for is None


async def test_cancelling_a_waiting_run_stops_the_wait(manager, sessions, containers, published, declared, accept,
                                                         monkeypatch):
    from exradius.worker import memory

    monkeypatch.setattr(memory, "RECHECK_SECONDS", 0.01)
    manager.memory.budget = 8 * GiB
    run_id = await submit_task(accept, published)
    async with manager.memory.reserve(4 * GiB):
        execution = asyncio.create_task(manager.execute(run_id, 6 * GiB))
        for _ in range(100):
            await asyncio.sleep(0.01)
            async with sessions() as session:
                if (await session.get(Run, run_id, populate_existing=True)).waiting_for:
                    break
        await manager.cancel(run_id)
        await asyncio.wait_for(execution, 5)  # gave up while the memory was still taken
    async with sessions() as session:
        run = await session.get(Run, run_id, populate_existing=True)
    assert run.state == State.CANCELLED and run.waiting_for is None and containers.started == []
    assert manager.memory.in_use == 0


async def test_too_large_a_declaration_fails_the_run(manager, sessions, containers, published, declared, accept):
    manager.memory.budget = 4 * GiB
    run_id = await submit_task(accept, published)
    await manager.execute(run_id, 6 * GiB)
    assert states(published)[-1] == "failed"
    assert "more than this worker's budget for runs (4g" in results(published)[-1]["error"]
    assert containers.started == []


async def test_exit_137_under_a_declared_limit_names_it(manager, sessions, containers, published, declared, accept):
    run_id = await submit_task(accept, published)

    async def killed(rid, env):
        return 137

    containers.behaviours.append(killed)
    await manager.execute(run_id, 6 * GiB)
    error = results(published)[-1]["error"]
    assert error.startswith("the run exceeded its declared memory (6g) and was killed (exit 137)")


async def test_setup_killed_under_a_declared_limit(manager, sessions, containers, published, declared, accept):
    containers.setup_exit = 137
    run_id = await submit_task(accept, published)
    await manager.execute(run_id, 6 * GiB)
    assert results(published)[-1]["error"].startswith(
        "PreparationError: setup command 'pnpm install' exceeded its declared memory (6g) and was killed")


async def test_restarted_worker_takes_over_waiting_runs(manager, sessions, published, declared, accept):
    manager.worker_id = "me"
    run_id = await submit_task(accept, published)
    async with sessions() as session:
        run = await session.get(Run, run_id)
        run.waiting_for, run.worker_id = "memory", "dead-worker"
        await session.commit()

    async def alive(worker):
        return False

    assert await manager.waiting_runs(alive) == [(run_id, 6 * GiB + 512 * 1024**2)]
    async with sessions() as session:
        assert (await session.get(Run, run_id)).worker_id == manager.worker_id

    async def other_alive(worker):
        return True

    manager.worker_id = "someone-else"
    assert await manager.waiting_runs(other_alive) == []


# Verify ----------------------------------------------------------------------------------------


async def test_verify_reserves_and_limits(manager, sessions, containers, declared):
    async with sessions() as session:
        verification = ProjectVerification(project_id=declared.id, state="pending", steps=[])
        session.add(verification)
        await session.commit()
        vid = verification.id
    manager.memory.budget = 16 * GiB
    seen = []

    async def setup(*, image, run_id, paths, env, command, service_network=None, memory=None, caches=()):
        seen.append((memory, [c.target for c in caches], manager.memory.in_use))
        return ContainerExit(137, "Killed")

    containers.run_setup_command = setup
    await ProjectVerifier(manager).run(vid)
    async with sessions() as session:
        verification = await session.get(ProjectVerification, vid)
    names = [s["name"] for s in verification.steps]
    assert names[1] == "Reserve memory (6.5g)"
    assert verification.steps[1]["log"] == "reserved 6.5g of the worker's 16g" and verification.steps[1]["state"] == "passed"
    assert seen == [(6 * GiB, ["/home/runner/.local/share/pnpm/store", "/var/cache/apt-lists"], 6 * GiB + 512 * 1024**2)]
    assert verification.state == "failed"
    assert verification.diagnosis == ("`pnpm install` exceeded the workstation's declared memory (6g) and was killed: "
                                      "raise `resources.memory` in the config, or lower the command's parallelism.")
    assert manager.memory.in_use == 0  # released at the end


async def test_verify_with_too_small_a_budget(manager, sessions, containers, declared):
    async with sessions() as session:
        verification = ProjectVerification(project_id=declared.id, state="pending", steps=[])
        session.add(verification)
        await session.commit()
        vid = verification.id
    manager.memory.budget = 2 * GiB
    await ProjectVerifier(manager).run(vid)
    async with sessions() as session:
        verification = await session.get(ProjectVerification, vid)
    assert verification.steps[1]["state"] == "failed"
    assert verification.diagnosis.startswith("The project declares 6.5g of memory, more than this worker's budget")
    assert diagnose_steps(verification.steps) == verification.diagnosis


# Docker ----------------------------------------------------------------------------------------


async def test_containers_get_limits_caches_and_the_git_environment(settings):
    ran = []
    fake = SimpleNamespace(
        containers=SimpleNamespace(run=lambda image, **kw: ran.append(kw) or SimpleNamespace(id="c")),
        api=SimpleNamespace(create_endpoint_config=lambda aliases=None: {"Aliases": aliases}),
    )
    manager = ContainerManager(settings, client=fake)
    manager.wait = lambda cid: _value(ContainerExit(0, ""))
    paths = prepare.create_run_directory(settings.data_dir, "r3")
    mounts = caches.mounts(settings.data_dir, "plane", ["~/.cache/uv"], "/home/runner")
    git_env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": "/h",
               "GIT_AUTHOR_NAME": "Dev"}
    project_env = {"GIT_CONFIG_COUNT": "0", "GIT_CONFIG_PARAMETERS": "'core.hooksPath'=''", "GIT_AUTHOR_NAME": "X",
                   "LOG": "debug"}
    await manager.start_runner(image="img", run_id="r3", paths=paths, env=project_env,
                               runtime=RuntimeMount("claude_code", "0.1.0", "vol"), git_env=git_env,
                               memory=6 * GiB, caches=mounts)
    kw = ran[-1]
    assert kw["mem_limit"] == 6 * GiB and kw["memswap_limit"] == 6 * GiB
    by_target = {m["Target"]: m for m in kw["mounts"]}
    assert by_target["/home/runner/.cache/uv"]["Source"] == str(settings.data_dir / "caches/plane/0-cache_uv")
    assert (paths.home / ".cache" / "uv").is_dir()
    env = kw["environment"]
    assert env["GIT_CONFIG_COUNT"] == "1" and env["GIT_CONFIG_VALUE_0"] == "/h" and "GIT_CONFIG_PARAMETERS" not in env
    assert env["GIT_AUTHOR_NAME"] == "Dev" and env["LOG"] == "debug"

    await manager.run_setup_command(image="img", run_id="r3", paths=paths, env={}, command="true")
    kw = ran[-1]
    assert "mem_limit" not in kw and "memswap_limit" not in kw  # undeclared: unlimited
    assert kw["environment"]["GIT_CONFIG_KEY_0"] == "safe.directory" and kw["environment"]["GIT_CONFIG_COUNT"] == "1"


async def test_service_command_memory_and_kill(docker_manager, docker_fake):
    network = await docker_manager.create_run_network("run-9")
    spec = ServiceSpec("minio", "minio/minio", command=["server", "/data"], memory=512 * 1024**2, memory_text="512m")
    started = await docker_manager.start_service(run_id="run-9", network=network, service=spec)
    kwargs = docker_fake.container_store[started.container_id].kwargs
    assert kwargs["command"] == ["server", "/data"]
    assert kwargs["mem_limit"] == kwargs["memswap_limit"] == 512 * 1024**2
    plain = await docker_manager.start_service(run_id="run-9", network=network, service=ServiceSpec("redis", "redis:7"))
    kwargs = docker_fake.container_store[plain.container_id].kwargs
    assert kwargs["command"] is None and "mem_limit" not in kwargs

    docker_fake.behaviour["big/db"] = {"statuses": [{"Status": "exited", "Running": False, "ExitCode": 137}]}
    with pytest.raises(ServiceFailed, match=r"service db \(big/db\) exceeded its declared memory \(512m\)"):
        await docker_manager.start_service(run_id="run-9", network=network,
                                           service=ServiceSpec("db", "big/db", memory=512 * 1024**2, memory_text="512m"))
    from exradius.worker.diagnose import diagnose

    assert diagnose("Start service db", "service db (big/db) exceeded its declared memory (512m): exited with code 137") == (
        "Service db exceeded its declared memory (512m) and was killed: raise its `memory` in the config.")


# The panel ----------------------------------------------------------------------------------------


def test_project_page_shows_the_cache_size(admin, tmp_path):
    project = setup_configuration(admin, tmp_path)["project"]
    assert project["cache_bytes"] is None
    data_dir = admin.app.state.exradius.settings.data_dir
    target = data_dir / "caches" / "billing-platform" / "0-cache_uv"
    target.mkdir(parents=True)
    (target / "wheel").write_bytes(b"w" * 50_000)
    assert admin.get(f"/api/projects/{project['id']}").json()["cache_bytes"] >= 50_000
    assert admin.get("/api/projects").json()[0]["cache_bytes"] is None  # the list does not walk caches
    assert admin.delete(f"/api/projects/{project['id']}").status_code == 204
    assert not (data_dir / "caches" / "billing-platform").exists()


def test_task_rows_say_waiting_for_memory(admin, sync_db, tmp_path):
    task_id, run_id = make_run(admin, sync_db, tmp_path, state="queued", with_token=None)
    sync_db.execute(Run.__table__.update().where(Run.id == uuid.UUID(run_id)).values(waiting_for="memory"))
    sync_db.commit()
    [row] = admin.get("/api/tasks").json()
    assert row["waiting_for"] == "memory" and row["state"] == "queued"
    detail = admin.get(f"/api/tasks/{task_id}").json()
    assert detail["waiting_for"] == "memory" and detail["run"]["waiting_for"] == "memory"
