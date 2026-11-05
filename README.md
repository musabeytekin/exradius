# Exradius

Exradius is a self-hosted execution harness for AI agents. Tasks arrive on a RabbitMQ
queue. A worker runs the named agent against the named project in an isolated Docker
container through the Claude Agent SDK. Status changes, questions from the agent and
results are published back on a topic exchange. Exradius only executes: managing tasks
and integrating with ticketing, chat or CI tools is the job of an integration layer you
build on top of the queues.

The design is in [docs/architecture.md](docs/architecture.md). It ends with
implementation notes that record the details the design left open.

## Repository layout

```
backend/            Python 3.14 package `exradius` (uv)
  exradius/api/       admin API (session cookie) and internal run API (run token)
  exradius/worker/    broker consumers, run preparation, container control
  exradius/runner/    run container entrypoint and the built-in `exradius` MCP server
  exradius/runtimes/  Runtime protocol and ClaudeCodeRuntime
  exradius/models/    SQLAlchemy models and Alembic migrations
  exradius/schemas/   Pydantic models for broker messages and APIs
  exradius/messaging/ RabbitMQ topology and publishing
  tests/
deploy/
  docker-compose.yml  api, worker, postgres, rabbitmq (management UI)
  runner.Dockerfile   the exradius-runner image (also used by api and worker)
```

The frontend is not in this repository yet.

## Running the stack

You need Docker with Compose v2.

```bash
cp deploy/.env.example deploy/.env
```

Edit `deploy/.env`:

- `EXRADIUS_SECRET_KEY` is the Fernet key that encrypts secrets at rest. Generate one with
  `cd backend && uv run exradius generate-key`.
- `EXRADIUS_ADMIN_PASSWORD` is the password of the single admin user (`admin` by default).
- `ANTHROPIC_API_KEY` is the single global key that every run uses. You can store it as
  a global secret named `ANTHROPIC_API_KEY` through the admin API instead.
- `EXRADIUS_DATA_DIR` is the host directory for run directories, git mirrors and the
  object store (transcripts, artifacts and attachments, under `objects/`). The api and
  worker services both mount it. The worker bind-mounts parts of it into run containers
  through the host's Docker daemon, so it must be an absolute host path. On macOS with Docker Desktop, choose a folder under
  `/Users`, for example `/Users/you/exradius-data`.
- `EXRADIUS_PUBLIC_API_URL` is the address at which consumers of the events reach the API
  (default `http://localhost:8000`). The artifact and transcript links in `task.result`
  are signed download URLs on it, so the integration layer can fetch them without logging
  in.
- To keep objects in an S3 API instead of the filesystem, uncomment
  `EXRADIUS_OBJECT_STORE=s3` and the `EXRADIUS_S3_*` lines.
- If ports 8000, 5672, 15672 or 5432 are already in use, change the `EXRADIUS_*_PORT`
  values.

Build and start the stack:

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

The API service runs the database migrations (`alembic upgrade head`) at start. Once it is
up:

- API: http://localhost:8000 (OpenAPI docs at `/docs`)
- RabbitMQ management UI: http://localhost:15672 (user `exradius`, password `exradius`)

Compose builds the `exradius-runner:latest` image. Run containers use it unless a project
sets its own `image`, and a project image must be built `FROM exradius-runner` so that the
runner entrypoint is available.

## Configuring a project and an agent

The admin API uses a session cookie, so the examples below keep a cookie jar.

```bash
API=http://localhost:8000
curl -s -c jar -H 'content-type: application/json' \
  -d '{"username":"admin","password":"<EXRADIUS_ADMIN_PASSWORD>"}' $API/api/auth/login

# A project with one repository (any git URL the worker can reach)
PROJECT=$(curl -s -b jar -H 'content-type: application/json' \
  -d '{"slug":"demo","name":"Demo","setup_commands":[]}' $API/api/projects | jq -r .id)
curl -s -b jar -H 'content-type: application/json' \
  -d '{"name":"app","url":"https://github.com/octocat/Hello-World.git","branch":"master"}' \
  $API/api/projects/$PROJECT/repositories

# An agent, attached to the project
AGENT=$(curl -s -b jar -H 'content-type: application/json' \
  -d '{"slug":"helper","name":"Helper","prompt":"You are a careful software engineer. When you are done, call submit_result."}' \
  $API/api/agents | jq -r .id)
curl -s -b jar -H 'content-type: application/json' \
  -d "{\"agent_id\":\"$AGENT\"}" $API/api/projects/$PROJECT/agents
```

The other resources follow the same pattern:

- `/api/projects/{id}/env-vars`
- `/api/projects/{id}/secrets` and `/api/secrets` (global). Secrets are write-only: the
  API never returns their values.
- `/api/connections` (MCP servers, `stdio` or `http`), plus
  `POST /api/connections/{id}/test`, which starts the server and lists its tools.
- `/api/agents` with `connection_ids`.
- `/api/projects/{id}/agents/{agent_id}` for overrides.
- `/api/tasks`, `/api/tasks/{task_id}`, `/api/tasks/{task_id}/transcript` and
  `POST /api/tasks/{task_id}/cancel`.

In a connection's `env` and `headers` you can reference a secret by name with
`${secret:NAME}`.

## Publishing a first task

### With the CLI

The `exradius` CLI ships with the backend package. The broker URL comes from
`--broker` or `EXRADIUS_BROKER_URL`.

```bash
cd backend
export EXRADIUS_BROKER_URL=amqp://exradius:exradius@localhost:5672/

# In one terminal: print events until the task's result arrives
uv run exradius events --task-id demo-1 --until-result

# In another terminal: publish the task
uv run exradius publish-task --task-id demo-1 --project demo --agent helper \
  --instructions "Summarise what the repository in app/ does." \
  --context '{"requested_by":"readme"}'
```

The event stream shows `task.status` events (`queued`, `preparing`, `running`, then
`completed` or `failed`) and finally a `task.result` event. Its `transcript_url` and
artifact `url`s are signed download links, for example `curl -o transcript.jsonl
"<transcript_url>"`. If the agent calls
`request_information`, a `task.info_request` event arrives and the run waits. To answer:

```bash
uv run exradius answer --task-id demo-1 --request-id <request_id> --answer "Yes."
```

### With a short Python script

```python
import asyncio, json
import aio_pika

async def main():
    connection = await aio_pika.connect_robust("amqp://exradius:exradius@localhost:5672/")
    async with connection:
        channel = await connection.channel()
        events = await channel.declare_exchange("exradius.events", aio_pika.ExchangeType.TOPIC, durable=True)
        queue = await channel.declare_queue(exclusive=True)
        await queue.bind(events, routing_key="task.#")

        task = {
            "version": 1,
            "task_id": "demo-2",
            "project": "demo",
            "agent": "helper",
            "instructions": "List the files in app/ and describe them.",
            "correlation": {"ticket": "DEMO-2"},
        }
        await channel.default_exchange.publish(
            aio_pika.Message(json.dumps(task).encode(), content_type="application/json",
                             delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
            routing_key="exradius.tasks",
        )

        async with queue.iterator() as messages:
            async for message in messages:
                async with message.process():
                    event = json.loads(message.body)
                    print(message.routing_key, event["payload"])
                    if message.routing_key == "task.result" and event["task_id"] == "demo-2":
                        break

asyncio.run(main())
```

## Development

```bash
cd backend
uv sync
uv run pytest
```

The unit tests use SQLite, an in-memory object store and local temporary git
repositories, so they need no external services. The integration tests in
`tests/test_integration.py` run only when their infrastructure is available:

- `EXRADIUS_TEST_BROKER_URL=amqp://… uv run pytest tests/test_integration.py` tests
  event publishing against a real RabbitMQ.
- With Docker running and the `exradius-runner:latest` image built, a test checks the run
  container sandbox: unprivileged user, read-only root filesystem and attachments, and a
  writable `/work`.

To run the services outside Docker, set `EXRADIUS_DATABASE_URL`, `EXRADIUS_BROKER_URL`,
`EXRADIUS_SECRET_KEY` and `EXRADIUS_DATA_DIR` (and `EXRADIUS_OBJECT_STORE=s3` with the
`EXRADIUS_S3_*` variables if you want S3). Then run
`uv run alembic upgrade head`, `uv run exradius-api` and `uv run exradius-worker`.
