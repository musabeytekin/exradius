"""End-to-end check of a kind install (`make -C deploy/kind e2e`, docs/kubernetes.md §17).

Publishes one task over RabbitMQ, as an integration would, waits for its ``task.result``,
then checks that nothing of the run is left in the runs namespace. Needs a project that the
install can run (its config repository added in the panel, its store filled, a provider) and
RabbitMQ on localhost:5673 (Tilt forwards it; the script starts a port-forward otherwise).

    uv run --project backend python deploy/kind/e2e.py --project <slug> [--agent <slug>]
        [--instructions ...] [--timeout 900]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import socket
import subprocess
import sys
import time
import uuid
from urllib.parse import quote

from exradius import messaging
from exradius.schemas.messages import TaskMessage

CONTEXT = "kind-exradius"
NAMESPACE = "exradius"
RUNS = "exradius-runs"
PORT = 5673


def kubectl(*args: str) -> str:
    return subprocess.run(["kubectl", "--context", CONTEXT, *args], check=True, capture_output=True, text=True).stdout


def broker_url() -> str:
    secret = json.loads(kubectl("-n", NAMESPACE, "get", "secret", "exradius-rabbitmq-default-user", "-o", "json"))
    data = {k: base64.b64decode(v).decode() for k, v in secret["data"].items()}
    return f"amqp://{quote(data['username'], safe='')}:{quote(data['password'], safe='')}@localhost:{PORT}/"


def listening(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


async def run(args: argparse.Namespace) -> int:
    forward = None
    if not listening(PORT):
        forward = subprocess.Popen(
            ["kubectl", "--context", CONTEXT, "-n", NAMESPACE, "port-forward", "svc/exradius-rabbitmq", f"{PORT}:5672"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(50):
            if listening(PORT):
                break
            time.sleep(0.2)
    try:
        return await _run(args, broker_url())
    finally:
        if forward is not None:
            forward.terminate()


async def _run(args: argparse.Namespace, url: str) -> int:
    task_id = f"e2e-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    message = TaskMessage(task_id=task_id, project=args.project, agent=args.agent, instructions=args.instructions)
    connection = await messaging.connect(url)
    run_id = None
    async with connection:
        channel = await connection.channel()
        exchange = await messaging.declare_topology(channel)
        queue = await channel.declare_queue(exclusive=True, auto_delete=True)
        await queue.bind(exchange, routing_key="task.#")
        await messaging.publish_to_queue(channel, messaging.TASKS_QUEUE, message.model_dump(mode="json", exclude_none=True))
        print(f"published {task_id} to {messaging.TASKS_QUEUE}", flush=True)
        deadline = time.monotonic() + args.timeout
        result = None
        async with queue.iterator() as iterator:
            while result is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    print(f"no task.result within {args.timeout:.0f}s", file=sys.stderr)
                    return 1
                try:
                    incoming = await asyncio.wait_for(iterator.__anext__(), remaining)
                except TimeoutError:
                    continue
                async with incoming.process():
                    body = json.loads(incoming.body)
                    if body.get("task_id") != task_id:
                        continue
                    run_id = body.get("run_id") or run_id
                    payload = body.get("payload") or {}
                    if incoming.routing_key == "task.status":
                        print(f"  {payload.get('state')}{' (' + payload['reason'] + ')' if payload.get('reason') else ''}",
                              flush=True)
                    elif incoming.routing_key == "task.info_request":
                        print(f"  question: {payload.get('question')}", flush=True)
                    elif incoming.routing_key == "task.result":
                        result = payload
    print(f"result: {result.get('state')}")
    if result.get("summary"):
        print(f"summary: {result['summary'][:500]}")
    if result.get("error"):
        print(f"error: {result['error'][:2000]}")
    left = await leftovers(run_id)
    if left:
        print(f"left in {RUNS} after 60 s: {left}", file=sys.stderr)
        return 1
    print(f"nothing of run-{run_id} is left in {RUNS}")
    return 0 if result.get("state") == "completed" else 1


async def leftovers(run_id: str | None) -> str:
    """Pods, PVCs and Secrets of the run, once garbage collection had a minute."""
    if run_id is None:
        return ""
    selector = f"exradius.dev/run=run-{run_id}"
    for _ in range(30):
        found = kubectl("-n", RUNS, "get", "runs.exradius.dev,pods,pvc,secrets", "-l", selector, "-o", "name").strip()
        if not found:
            return ""
        await asyncio.sleep(2)
    return found.replace("\n", ", ")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", required=True)
    parser.add_argument("--agent")
    parser.add_argument("--instructions", default="List the folders of the working directory, then submit a "
                        "result whose output is that list. Change nothing.")
    parser.add_argument("--timeout", type=float, default=900)
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
