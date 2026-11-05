"""Integration tests against real infrastructure. Skipped unless it is available:

- RabbitMQ: set ``EXRADIUS_TEST_BROKER_URL`` (e.g. ``amqp://exradius:exradius@localhost:5672/``).
- Docker: needs a reachable daemon, the default project image and the Claude Code bundle
  image (``docker compose -f deploy/docker-compose.yml build project-default runtime-claude-code``).
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from exradius.schemas.messages import EventEnvelope, status_payload
from exradius.states import State

BROKER_URL = os.environ.get("EXRADIUS_TEST_BROKER_URL")
PROJECT_IMAGE = os.environ.get("EXRADIUS_TEST_PROJECT_IMAGE", "exradius-project-default:0.1.0")
BUNDLE_IMAGE = os.environ.get("EXRADIUS_TEST_BUNDLE_IMAGE", "exradius-runtime-claude-code:0.1.0")


def _docker_client():
    try:
        import docker

        client = docker.from_env()
        client.ping()
        client.images.get(PROJECT_IMAGE)
        client.images.get(BUNDLE_IMAGE)
        return client
    except Exception:
        return None


@pytest.mark.skipif(not BROKER_URL, reason="EXRADIUS_TEST_BROKER_URL not set")
async def test_events_reach_bound_queues():
    from exradius import messaging

    connection = await messaging.connect(BROKER_URL)
    async with connection:
        channel = await connection.channel()
        exchange = await messaging.declare_topology(channel)
        queue = await channel.declare_queue(exclusive=True, auto_delete=True)
        await queue.bind(exchange, routing_key="task.#")

        envelope = EventEnvelope(
            task_id="IT-1", run_id="r", project="p", agent="a", correlation={"x": 1},
            type="status", payload=status_payload(State.RUNNING, State.PREPARING),
        )
        await messaging.EventPublisher(exchange).publish(envelope, message_id="m-1")

        message = await _get(queue)
        assert message.routing_key == "task.status"
        assert message.message_id == "m-1"
        body = json.loads(message.body)
        assert body["version"] == 1 and body["payload"]["state"] == "running"
        assert body["correlation"] == {"x": 1}
        await message.ack()


async def _get(queue, timeout: float = 5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        message = await queue.get(fail=False)
        if message is not None:
            return message
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("no message received")
        await asyncio.sleep(0.1)


docker_client = _docker_client()
needs_docker = pytest.mark.skipif(docker_client is None, reason=f"Docker, {PROJECT_IMAGE} or {BUNDLE_IMAGE} unavailable")


@pytest.fixture
def docker_settings(settings):
    settings.project_default_image = PROJECT_IMAGE
    settings.runtime_image_claude_code = BUNDLE_IMAGE
    return settings


@needs_docker
async def test_run_container_sandbox(docker_settings, tmp_path):
    """Run containers: /work writable, attachments, bundle and root filesystem read-only,
    unprivileged user, state mount and tmpfs available, and the bundle's runner and
    Claude Code start in a project image that has no Exradius content."""
    from exradius.worker import prepare
    from exradius.worker.containers import ContainerManager

    settings = docker_settings
    paths = prepare.create_run_directory(settings.data_dir, "it-run")
    prepare.write_attachments(paths.attachments, [("note.txt", b"hello")])
    prepare.seal_read_only(paths.attachments)
    for path in (paths.project, paths.state):
        path.chmod(0o777)  # the host user is not uid 1000; the worker chowns instead

    containers = ContainerManager(settings, docker_client)
    runtime = await containers.ensure_runtime("claude_code")
    script = " && ".join(
        [
            "test $(id -u) = 1000",
            "echo ok > /work/written.txt",
            "test \"$(cat /work/.exradius/attachments/note.txt)\" = hello",
            "! touch /work/.exradius/attachments/x 2>/dev/null",
            "! touch /usr/evil 2>/dev/null",
            "! touch /opt/exradius/runtime/evil 2>/dev/null",
            "touch /tmp/ok $HOME/ok $EXRADIUS_STATE_DIR/ok",
            "git --version >/dev/null",
            "! command -v node",
            "/opt/exradius/runtime/bin/claude --version",
            "{ /opt/exradius/runtime/bin/exradius-runner 2>&1 || true; }",
            "echo FOO=$FOO",
        ]
    )
    created = await asyncio.to_thread(
        containers._create, image=PROJECT_IMAGE, run_id="it-run", kind="run", paths=paths,
        env={"FOO": "bar"}, entrypoint=["/bin/sh", "-c"], command=[script], runtime=runtime,
    )
    result = await containers.wait(created.id)
    assert result.exit_code == 0, result.logs
    assert "FOO=bar" in result.logs
    assert "(Claude Code)" in result.logs
    assert "usage: exradius-runner <run_id>" in result.logs
    assert (paths.project / "written.txt").read_text() == "ok\n"
    assert (paths.state / "ok").exists()
    prepare.remove_run_directory(paths)


@needs_docker
async def test_bundle_extraction_is_idempotent(docker_settings):
    """The worker extracts the bundle once per build, into a volume named after it."""
    from exradius.worker.bundles import volume_name
    from exradius.worker.containers import ContainerManager

    first = await ContainerManager(docker_settings, docker_client).ensure_runtime("claude_code")
    again = await ContainerManager(docker_settings, docker_client).ensure_runtime("claude_code")
    image = docker_client.images.get(BUNDLE_IMAGE)
    assert first == again
    assert first.volume == volume_name("claude_code", image.labels["exradius.version"], image.id)
    assert docker_client.volumes.get(first.volume).attrs["Labels"]["exradius.runtime"] == "claude_code"


@needs_docker
async def test_list_and_remove_run_containers(docker_settings):
    """Recovery finds a run's containers by label and can remove them."""
    from exradius.worker import prepare
    from exradius.worker.containers import ContainerManager

    paths = prepare.create_run_directory(docker_settings.data_dir, "it-recover")
    containers = ContainerManager(docker_settings, docker_client)
    created = await asyncio.to_thread(
        containers._create, image=PROJECT_IMAGE, run_id="it-recover", kind="run", paths=paths,
        env={}, entrypoint=["/bin/sh", "-c"], command=["sleep 60"],
    )
    try:
        [found] = await containers.list_run_containers("it-recover")
        assert (found.id, found.kind, found.running) == (created.id, "run", True)
        assert await containers.list_run_containers("other-run") == []
    finally:
        await containers.remove_containers([created.id])
    assert await containers.list_run_containers("it-recover") == []


@needs_docker
async def test_project_python_settings_do_not_reach_the_runner(docker_settings):
    """A project's PYTHONPATH (with a module named like the runner's), PYTHONHOME and
    PYTHONSTARTUP do not affect the bundle's runner (runtime-bundles.md §5)."""
    from exradius.worker import prepare
    from exradius.worker.containers import ContainerManager

    paths = prepare.create_run_directory(docker_settings.data_dir, "it-isolation")
    evil = paths.project / "evil"
    (evil / "exradius").mkdir(parents=True)
    (evil / "exradius" / "__init__.py").write_text("raise SystemExit('EVIL exradius imported')\n")
    (evil / "sitecustomize.py").write_text("print('EVIL sitecustomize')\n")
    (evil / "json.py").write_text("raise SystemExit('EVIL json imported')\n")
    for path in (paths.project, paths.state, evil, evil / "exradius"):
        path.chmod(0o777)
    containers = ContainerManager(docker_settings, docker_client)
    runtime = await containers.ensure_runtime("claude_code")
    created = await asyncio.to_thread(
        containers._create, image=PROJECT_IMAGE, run_id="it-isolation", kind="run", paths=paths,
        env={"PYTHONPATH": "/work/evil", "PYTHONHOME": "/nonexistent", "PYTHONSTARTUP": "/work/evil/sitecustomize.py",
             "VIRTUAL_ENV": "/work/.venv", "NODE_OPTIONS": "--require /work/evil/x.js",
             "EXRADIUS_API_URL": "http://api.invalid"},
        entrypoint=[containers.entrypoint], command=["it-isolation"], runtime=runtime,
    )
    result = await containers.wait(created.id)
    prepare.remove_run_directory(paths)
    assert "EVIL" not in result.logs
    # The runner started on its own Python and got as far as looking for its run token.
    assert "EXRADIUS_RUN_TOKEN is not set" in result.logs, result.logs
