"""``exradius``: a small CLI for talking to an Exradius installation over the broker.

    exradius publish-task --project billing-platform --agent bug-fixer \\
        --instructions "Find the cause of negative invoice totals and fix it."
    exradius events                 # print every outbound event
    exradius answer --task-id JIRA-4821 --request-id <id> --answer "Yes, on Monday."
    exradius generate-key           # a new EXRADIUS_SECRET_KEY
    exradius migrate                # alembic upgrade head, one process at a time
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import mimetypes
import os
import sys
import uuid
from pathlib import Path

from exradius import messaging
from exradius.schemas.messages import AnswerMessage, TaskMessage


def _broker_url(args: argparse.Namespace) -> str:
    return args.broker or os.environ.get("EXRADIUS_BROKER_URL", "amqp://guest:guest@localhost:5672/")


def _attachments(paths: list[str]) -> list[dict]:
    result = []
    for raw in paths:
        path = Path(raw)
        result.append(
            {
                "name": path.name,
                "content_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "content_base64": base64.b64encode(path.read_bytes()).decode(),
            }
        )
    return result


async def _publish(url: str, queue: str, body: dict) -> None:
    connection = await messaging.connect(url)
    async with connection:
        channel = await connection.channel()
        await messaging.declare_topology(channel)
        await messaging.publish_to_queue(channel, queue, body)


async def publish_task(args: argparse.Namespace) -> None:
    message = TaskMessage(
        task_id=args.task_id or f"task-{uuid.uuid4().hex[:8]}",
        project=args.project,
        agent=args.agent,
        instructions=args.instructions,
        context=json.loads(args.context) if args.context else None,
        attachments=_attachments(args.attach or []),
        correlation=json.loads(args.correlation) if args.correlation else None,
    )
    await _publish(_broker_url(args), messaging.TASKS_QUEUE, message.model_dump(mode="json", exclude_none=True))
    print(message.task_id)


async def answer(args: argparse.Namespace) -> None:
    message = AnswerMessage(
        task_id=args.task_id,
        request_id=args.request_id,
        answer=args.answer,
        attachments=_attachments(args.attach or []),
    )
    await _publish(_broker_url(args), messaging.ANSWERS_QUEUE, message.model_dump(mode="json"))


async def events(args: argparse.Namespace) -> None:
    connection = await messaging.connect(_broker_url(args))
    async with connection:
        channel = await connection.channel()
        exchange = await messaging.declare_topology(channel)
        queue = await channel.declare_queue(exclusive=True, auto_delete=True)
        await queue.bind(exchange, routing_key=args.routing_key)
        print(f"listening on {messaging.EVENTS_EXCHANGE} ({args.routing_key}); Ctrl-C to stop", file=sys.stderr)
        async with queue.iterator() as iterator:
            async for message in iterator:
                async with message.process():
                    body = json.loads(message.body)
                    if args.task_id and body.get("task_id") != args.task_id:
                        continue
                    print(f"{message.routing_key} {json.dumps(body, ensure_ascii=False)}", flush=True)
                    if args.until_result and message.routing_key == "task.result":
                        return


def generate_key(_: argparse.Namespace) -> None:
    from exradius.crypto import generate_secret_key

    print(generate_secret_key())


MIGRATION_LOCK = 0x6578726164697573  # "exradius"


async def migrate(_: argparse.Namespace) -> None:
    """``alembic upgrade head`` under a Postgres advisory lock, so the api's and the worker's
    init containers can both run it (docs/kubernetes.md §16)."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    from exradius import models
    from exradius.config import get_settings
    from exradius.db import make_engine

    settings = get_settings()
    config = Config()
    config.set_main_option("script_location", str(Path(models.__file__).parent / "migrations"))
    engine = make_engine(settings.database_url)
    postgres = engine.dialect.name == "postgresql"
    try:
        async with engine.connect() as connection:
            if postgres:
                await connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK})
                await connection.commit()
            try:
                # env.py runs its own event loop: in a thread of its own.
                await asyncio.to_thread(command.upgrade, config, "head")
            finally:
                if postgres:
                    await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK})
                    await connection.commit()
    finally:
        await engine.dispose()
    print("database is at the latest migration", file=sys.stderr)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="exradius")
    parser.add_argument("--broker", help="AMQP URL (default: $EXRADIUS_BROKER_URL)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("publish-task", help="publish a task to exradius.tasks")
    p.add_argument("--project", required=True)
    p.add_argument("--agent", help="an agent of the project; without it Exradius picks one (docs/agent-routing.md)")
    p.add_argument("--instructions", required=True)
    p.add_argument("--task-id")
    p.add_argument("--context", help="JSON value")
    p.add_argument("--correlation", help="JSON value echoed back on every event")
    p.add_argument("--attach", action="append", help="file to attach (repeatable)")
    p.set_defaults(func=publish_task)

    p = sub.add_parser("answer", help="answer an info request")
    p.add_argument("--task-id", required=True)
    p.add_argument("--request-id", required=True)
    p.add_argument("--answer", required=True)
    p.add_argument("--attach", action="append")
    p.set_defaults(func=answer)

    p = sub.add_parser("events", help="print outbound events")
    p.add_argument("--routing-key", default="task.#")
    p.add_argument("--task-id", help="only events of this task")
    p.add_argument("--until-result", action="store_true", help="exit after the first task.result")
    p.set_defaults(func=events)

    p = sub.add_parser("generate-key", help="print a new EXRADIUS_SECRET_KEY")
    p.set_defaults(func=generate_key)

    p = sub.add_parser("migrate", help="bring the database to the latest migration")
    p.set_defaults(func=migrate)

    args = parser.parse_args(argv)
    result = args.func(args)
    if asyncio.iscoroutine(result):
        try:
            asyncio.run(result)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
