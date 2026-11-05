"""Integration tests against real infrastructure. Skipped unless it is available:

- RabbitMQ: set ``EXRADIUS_TEST_BROKER_URL`` (e.g. ``amqp://exradius:exradius@localhost:5672/``).
- Docker: needs a reachable daemon and the ``exradius-runner:latest`` image
  (``docker build -f deploy/runner.Dockerfile -t exradius-runner:latest .``).
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from exradius.schemas.messages import EventEnvelope, status_payload
from exradius.states import State

BROKER_URL = os.environ.get("EXRADIUS_TEST_BROKER_URL")
RUNNER_IMAGE = os.environ.get("EXRADIUS_TEST_RUNNER_IMAGE", "exradius-runner:latest")


def _docker_client():
    try:
        import docker

        client = docker.from_env()
        client.ping()
        client.images.get(RUNNER_IMAGE)
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


@pytest.mark.skipif(docker_client is None, reason=f"Docker or {RUNNER_IMAGE} unavailable")
async def test_run_container_sandbox(settings, tmp_path):
    """Run containers: /work writable, attachments and root filesystem read-only,
    unprivileged user, state mount and tmpfs available."""
    from exradius.worker import prepare
    from exradius.worker.containers import ContainerManager

    settings.runner_image = RUNNER_IMAGE
    paths = prepare.create_run_directory(settings.data_dir, "it-run")
    prepare.write_attachments(paths.attachments, [("note.txt", b"hello")])
    prepare.seal_read_only(paths.attachments)
    for path in (paths.project, paths.state):
        path.chmod(0o777)  # the host user is not uid 1000; the worker chowns instead

    script = " && ".join(
        [
            "test $(id -u) = 1000",
            "echo ok > /work/written.txt",
            "test \"$(cat /work/.exradius/attachments/note.txt)\" = hello",
            "! touch /work/.exradius/attachments/x 2>/dev/null",
            "! touch /usr/evil 2>/dev/null",
            "touch /tmp/ok $HOME/ok $EXRADIUS_STATE_DIR/ok",
            "git --version >/dev/null && node --version >/dev/null && python --version >/dev/null",
            "echo FOO=$FOO",
        ]
    )
    containers = ContainerManager(settings, docker_client)
    result = await containers.run_setup_command(
        image=RUNNER_IMAGE, run_id="it-run", paths=paths, env={"FOO": "bar"}, command=script
    )
    assert result.exit_code == 0, result.logs
    assert "FOO=bar" in result.logs
    assert (paths.project / "written.txt").read_text() == "ok\n"
    assert (paths.state / "ok").exists()
    prepare.remove_run_directory(paths)


@pytest.mark.skipif(docker_client is None, reason=f"Docker or {RUNNER_IMAGE} unavailable")
async def test_list_and_remove_run_containers(settings):
    """Recovery finds a run's containers by label and can remove them."""
    from exradius.worker import prepare
    from exradius.worker.containers import ContainerManager

    paths = prepare.create_run_directory(settings.data_dir, "it-recover")
    containers = ContainerManager(settings, docker_client)
    created = await asyncio.to_thread(
        containers._create, image=RUNNER_IMAGE, run_id="it-recover", kind="run", paths=paths,
        env={}, entrypoint=["/bin/sh", "-c"], command=["sleep 60"],
    )
    try:
        [found] = await containers.list_run_containers("it-recover")
        assert (found.id, found.kind, found.running) == (created.id, "run", True)
        assert await containers.list_run_containers("other-run") == []
    finally:
        await containers.remove_containers([created.id])
    assert await containers.list_run_containers("it-recover") == []
