# Onboarding and the admin panel

Status: agreed design, 2026-11-05. Companion to `architecture.md`. This document is the
brief for the frontend and for the admin API additions the frontend needs.

## 1. The setup path today, and where it hurts

Configuring Exradius end to end takes nine steps across three surfaces:

| Step | Surface | What can go wrong, and when the user finds out |
|---|---|---|
| Fill `deploy/.env` (Fernet key, admin password, Anthropic key, data dir, public URL) | shell | A bad data dir or missing key is found at the first run, not at startup. |
| `docker compose up` | shell | Fine; the API runs migrations. |
| Log in | API | Fine. |
| Create a project | API | Nothing can fail yet, but nothing is checked either. |
| Add repositories, with a secret for private ones | API | A wrong URL, branch or credential fails the **first task**, minutes later, as a failed run. The secret must exist before the repository that uses it. |
| Add env vars, secrets, setup commands | API | A setup command that fails is found at the first task. |
| Create connections (MCP servers) | API | Raw `command`/`args`/`env`. A typo in `${secret:NAME}` fails at run time. "Test connection" exists but is a separate call. |
| Create an agent, attach it | API | A blank prompt. Nothing tells the user about `request_information` and `submit_result` or how the task text is laid out. |
| Publish a task and watch events | CLI | The only way to see Exradius work is to build or run a broker client. |

Two patterns dominate: **errors are discovered late**, at the first real run, and **the
user has to already know the conventions** (secret references, MCP configuration, the
built-in tools) that nothing in the product teaches.

## 2. What mature self-hosted products do

- **First-run readiness, not a wizard for everything.** GitLab, Sentry and Grafana show a
  status page: what is configured, what is reachable, what is missing. Coolify checks
  Docker and connectivity on a server before letting you deploy to it.
- **Validate at the point of entry.** n8n and Home Assistant test a credential when it is
  saved. Vercel checks the repository when it is imported, then suggests build settings.
- **Dry runs.** Terraform `plan` and a Vercel preview deploy let you see the failure before
  it matters. The equivalent here is preparing a project without running an agent.
- **Catalogs and import, not blank forms.** n8n has credential types; Claude Code, Cursor
  and others accept the standard `mcpServers` JSON block, so users paste what the
  server's README gives them.
- **A derived checklist, not a tutorial.** Stripe and Segment show "waiting for your first
  event" with a live indicator and copy-paste snippets with the user's real values filled
  in. The checklist is computed from state, so it is always right and never nags.
- **Templates that teach.** Linear and Notion ship starter content that shows the
  conventions by example.
- **Show the wire.** Stripe's webhook page lists recent events with their payloads. For a
  product whose whole contract is a queue, the admin panel must show the messages.

## 3. The design for Exradius

### 3.1 Overview page: readiness and checklist

The landing page after login. Top: a **system status** strip computed by the API: database,
broker, live workers with their concurrency, Docker reachable from the worker, runner
image present, Anthropic key configured, public API URL, object store. Each item is green,
amber or red with a one-line fix ("set `ANTHROPIC_API_KEY` in `deploy/.env` or add a global
secret named `ANTHROPIC_API_KEY`").

Below it: a **setup checklist** derived from state, never stored:

1. Anthropic key configured
2. A project exists
3. The project has a repository that passed a check
4. The project passed verification (optional if it has no repositories)
5. An agent exists and is attached to the project
6. A first task has reached `completed`

Each item links to the place that completes it. The checklist disappears once everything
is done and can be reopened from the overview.

### 3.2 Projects: check and verify

The project page has tabs: Repositories, Environment, Secrets, Agents, Verify.

- **Repository check** on save and on demand: the API runs `git ls-remote` with the chosen
  secret and reports the branches it saw. A wrong URL, branch or credential is caught
  here.
- **Verify project**: the worker prepares the project exactly as a run would (mirrors,
  clones, attachments folder, setup commands in the project image) in a throwaway
  directory, then deletes it. The page shows each step with its duration and log tail.
  This is the dry run.
- **Secrets first**: the repository form offers "create a secret" inline, so order never
  bites.

### 3.3 Connections: catalog, import, test

- **Catalog** of common MCP servers (static, in the frontend): GitHub, GitLab, filesystem,
  Postgres, Slack, Sentry, Linear, Jira, fetch, Playwright, and "custom". Picking one
  fills the command and args and lists the secrets it needs, each with an inline "create
  secret" that inserts `${secret:NAME}`.
- **Import JSON**: paste a standard `mcpServers` block; each entry becomes a connection.
- **Test** is part of the form: saving runs the test and shows the tool list, or the error.
- **Reference check**: the form resolves every `${secret:NAME}` against existing global
  secrets and flags unknown ones. Project-scoped references are checked again on attach.

### 3.4 Agents: templates and the guide

- **Starter prompts** for the three example agents (bug fixer, feature integrator,
  tracer) and a blank one. They show the conventions: start in the project directory,
  call `request_information` when blocked, call `submit_result` when done.
- **Guide panel** beside the prompt editor: the built-in tools with their signatures, the
  exact layout of the task text the agent receives (`# Task`, `# Context`,
  `# Attachments`), and the connections attached with their tool names from the last test.
- **Attach** shows the resolved configuration for this project (secret names, never
  values) and any unresolved references.

### 3.5 Tasks: send, follow, answer

- **Send a task** from a project or agent page. The admin API publishes it to
  `exradius.tasks`, the same path an integration uses. The form shows the exact JSON it
  will publish.
- **Task detail** follows the run live: a timeline of status events, the transcript as a
  stream of assistant text and collapsible tool calls, info requests with an **answer box**
  that publishes to `exradius.answers`, and artifacts with download and preview.
- **Cancel** stays.

### 3.6 Integration page: show the wire

For whoever builds the integration layer: broker address and credentials status, the
queue and exchange names, routing keys, every message schema with an example, snippets
in Python and curl with this installation's values filled in, and a **live tail** of
`exradius.events` for the last hour with payloads.

### 3.7 Infrastructure settings

`deploy/.env` stays the source for infrastructure values. The Settings page shows which are
set (values masked) and what each one does, so a misconfiguration is visible without a
shell.

## 4. Admin API additions

Everything the frontend needs beyond the existing routes. The frontend may mock these
until they land.

```
GET  /api/system/status
     { database: {ok, detail}, broker: {ok, detail},
       workers: [{id, hostname, started_at, last_seen_at, concurrency, docker_ok,
                  runtimes: [{name, version, ready, image, error}]}],
       provider: {configured, default: {id, name, kind, auth_method, last_test} | null, count},
       public_api_url, data_dir, version }
     (Since providers.md: `anthropic_key` and `object_store` are gone.)
     Workers heartbeat into a `workers` table every 15 s (hostname, concurrency, docker
     and image checks); a worker unseen for 60 s is not listed.

GET  /api/system/integration
     { broker: {host, port, vhost, username, password_set},
       tasks_queue, answers_queue, events_exchange, routing_keys: [...],
       public_api_url }

POST /api/projects/{id}/repositories/{repo_id}/check
     -> { ok, branches: [...], default_branch, error }        (git ls-remote in the API)

POST /api/projects/{id}/verify              -> 202 { verification_id }
GET  /api/projects/{id}/verifications/{vid}
     { state: pending|running|passed|failed,
       steps: [{name, state, started_at, ended_at, log}] }
     The API publishes a control message; the worker prepares in a throwaway directory
     and records each step; the directory is removed afterwards.

GET  /api/projects/{id}/agents/{agent_id}/resolved
     { connections: [{slug, transport, command, args, url,
                      env: {KEY: "${secret:NAME}" | "<literal>"}, unresolved: [...] }] }
     Secret values are never returned; references are shown as written.

POST /api/tasks                              -> 202   (body: task envelope without version)
POST /api/tasks/{task_id}/answer             -> 202   (body: {request_id, answer, attachments})
     Both publish to the broker exactly as an integration would.

GET  /api/tasks/{task_id}/events             -> stored events, oldest first
GET  /api/tasks/{task_id}/stream             -> server-sent events: every new status,
     info_request, result and transcript chunk as it is stored (server polls Postgres)

GET  /api/events?since=<iso>&limit=<n>       -> recent events across tasks (live tail)
```

GET  /api/system/images
     [{tag, created_at, size}]   images on the worker's daemon that carry the label
     (since runtime-bundles.md: every tagged image except Exradius' own backend and
     bundle images; no label needed), so the
     project image field can be a pick list. Reported through the worker heartbeat.

GET  /api/system/runtimes          -> [{name, label, providers: [...]}]   registered runtimes
     with the provider kinds and auth methods each accepts (providers.md §2.1); each auth
     method has {name, label, credential_label, settings: [{name, label, required}],
     notice | null}

Connections keep their last test: `ConnectionOut` gains
     last_test: {ok, tested_at, tools: [{name, description}], error} | null
so the agent form can show each connection's tools without re-running the test.

Implemented 2026-11-05 with these details (the backend's `/docs` OpenAPI page is the
exact reference):

- Repository checks are stored as `last_check` on the repository (with `checked_at`) and
  cleared when the URL or secret changes; checklist item 3 reads it. A missing branch makes
  `ok` false but the branches seen are still returned.
- `GET /api/system/integration` takes the broker host and port from
  `EXRADIUS_PUBLIC_BROKER_HOST` and `EXRADIUS_PUBLIC_BROKER_PORT` (compose sets
  `localhost` and the AMQP port) and includes `events_exchange_type`.
- `POST /api/tasks` and `POST /api/tasks/{id}/answer` do no lookups: they publish and
  reply 202 with the exact JSON published, or 503 when the broker is down. An unknown
  project or agent arrives later as a `rejected` status event.
- The stream replays stored events from the start, then goes live; it waits for a task
  that is not stored yet (the panel opens it right after sending); it ends after `result`,
  or after a rejection for a task that was never stored. Heartbeat comments every 15 s.
- `GET /api/events` without `since` returns the latest `limit` events, oldest first.
  `EventOut` carries `task_id` and `run_id`.
- Verification steps may also be `skipped`. Resolved connections carry `references` and
  `overridden` besides `unresolved`. Workers carry `docker_detail`, `runtimes` (which replaced `runner_image` and
  `runner_image_ok`) and
  `version`.

Providers (docs/providers.md §5) were added later, on 2026-11-05, with these details:

- `ProviderOut` also has `created_at` and `updated_at`. `agent_count` includes, for the
  default provider, the agents without a provider of their own.
- PATCH takes the create fields (slug, name, kind, auth_method, credential, settings), not
  `is_default`; use `POST /api/providers/{id}/default`. Changing the kind or auth method
  needs a new credential in the same request.
- `GET /api/providers/{id}/tests/{tid}` returns
  `{id, provider_id, state: pending|running|passed|failed, created_at, started_at, ended_at,
  detail, error}`.
- `POST /api/auth/password` answers 400 when the current password is wrong and 422 when
  the new one is shorter than 8 characters.

Connection reference validation is done in the frontend from `GET /api/secrets` and
`GET /api/projects/{id}/secrets`; no API change.

## 5. Frontend stack and design direction

- React 19, TypeScript, Vite, Tailwind CSS 4, Radix primitives, TanStack Query and
  Router. No component-kit theme: the look is our own.
- Served by a `frontend` service in compose (nginx, port 3000) that proxies `/api` to the
  API, so the session cookie is same-origin. Vite dev server proxies the same way.
- **Design direction**: a calm, dense, expert tool. One typeface family for UI (Inter or
  Geist), a monospace for ids, commands and payloads. A neutral palette with a single
  accent; light and dark themes. 8-pixel rhythm, generous whitespace around dense tables.
  Status as small chips with consistent colors across every page. Motion only to
  communicate state changes. Command palette on ⌘K for navigation and actions. Skeleton
  loading, optimistic updates, undo on destructive actions where possible. Empty states
  explain what the thing is and offer the next action. Forms validate inline and show the
  request they will send when that helps. Minimum width 1024; nothing breaks narrower.
- The transcript viewer is the signature screen: a timeline that reads like a
  conversation, tool calls collapsed to one line with the tool name and the first
  argument, expandable to full input and output, JSON rendered as a tree.

## 6. Pick, don't type

Rule: **a value that already exists in the system, or that the system can discover, is
offered, never typed.** Free text is for what only the user knows: names, prompts, task
instructions, URLs, secret values, environment values. Everything else is a pick list, a
derived default or an inline "create" that returns to the form. Assistance stops there:
no autocomplete for prose, no wizards for one-field forms, no tooltips on self-explanatory
fields.

| Form | Field | Assistance |
|---|---|---|
| Project | slug | Derived from the name as it is typed; editable; uniqueness checked live. |
| Project | image | Pick list from `GET /api/system/images`, empty means `exradius-project-default`. Any glibc image with bash and git; Alpine/musl is not supported (say so in the help text). Custom tag allowed. |
| Project | setup commands | Free text, one per line. Chips offer common commands by ecosystem (`npm ci`, `pnpm install`, `uv sync`, `pip install -r requirements.txt`, `go mod download`, `bundle install`, `composer install`); a chip appends a line. The verify tab shows the result. |
| Repository | name | Derived from the URL's last path segment without `.git`; editable. |
| Repository | branch | After the check runs: pick list of the branches it returned, default branch preselected. Before the check: a text field with "run check to list branches". |
| Repository | auth secret | Pick list of secrets (project ones first, then global) plus "create a secret" inline. |
| Secret | key | Free text, uppercase enforced, uniqueness checked live. Where a secret is created inline from another form, the key is prefilled from context (for example `GITHUB_TOKEN` from the catalog entry). |
| Connection | transport | Two choices, shown as a segmented control, not a dropdown. |
| Connection | catalog entry | Picking one fills slug, name, transport, command, args, and the env or header keys it needs. |
| Connection | command | For stdio: suggestions `npx`, `uvx`, `docker`, `node`, `python`; free text allowed. |
| Connection | env and header values | A value field with an "insert secret" control that inserts `${secret:NAME}` from a pick list of existing secrets, plus "create a secret" inline. Unknown references are flagged on the field. |
| Agent | slug | Derived from the name; editable. |
| Agent | runtime | Pick list from `GET /api/system/runtimes`; hidden when there is only one, which is the case in version one. |
| Agent | connections | Checklist of existing connections, each showing its tool names from `last_test` and a "not tested" or "failed" badge; plus "create a connection" inline. |
| Agent | prompt | Starter templates and the guide panel (§3.4). No autocomplete. |
| Attach agent | agent | Pick list of agents not yet attached. |
| Attach agent | connection overrides | Pick a connection from the agent's list; its current fields appear prefilled, the user edits what differs. |
| Attach agent | secret overrides | Left side: pick list of the secret names the agent's connections reference. Right side: pick list of this project's secrets. |
| Send task | project, agent | Pick lists; the agent list is filtered to agents attached to the chosen project. |
| Send task | task id | Generated (`<project-slug>-<short random>`); editable. |
| Send task | context | JSON editor with validation; empty is allowed and sent as null. |
| Send task | attachments | File upload; names and content types derived from the files. |
| Answer | request | Implicit: the answer box sits under the info request it answers. |
| Everywhere | ids | Never typed. Every reference to another entity is a pick list or a link. |
| Everywhere | tables | Filters offer the values present in the data, not free text, except for search. |
