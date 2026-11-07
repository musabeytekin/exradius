# Environment section, services, model choice

> **Since the config-repo change (`docs/config-repo.md`, 2026-11-07)** the project's file provides
> what this document puts in tables and panel tabs: `services` (with `image` or a `dockerfile`
> built from the config repository), `env`, `notes`, `workstation` (image or Dockerfile, and
> `setup`), and each agent's `model`. The environment section, the services' lifecycle, the
> networks, the delivery warning and the passwd fix below are unchanged; the `project_services`
> table, the Services API and tab, `projects.notes` and `agents.model` columns are gone. A
> `${secret:NAME}` now resolves from the config's secret store, and the workstation carries every
> secret its services' settings use under its own name, which is what `$NAME` in the section means.

Agreed 2026-11-07 after the Bank of Anthos live tests. Four problems and the decisions:

| problem seen | decision |
|---|---|
| the agent discovers its surroundings by trial ("no Docker", "not a git repository") | Exradius states the facts in a generated **environment section** (§1) |
| work left in the run directory is lost when the agent does not push | stays the agent's job; the environment section and the rules make the consequence explicit, and the worker adds a **report-only warning** to the result (§4). The worker never commits or pushes for the agent |
| projects whose dependencies need Docker cannot run anything | **services** on the project, GitHub Actions style (§2) |
| every run uses the runtime's default model | **model** on the agent, picked from the provider's catalog (§3) |

## 1. The environment section

A third part of the system prompt: Claude Code's preset, the operating rules, then
`# Your environment`, then the agent's role (architecture §13 is extended, not changed).
Exradius generates it from facts it holds; nothing in it is typed by the admin except the
project notes. It never names Exradius.

```
# Your environment

You run unattended in a Linux container built from the image bank-of-anthos-dev:latest.
Each folder in the working directory is a git clone of one repository:

- bank-of-anthos: branch main, origin https://github.com/musabeytekin/bank-of-anthos.git, pushing allowed
- platform-config: branch main, origin https://github.com/acme/platform-config.git, pushing NOT allowed

Not available: a Docker daemon, Kubernetes, sudo, installing packages. Outbound network access is available.

Services running for this task, reachable by host name from this container:
- postgres (postgres:16) on port 5432, POSTGRES_USER=app, POSTGRES_DB=app, POSTGRES_PASSWORD=$DB_PASSWORD
- redis (redis:7) on port 6379
A value written as $NAME is the value of your environment variable NAME.

The working directory is deleted when the task ends. Only what you push, attach to the result,
or write in the result survives.

Notes from the project's maintainers:
<projects.notes, verbatim>
```

Sources:

- **Repositories**: `repositories` rows of the project (`name`, `branch`, `url` with any
  credentials stripped, `push_allowed`).
- **Not available**: fixed text. The agent discovers installed tools itself (`which`,
  `--version`); Exradius lists only what it cannot discover: there is no Docker daemon even
  if a client is present, no Kubernetes, no sudo, no package installation.
- **Services**: the project's services (§2) with name, image, ports and settings. A
  setting is an env entry shown as `KEY=value`; a `${secret:NAME}` in it is shown as
  `$NAME`, the variable the run environment carries (project secrets are exported under
  their own names), never as the value. A secret that is only global is not in the run
  environment and shows as `<a secret you do not have>`. The `$NAME` hint line is added
  when any setting names a variable. (Added after BANK-204: the agent asked for the user
  and database because the section gave only the port.)
- **The last paragraph** is fixed text.
- **Notes**: `projects.notes`, a text field; omitted when empty.

The runner receives the section ready-made in the run spec (`environment_text`), so a
runtime only concatenates. `build_system_prompt` becomes rules + environment + role.
The resolved agent view (`GET /api/projects/{id}/agents/{slug}/resolved`) returns the
section as the worker would build it, so the panel can show it.

## 2. Services

A project may list services, the dependencies an engineer would run in Docker Desktop.
GitHub Actions `services:` is the model: an image and its settings, nothing to build,
nothing to parse from the repository.

`project_services` table: `id`, `project_id`, `name` (host name on the run network,
`[a-z][a-z0-9-]*`, unique per project), `image`, `env` JSON (values may use
`${secret:NAME}` like connections), `ports` JSON (integers, documentation for the agent;
nothing is published), `ready` JSON (`null` = the image's HEALTHCHECK when it has one,
else "container is running"; or `{"tcp": 5432}` to wait for a port; or
`{"command": ["pg_isready", "-U", "app"]}`), `position` for ordering.

Lifecycle, per run:

1. In `preparing`, after cloning and before setup commands: the worker creates a network
   `exradius-run-<run_id>` and starts each service on it with the alias `name`, labels
   `exradius.run_id` and `exradius.kind=service`, `cap_drop ALL` plus the capabilities
   common server images need (`CHOWN`, `SETUID`, `SETGID`, `FOWNER`, `DAC_OVERRIDE`,
   `NET_BIND_SERVICE`), `no-new-privileges`, no mounts, env decrypted at that moment.
2. It waits for `ready` (timeout 120 s per service, configurable later). A service that
   does not become ready fails the run in `preparing` with the service name and its log
   tail.
3. Setup commands and the agent container join the same network. Project env vars can
   therefore say `DATABASE_URL=postgres://app:${secret:DB_PASSWORD}@postgres:5432/app`;
   secret references in project env vars are resolved like connection values.
4. While the run waits for input the services stay up; resume joins the same network.
5. At finalize: stop and remove the services and the network. `reconcile` and
   `sweep_orphans` remove services and networks whose run is finished or unknown, by label.

Networks (changed after BANK-204, where a run's `postgres` resolved to Exradius's own
database because runs were on the stack network):

- `exradius`: the stack (api, worker, frontend, postgres, rabbitmq). Runs never join it.
- `exradius-runs` (`run_network`): the API, with the alias `exradius-api`, and every run,
  setup and provider-probe container. Runs reach the API at `http://exradius-api:8000`
  (`run_api_url`). The worker is not on it.
- `exradius-run-<run_id>`: with services, the run's own network, so parallel runs never
  share a database. Setup and run containers are on it and on `exradius-runs`.
- The delivery check (§4) runs with no network.

Service names that would shadow a name runs already resolve are refused: `api`, the host of
`run_api_url`, and anything starting with `exradius`.

Verify: steps "Start service <name>" (with readiness) for each service, before the setup
commands, and "Stop services" at the end. There is no image probe or "Inspect image" step:
agents discover tools themselves.

API: `GET/POST /api/projects/{id}/services`, `PATCH/DELETE /api/projects/{id}/services/{sid}`,
`PUT /api/projects/{id}/services/order`. `ProjectServiceIn/Out` carry the columns above.

Panel: a **Services** tab on the project. "Add service" opens a small catalog (Postgres,
MySQL, Redis, RabbitMQ, MongoDB, Elasticsearch, Mailpit, MinIO) that prefills image, env
with sensible defaults and the secret-backed password field labelled as such, ports and
readiness; a "Custom image" entry leaves the fields empty. Rows show name, image, ports,
readiness and the last verify outcome for the service. Pick, don't type.

## 3. Model on the agent

`agents.model` (text, nullable; `null` = the runtime's default). The runtime passes it
through (`ClaudeAgentOptions(model=...)`). Provider catalog entries list the models they
offer (`anthropic`: `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5`, with a
short description each); the panel's agent form offers "Runtime default" first, the
provider's models, and "Other" with a text field for an id the list does not know. The
provider test (`POST /api/providers/{id}/test`) accepts an optional `model` so a chosen
id can be checked before saving. The run's transcript already records the model used;
`AgentOut` and the resolved view expose `model`.

## 4. Report-only delivery warning

At finalize, before the run directory is removed, the worker checks each repository
clone with `git status --porcelain` and `git log @{upstream}..` (or `git log <base>..` when
no upstream). When a clone has uncommitted changes or commits that were not pushed, the
result payload gains `warnings: ["bank-of-anthos: 2 commits on BANK-204-fix were not pushed
and were discarded with the task directory", ...]`, and the task.result event carries it.
The worker changes nothing in the clone and pushes nothing.

## 5. Small fixes in the same batch

- **Run user home**: runs use uid 1000 with `HOME=/home/runner`, but images carry a passwd
  entry for uid 1000 with another home (Ubuntu's `ubuntu`), and Java reads the home from
  passwd. The worker mounts a generated `/etc/passwd` and `/etc/group` read-only into the
  agent and setup containers: `root` plus `runner:x:<uid>:<gid>::/home/runner:/bin/bash`
  and the matching group. Document in runtime-bundles §2.4.
- **Co-author line**: the Claude Code runtime writes `{"includeCoAuthoredBy": false}` into
  `settings.json` under `CLAUDE_CONFIG_DIR` before the session starts, unless the file
  exists. Commits then carry only the identity and the Exradius trailers.
- **Task id required**: the panel's New task form requires a task id (the ticket key) and
  no longer invents one; the API keeps accepting a missing id for integrations.
- **Notes**: `projects.notes` text field and a Notes tab (one text area, hint "Write what
  you would tell a new engineer on their first day."), shown in the environment section.

## 6. Out of scope

Worker-side commits, pushes or pull requests; compose files; Docker inside runs; services
with mounts or published ports; per-task model overrides.
